"""Monitor Claude Code sessions and update Discord Rich Presence."""

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from pypresence import Presence

import logging

from . import __version__
from .logsetup import LOG_FILE, configure_logging
from .single_instance import InstanceLock, PID_FILE, STOP_FILE

logger = logging.getLogger("claudecode_discord_presence")

def _env_int(name: str, default: int) -> int:
    """Return a positive int from env var `name`, or `default` if unset/invalid."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


CLIENT_ID = "1488214388920815667"
POLL_INTERVAL_SEC = _env_int("CCDP_POLL_INTERVAL_SEC", 15)
STOP_POLL_SEC = 2
IDLE_TIMEOUT_SEC = _env_int("CCDP_IDLE_TIMEOUT_SEC", 600)
EXIT_CONFIRM_COUNT = _env_int("CCDP_EXIT_CONFIRM_COUNT", 3)
SUBPROCESS_TIMEOUT_SEC = 10


def _claude_process_name() -> str:
    """Resolve the Claude Code process name (env override, else platform default)."""
    override = os.environ.get("CCDP_CLAUDE_PROCESS_NAME")
    if override:
        return override
    return "claude.exe" if sys.platform == "win32" else "claude"


def get_claude_projects_dir() -> Path:
    """Return the path to Claude Code's projects directory."""
    return Path.home() / ".claude" / "projects"


def find_latest_jsonl_mtime(projects_dir: Path) -> float | None:
    """Find the most recently modified .jsonl file and return its mtime.

    Returns None if no .jsonl files exist.
    """
    latest = None
    try:
        for jsonl in projects_dir.rglob("*.jsonl"):
            mtime = jsonl.stat().st_mtime
            if latest is None or mtime > latest:
                latest = mtime
    except OSError:
        return None
    return latest


def is_session_active(projects_dir: Path, timeout_sec: int) -> bool:
    """Check if a Claude Code session is active.

    A session is considered active if any .jsonl file in the projects
    directory was modified within timeout_sec seconds.
    """
    latest_mtime = find_latest_jsonl_mtime(projects_dir)
    if latest_mtime is None:
        return False
    return (time.time() - latest_mtime) < timeout_sec


def connect_rpc(client_id: str) -> Presence | None:
    """Attempt to connect to Discord RPC. Returns None on failure."""
    try:
        rpc = Presence(client_id)
        rpc.connect()
        return rpc
    except Exception:
        return None


def is_claude_running() -> bool:
    """Check if any Claude Code process is running."""
    name = _claude_process_name()
    if sys.platform == "win32":
        try:
            result = subprocess.run(
                ["tasklist", "/NH", "/FI", f"IMAGENAME eq {name}"],
                capture_output=True, text=True,
                creationflags=subprocess.CREATE_NO_WINDOW,
                timeout=SUBPROCESS_TIMEOUT_SEC,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        target = name.lower()
        # A tasklist /NH data row starts with the image name; the
        # "INFO: No tasks..." message and unrelated text do not.
        return any(
            line.strip().lower().startswith(target)
            for line in result.stdout.splitlines()
        )
    # POSIX detection is EXPERIMENTAL/UNVERIFIED: Claude Code may run as a
    # `node` process, so `pgrep -x claude` can fail. Override with
    # CCDP_CLAUDE_PROCESS_NAME. See the platform policy in the design doc.
    if shutil.which("pgrep") is None:
        return False
    try:
        return subprocess.run(
            ["pgrep", "-x", name],
            capture_output=True,
            timeout=SUBPROCESS_TIMEOUT_SEC,
        ).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _stop_requested() -> bool:
    """True iff the STOP sentinel exists and targets this process.

    A sentinel targeting a different PID (stale, or meant for a previous
    instance) is removed and treated as no stop request.
    """
    try:
        target = int(STOP_FILE.read_text().strip())
    except (OSError, ValueError):
        return False
    if target == os.getpid():
        return True
    try:
        STOP_FILE.unlink()
    except OSError:
        pass
    return False


def _sleep_until_poll() -> bool:
    """Sleep up to POLL_INTERVAL_SEC, checking the stop sentinel each chunk.

    Returns True as soon as a stop is requested, else False after the full wait.
    """
    for _ in range(0, POLL_INTERVAL_SEC, STOP_POLL_SEC):
        if _stop_requested():
            return True
        time.sleep(STOP_POLL_SEC)
    return _stop_requested()


def _clear_own_stop_sentinel() -> None:
    """Remove the STOP sentinel only if it targets this process."""
    try:
        if int(STOP_FILE.read_text().strip()) == os.getpid():
            STOP_FILE.unlink()
    except (OSError, ValueError):
        pass


def _drop_rpc(rpc):
    """Best-effort clear + close, then discard the connection. Returns None.

    Used on every RPC failure path so sockets are never leaked. Callers
    reassign: ``rpc = _drop_rpc(rpc)``.
    """
    if rpc is not None:
        try:
            rpc.clear()
        except Exception:
            pass
        try:
            rpc.close()
        except Exception:
            pass
    return None


def _reconcile_presence(active, presence_active, rpc):
    """Reconcile Discord presence with session state. Returns (rpc, presence_active).

    Callers MUST assign both return values so the daemon's finally sees the
    latest rpc. (Logging replaces the prints in a later task.)
    """
    if active and not presence_active:
        if rpc is None:
            rpc = connect_rpc(CLIENT_ID)
        if rpc is not None:
            try:
                rpc.update()
                presence_active = True
                logger.info("session active - presence shown")
            except Exception:
                rpc = _drop_rpc(rpc)
                presence_active = False
    elif not active and presence_active:
        try:
            rpc.clear()
            presence_active = False
            logger.info("session idle - presence cleared")
        except Exception:
            rpc = _drop_rpc(rpc)
            presence_active = False
    elif active and presence_active:
        try:
            rpc.update()
        except Exception:
            rpc = _drop_rpc(rpc)
            presence_active = False
    return rpc, presence_active


def _run_daemon() -> None:
    configure_logging()
    lock = InstanceLock(PID_FILE)
    if not lock.acquire():
        logger.info("another instance holds the lock; exiting")
        return

    logger.info(
        "started pid=%s version=%s platform=%s poll=%ss idle=%ss",
        os.getpid(), __version__, sys.platform,
        POLL_INTERVAL_SEC, IDLE_TIMEOUT_SEC,
    )

    projects_dir = get_claude_projects_dir()
    presence_active = False
    rpc: Presence | None = None
    exit_reason = "unknown"

    try:
        while True:
            if _stop_requested():
                exit_reason = "stop requested"
                break
            if not is_claude_running():
                exit_reason = "claude gone"
                break
            active = is_session_active(projects_dir, IDLE_TIMEOUT_SEC)
            rpc, presence_active = _reconcile_presence(active, presence_active, rpc)
            if _sleep_until_poll():
                exit_reason = "stop requested"
                break
    except KeyboardInterrupt:
        exit_reason = "interrupted"
    except Exception:
        # Detached daemon: stderr is DEVNULL, so log the traceback to the file
        # before it is lost. Re-raise so behavior is otherwise unchanged.
        exit_reason = "unexpected exception"
        logger.exception("unexpected exception in daemon loop")
        raise
    finally:
        logger.info("exiting: %s", exit_reason)
        _drop_rpc(rpc)
        lock.release()
        _clear_own_stop_sentinel()


def _parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="claudecode-discord-presence")
    parser.add_argument(
        "--status", action="store_true",
        help="Report whether the daemon is running (point-in-time) and exit.",
    )
    parser.add_argument(
        "--stop", action="store_true",
        help="Ask a running daemon to stop, then exit.",
    )
    return parser.parse_args(argv)


def _cmd_status() -> None:
    probe = InstanceLock(PID_FILE)
    if probe.acquire():
        probe.release()
        print("not running")
    else:
        try:
            pid = PID_FILE.read_text().strip() or "unknown"
        except OSError:
            pid = "unknown"
        print(f"running (pid {pid})")
    print(f"  log: {LOG_FILE}")
    print(f"  pid: {PID_FILE}")


def _cmd_stop() -> None:
    probe = InstanceLock(PID_FILE)
    if probe.acquire():
        probe.release()
        print("not running")
        return
    try:
        pid = PID_FILE.read_text().strip()
    except OSError:
        pid = ""
    if not pid:
        print("running but PID unknown; cannot signal stop")
        return
    STOP_FILE.parent.mkdir(parents=True, exist_ok=True)
    STOP_FILE.write_text(pid)
    print(f"stop requested (pid {pid})")


def main() -> None:
    args = _parse_args()
    if args.status:
        return _cmd_status()
    if args.stop:
        return _cmd_stop()
    _run_daemon()


if __name__ == "__main__":
    main()
