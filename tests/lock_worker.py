"""Helper process for the single-instance concurrency test.

Usage: python lock_worker.py <lock_path> <hold_seconds>
Exit code 0 = acquired the lock; 3 = did not acquire it.
"""

import sys
import time
from pathlib import Path

# Make the package importable when this file is run as a standalone script.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from claudecode_discord_presence.single_instance import InstanceLock


def main() -> None:
    lock_path = Path(sys.argv[1])
    hold_seconds = float(sys.argv[2])
    lock = InstanceLock(lock_path)
    if lock.acquire():
        # Hold long enough that every sibling worker has attempted acquire.
        time.sleep(hold_seconds)
        lock.release()
        sys.exit(0)
    sys.exit(3)


if __name__ == "__main__":
    main()
