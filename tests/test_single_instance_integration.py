import subprocess
import sys
from pathlib import Path

WORKER = Path(__file__).parent / "lock_worker.py"


def test_concurrent_acquire_yields_exactly_one_winner(tmp_path):
    lock_path = tmp_path / "race.pid"
    hold_seconds = "2.0"  # winner holds long enough for all workers to attempt
    n = 8

    procs = [
        subprocess.Popen(
            [sys.executable, str(WORKER), str(lock_path), hold_seconds]
        )
        for _ in range(n)
    ]
    codes = [p.wait() for p in procs]

    winners = codes.count(0)
    assert winners == 1, f"expected exactly 1 winner, got {winners} (codes={codes})"
