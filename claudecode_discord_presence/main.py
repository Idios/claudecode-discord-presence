"""Monitor coding-agent sessions and update Discord Rich Presence."""

import argparse
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
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


@dataclass(frozen=True)
class Tool:
    """A monitored coding tool and how to detect/display it.

    ``sessions_dir`` is the directory polled for recent activity. When it is
    None the tool is "process-only": it is active for as long as its process
    is running (no idle clearing).
    """

    key: str
    label: str
    client_id: str
    process_name: str
    sessions_dir: Path | None


_DEFAULT_PROCESS_NAMES = {
    "claude": ("claude.exe", "claude"),
    "codex": ("codex.exe", "codex"),
    "zed": ("Zed.exe", "zed"),
}


def _default_process_name(key: str) -> str:
    """Platform default image name for a known tool key."""
    win, posix = _DEFAULT_PROCESS_NAMES[key]
    return win if sys.platform == "win32" else posix


def _claude_process_name() -> str:
    """Resolve the Claude Code process name (env override, else platform default)."""
    override = os.environ.get("CCDP_CLAUDE_PROCESS_NAME")
    if override:
        return override
    return _default_process_name("claude")


def get_claude_projects_dir() -> Path:
    """Return the path to Claude Code's projects directory."""
    return Path.home() / ".claude" / "projects"


def _env_sessions_dir(name: str, default: Path | None) -> Path | None:
    """Read a sessions-dir env var. Empty string means "process-only" (None)."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    if not raw.strip():
        return None
    return Path(raw).expanduser()


def _build_tool(key: str, label: str, default_sessions_dir: Path | None) -> Tool:
    """Build a tool from ``CCDP_{KEY}_*`` env vars.

    Every tool is enabled by default and falls back to the built-in Discord
    client ID, so the presence text stays "🦀ClaudeCode🦀" unless a
    tool-specific CCDP_<KEY>_CLIENT_ID overrides it.
    """
    env = key.upper()
    client_id = os.environ.get(f"CCDP_{env}_CLIENT_ID") or CLIENT_ID
    process_name = (
        os.environ.get(f"CCDP_{env}_PROCESS_NAME") or _default_process_name(key)
    )
    sessions_dir = _env_sessions_dir(f"CCDP_{env}_SESSIONS_DIR", default_sessions_dir)
    return Tool(key, label, client_id, process_name, sessions_dir)


def resolve_tools() -> list[Tool]:
    """Return the monitored tools: Claude Code, Codex, and Zed.

    All three are enabled by default and share the built-in client ID (unless
    overridden per tool), so presence is shown whenever any of them is active.
    """
    return [
        _build_tool("claude", "🦀ClaudeCode🦀", get_claude_projects_dir()),
        _build_tool("codex", "Codex", Path.home() / ".codex" / "sessions"),
        _build_tool("zed", "Zed", None),
    ]


def find_latest_jsonl_mtime(sessions_dir: Path) -> float | None:
    """Find the most recently modified .jsonl file and return its mtime.

    Returns None if no .jsonl files exist.
    """
    latest = None
    try:
        for jsonl in sessions_dir.rglob("*.jsonl"):
            mtime = jsonl.stat().st_mtime
            if latest is None or mtime > latest:
                latest = mtime
    except OSError:
        return None
    return latest


def is_session_active(sessions_dir: Path, timeout_sec: int) -> bool:
    """Check if a session is active.

    A session is considered active if any .jsonl file in the directory was
    modified within timeout_sec seconds.
    """
    latest_mtime = find_latest_jsonl_mtime(sessions_dir)
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


def is_process_running(process_name: str) -> bool:
    """Check if any process with the exact image name `process_name` is running."""
    name = process_name
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
    # POSIX detection is EXPERIMENTAL/UNVERIFIED: an agent may run as a `node`
    # process, so `pgrep -x` can fail. Override with CCDP_<KEY>_PROCESS_NAME.
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


def is_claude_running() -> bool:
    """Check if any Claude Code process is running."""
    return is_process_running(_claude_process_name())


def resolve_active_tool(running: list[Tool], timeout_sec: int) -> Tool | None:
    """Pick which running tool to show, or None if none is active.

    `running` are the tools whose process is currently detected. A file-based
    tool is "active" when its newest session file was modified within
    `timeout_sec` seconds; a process-only tool is active whenever it runs.

    File-based activity is a stronger signal than merely having an editor
    open, so a recently-active file-based tool wins over any running
    process-only tool. Process-only tools are only the fallback.
    """
    # Prefer the file-based tool whose session was most recently updated.
    best_file: Tool | None = None
    best_mtime: float | None = None
    for tool in running:
        if tool.sessions_dir is None:
            continue
        mtime = find_latest_jsonl_mtime(tool.sessions_dir)
        if mtime is None:
            continue  # running but no session files
        if (time.time() - mtime) >= timeout_sec:
            continue  # running but idle
        if best_mtime is None or mtime > best_mtime:
            best_mtime = mtime
            best_file = tool
    if best_file is not None:
        return best_file
    # Otherwise fall back to any running process-only tool.
    for tool in running:
        if tool.sessions_dir is None:
            return tool
    return None


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


def _reconcile_presence(active, presence_active, rpc, rpc_client_id):
    """Reconcile Discord presence with the active tool.

    `active` is a Tool to show, or None to clear. Reconnects (with a different
    Discord client ID) when the active tool changes. Returns
    (rpc, rpc_client_id, presence_active); callers MUST assign all three.
    """
    if active is not None:
        target_id = active.client_id
        if rpc is None or rpc_client_id != target_id:
            if rpc is not None:
                rpc = _drop_rpc(rpc)
            rpc = connect_rpc(target_id)
            rpc_client_id = target_id if rpc is not None else None
        if rpc is not None:
            try:
                rpc.update()
                presence_active = True
                logger.info("session active (%s) - presence shown", active.key)
            except Exception:
                rpc = _drop_rpc(rpc)
                rpc_client_id = None
                presence_active = False
    elif presence_active:
        try:
            rpc.clear()
            presence_active = False
            logger.info("session idle - presence cleared")
        except Exception:
            rpc = _drop_rpc(rpc)
            rpc_client_id = None
            presence_active = False
    return rpc, rpc_client_id, presence_active


def _run_daemon() -> None:
    configure_logging()
    lock = InstanceLock(PID_FILE)
    if not lock.acquire():
        logger.info("another instance holds the lock; exiting")
        return

    tools = resolve_tools()
    logger.info(
        "started pid=%s version=%s platform=%s poll=%ss idle=%ss tools=%s",
        os.getpid(), __version__, sys.platform,
        POLL_INTERVAL_SEC, IDLE_TIMEOUT_SEC,
        ",".join(t.key for t in tools),
    )

    presence_active = False
    rpc: Presence | None = None
    rpc_client_id: str | None = None
    exit_reason = "unknown"
    missed = 0

    try:
        while True:
            if _stop_requested():
                exit_reason = "stop requested"
                break
            running = [t for t in tools if is_process_running(t.process_name)]
            if running:
                missed = 0
            else:
                missed += 1
                if missed >= EXIT_CONFIRM_COUNT:
                    exit_reason = "all tools gone"
                    break
            active = resolve_active_tool(running, IDLE_TIMEOUT_SEC)
            rpc, rpc_client_id, presence_active = _reconcile_presence(
                active, presence_active, rpc, rpc_client_id,
            )
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
