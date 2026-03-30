"""Claude Code hook entry point.

Started by SessionStart hook. Launches main process in background
if not already running.
"""

import subprocess
import sys
from pathlib import Path

from .main import is_already_running


def main() -> None:
    if is_already_running():
        return

    # Launch main process in background
    main_module = Path(__file__).parent / "main.py"
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
