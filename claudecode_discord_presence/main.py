"""Monitor Claude Code sessions and update Discord Rich Presence."""

import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from pypresence import Presence, exceptions as rpc_exceptions

CLIENT_ID = "1488214388920815667"
POLL_INTERVAL_SEC = 60
IDLE_TIMEOUT_SEC = 600  # 10 minutes
SUBPROCESS_TIMEOUT_SEC = 10
CLAUDE_PROCESS_NAME = "claude.exe" if sys.platform == "win32" else "claude"

PID_FILE = Path.home() / ".claude" / "claudecode-discord-presence.pid"


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


def write_pid_file() -> None:
    """Write current process PID to the PID file."""
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()))


def remove_pid_file() -> None:
    """Remove the PID file if it exists."""
    try:
        PID_FILE.unlink(missing_ok=True)
    except OSError:
        pass


def is_process_alive(pid: int) -> bool:
    """Check if a process with the given PID is alive."""
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


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


def is_already_running() -> bool:
    """Check if another instance is already running via PID file."""
    if not PID_FILE.exists():
        return False
    try:
        pid = int(PID_FILE.read_text().strip())
    except (ValueError, OSError):
        return False
    if pid == os.getpid():
        return False
    return is_process_alive(pid)


def main() -> None:
    if is_already_running():
        print("Another instance is already running. Exiting.")
        sys.exit(0)

    write_pid_file()
    projects_dir = get_claude_projects_dir()
    presence_active = False
    rpc: Presence | None = None

    def shutdown(signum: int, frame: object) -> None:
        nonlocal rpc
        if rpc is not None:
            try:
                rpc.clear()
                rpc.close()
            except Exception:
                pass
        remove_pid_file()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    print("claudecode-discord-presence started.")
    print(f"  Monitoring: {projects_dir}")
    print(f"  Poll interval: {POLL_INTERVAL_SEC}s")
    print(f"  Idle timeout: {IDLE_TIMEOUT_SEC}s")

    while True:
        # Exit if Claude Code process is gone
        if not is_claude_running():
            if rpc is not None:
                try:
                    rpc.clear()
                    rpc.close()
                except Exception:
                    pass
            remove_pid_file()
            print("Claude Code is not running. Exiting.")
            sys.exit(0)

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
                    rpc = None

        elif not active and presence_active:
            if rpc is not None:
                try:
                    rpc.clear()
                    print("Session idle - presence cleared.")
                except Exception:
                    pass
            presence_active = False

        elif active and presence_active:
            if rpc is not None:
                try:
                    rpc.update()
                except Exception:
                    rpc = None
                    presence_active = False

        time.sleep(POLL_INTERVAL_SEC)


if __name__ == "__main__":
    main()
