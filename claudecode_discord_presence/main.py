"""Monitor Claude Code sessions and update Discord Rich Presence."""

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from pypresence import Presence

from .single_instance import InstanceLock, PID_FILE, STOP_FILE

CLIENT_ID = "1488214388920815667"
POLL_INTERVAL_SEC = 60
STOP_POLL_SEC = 2
IDLE_TIMEOUT_SEC = 600  # 10 minutes
SUBPROCESS_TIMEOUT_SEC = 10
CLAUDE_PROCESS_NAME = "claude.exe" if sys.platform == "win32" else "claude"


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
    if sys.platform == "win32":
        try:
            result = subprocess.run(
                ["tasklist", "/NH", "/FI", f"IMAGENAME eq {CLAUDE_PROCESS_NAME}"],
                capture_output=True, text=True,
                creationflags=subprocess.CREATE_NO_WINDOW,
                timeout=SUBPROCESS_TIMEOUT_SEC,
            )
            return CLAUDE_PROCESS_NAME.lower() in result.stdout.lower()
        except (OSError, subprocess.TimeoutExpired):
            return False
    else:
        if shutil.which("pgrep") is None:
            return False
        try:
            return subprocess.run(
                ["pgrep", "-x", CLAUDE_PROCESS_NAME],
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


def main() -> None:
    lock = InstanceLock(PID_FILE)
    if not lock.acquire():
        print("Another instance is already running. Exiting.")
        sys.exit(0)

    projects_dir = get_claude_projects_dir()
    presence_active = False
    rpc: Presence | None = None

    print("claudecode-discord-presence started.")
    print(f"  Monitoring: {projects_dir}")
    print(f"  Poll interval: {POLL_INTERVAL_SEC}s")
    print(f"  Idle timeout: {IDLE_TIMEOUT_SEC}s")

    try:
        while True:
            # Exit if Claude Code process is gone.
            if not is_claude_running():
                print("Claude Code is not running. Exiting.")
                return

            active = is_session_active(projects_dir, IDLE_TIMEOUT_SEC)

            if active and not presence_active:
                if rpc is None:
                    rpc = connect_rpc(CLIENT_ID)
                if rpc is not None:
                    try:
                        rpc.update()
                        presence_active = True
                        print("Session active - presence shown.")
                    except Exception:
                        rpc = _drop_rpc(rpc)
                        presence_active = False

            elif not active and presence_active:
                try:
                    rpc.clear()
                    presence_active = False
                    print("Session idle - presence cleared.")
                except Exception:
                    rpc = _drop_rpc(rpc)
                    presence_active = False

            elif active and presence_active:
                try:
                    rpc.update()
                except Exception:
                    rpc = _drop_rpc(rpc)
                    presence_active = False

            time.sleep(POLL_INTERVAL_SEC)
    except KeyboardInterrupt:
        pass
    finally:
        _drop_rpc(rpc)
        lock.release()


if __name__ == "__main__":
    main()
