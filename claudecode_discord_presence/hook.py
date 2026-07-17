"""Claude Code hook entry point.

Started by the SessionStart hook. Launches the main process in the background
unless one is already running. The probe below is only a cheap optimization —
the real single-instance guarantee is the lock that main() itself holds.
"""

import subprocess
import sys

from .single_instance import InstanceLock, PID_FILE


def main() -> None:
    # Cheap, non-authoritative check: if the lock is already held, skip the
    # cost of spawning a process that would only exit immediately.
    probe = InstanceLock(PID_FILE)
    if not probe.acquire():
        return
    probe.release()  # let the real main process take the lock

    subprocess.Popen(
        [sys.executable, "-m", "claudecode_discord_presence.main"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW
        if sys.platform == "win32"
        else 0,
        start_new_session=True,
    )


if __name__ == "__main__":
    main()
