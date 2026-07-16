# Phase A: Atomic Single-Instance Lock Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the non-atomic check-then-write PID logic with a process-lifetime OS file lock so exactly one instance can ever run, and fix the RPC socket leaks in the main loop.

**Architecture:** A new `single_instance.py` module holds an exclusive OS file lock (POSIX `fcntl.flock`, Windows `msvcrt.locking`) for the life of the process. `main()` acquires the lock or exits, and cleans up in a `try/finally`. The old `is_process_alive`/`is_already_running`/`write_pid_file`/signal-handler machinery is deleted. `hook.py` uses the same lock as a cheap non-authoritative probe. RPC failures route through one `_drop_rpc` helper that always closes the socket.

**Tech Stack:** Python ≥3.10, stdlib only for locking (`fcntl` on POSIX, `msvcrt` on Windows — no new dependencies), `pypresence` for Discord RPC (unchanged), `pytest` for tests.

## Global Constraints

- Python `>=3.10` (uses `X | None` unions, `Path` APIs). — verbatim from `pyproject.toml`.
- Sole runtime dependency is `pypresence>=4.0`. Do NOT add dependencies; locking must use stdlib (`fcntl`/`msvcrt`). — verbatim from spec/`pyproject.toml`.
- Windows (x64) is the tested/supported platform; POSIX is best-effort. Platform split on `sys.platform == "win32"`.
- The lock file path is `~/.claude/claudecode-discord-presence.pid`; it doubles as the PID file.
- The lock — not the PID file's text content — is the source of truth. Writing the PID is best-effort and must never cause `acquire()` to fail.

---

## File Structure

- Create: `claudecode_discord_presence/single_instance.py` — `InstanceLock` primitive + `PID_FILE` constant.
- Modify: `claudecode_discord_presence/main.py` — lock-gated `main()`, `try/finally` cleanup, `_drop_rpc` helper, RPC-leak fixes; delete `is_process_alive`, `is_already_running`, `write_pid_file`, `remove_pid_file`, signal handler, and its own `PID_FILE` definition.
- Modify: `claudecode_discord_presence/hook.py` — lock-based probe replacing `is_already_running`.
- Create: `tests/test_single_instance.py` — unit tests for `InstanceLock`.
- Create: `tests/lock_worker.py` — helper process for the concurrency integration test.
- Create: `tests/test_single_instance_integration.py` — the anchor test (N concurrent → exactly 1 winner).
- Create: `tests/test_hook.py` — hook probe tests.
- Modify: `tests/test_main.py` — add `_drop_rpc` and lock-exit tests; remove tests for deleted functions.

---

## Task 1: `InstanceLock` primitive

**Files:**
- Create: `claudecode_discord_presence/single_instance.py`
- Test: `tests/test_single_instance.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `PID_FILE: Path` — `Path.home() / ".claude" / "claudecode-discord-presence.pid"`.
  - `class InstanceLock:`
    - `__init__(self, path)` — `path` is a `str` or `Path`.
    - `acquire(self) -> bool` — non-blocking; `True` if this process now holds the lock, `False` if another holder exists. On success, best-effort writes `os.getpid()` into the file.
    - `release(self) -> None` — unlock, close the fd, and delete the file. Safe to call when not held.

- [ ] **Step 1: Write the failing unit tests**

Create `tests/test_single_instance.py`:

```python
import os
from pathlib import Path

from claudecode_discord_presence.single_instance import InstanceLock


def test_acquire_returns_true_when_uncontended(tmp_path):
    lock = InstanceLock(tmp_path / "x.pid")
    assert lock.acquire() is True
    lock.release()


def test_second_lock_on_same_file_fails(tmp_path):
    path = tmp_path / "x.pid"
    first = InstanceLock(path)
    assert first.acquire() is True
    second = InstanceLock(path)
    assert second.acquire() is False
    first.release()


def test_release_allows_reacquire(tmp_path):
    path = tmp_path / "x.pid"
    first = InstanceLock(path)
    assert first.acquire() is True
    first.release()
    second = InstanceLock(path)
    assert second.acquire() is True
    second.release()


def test_acquire_writes_pid(tmp_path):
    path = tmp_path / "x.pid"
    lock = InstanceLock(path)
    assert lock.acquire() is True
    assert path.read_text().strip() == str(os.getpid())
    lock.release()


def test_release_removes_pid_file(tmp_path):
    path = tmp_path / "x.pid"
    lock = InstanceLock(path)
    lock.acquire()
    lock.release()
    assert not path.exists()


def test_acquire_creates_parent_dirs(tmp_path):
    path = tmp_path / "sub" / "deep" / "x.pid"
    lock = InstanceLock(path)
    assert lock.acquire() is True
    assert path.exists()
    lock.release()


def test_release_without_acquire_is_safe(tmp_path):
    lock = InstanceLock(tmp_path / "x.pid")
    lock.release()  # must not raise
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_single_instance.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'claudecode_discord_presence.single_instance'`

- [ ] **Step 3: Write the implementation**

Create `claudecode_discord_presence/single_instance.py`:

```python
"""Cross-platform single-instance lock backed by an OS file lock.

The lock is held for the lifetime of the owning file descriptor. When the
process exits for any reason (normal, crash, kill), the OS releases the lock
automatically — so there is no stale state, PID reuse, or liveness polling to
worry about. The lock, not the file's text content, is the source of truth.
"""

import os
import sys
from pathlib import Path

PID_FILE = Path.home() / ".claude" / "claudecode-discord-presence.pid"

if sys.platform == "win32":
    import msvcrt

    def _try_lock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)

    def _unlock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _try_lock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


class InstanceLock:
    """Exclusive OS lock on a file, held for the process lifetime."""

    def __init__(self, path) -> None:
        self.path = Path(path)
        self._fd: int | None = None

    def acquire(self) -> bool:
        """Try to take the lock without blocking. Return True on success."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            _try_lock(fd)
        except OSError:
            os.close(fd)
            return False
        self._fd = fd
        self._write_pid()
        return True

    def _write_pid(self) -> None:
        # Best-effort only: the lock is the source of truth, not this text.
        try:
            os.lseek(self._fd, 0, os.SEEK_SET)
            data = str(os.getpid()).encode()
            os.write(self._fd, data)
            os.ftruncate(self._fd, len(data))
        except OSError:
            pass

    def release(self) -> None:
        """Unlock, close the fd, and remove the file. Safe if not held."""
        if self._fd is None:
            return
        try:
            _unlock(self._fd)
        except OSError:
            pass
        try:
            os.close(self._fd)
        except OSError:
            pass
        self._fd = None
        try:
            self.path.unlink()
        except OSError:
            pass
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_single_instance.py -v`
Expected: PASS (7 passed)

- [ ] **Step 5: Commit**

```bash
git add claudecode_discord_presence/single_instance.py tests/test_single_instance.py
git commit -m "feat: add InstanceLock single-instance OS file lock primitive"
```

---

## Task 2: Concurrency anchor test (N processes → exactly 1 winner)

**Files:**
- Create: `tests/lock_worker.py`
- Test: `tests/test_single_instance_integration.py`

**Interfaces:**
- Consumes: `InstanceLock`, `PID_FILE` from Task 1 (worker imports `InstanceLock`).
- Produces: nothing consumed by later tasks. This test is the regression anchor for the "33 orphaned processes" bug.

- [ ] **Step 1: Write the worker helper**

Create `tests/lock_worker.py`:

```python
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
```

- [ ] **Step 2: Write the failing integration test**

Create `tests/test_single_instance_integration.py`:

```python
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
```

- [ ] **Step 3: Run the test**

Run: `python -m pytest tests/test_single_instance_integration.py -v`
Expected: PASS — exactly one worker exits 0, the other seven exit 3. (This exercises the real primitive under concurrency; it must be green now that Task 1 is implemented. If it fails, the lock is not actually mutually exclusive — stop and fix `single_instance.py`.)

- [ ] **Step 4: Commit**

```bash
git add tests/lock_worker.py tests/test_single_instance_integration.py
git commit -m "test: add concurrency anchor — N starts yield exactly one lock winner"
```

---

## Task 3: RPC socket-leak fix (`_drop_rpc`)

**Files:**
- Modify: `claudecode_discord_presence/main.py` (add `_drop_rpc`; rewire the three in-loop RPC failure paths around lines 171-197)
- Test: `tests/test_main.py` (add `TestDropRpc`)

**Interfaces:**
- Consumes: nothing new.
- Produces: `_drop_rpc(rpc) -> None` — best-effort `clear()` then `close()` on a `Presence` (or `None`); always returns `None`; never raises.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_main.py` (append a new class):

```python
class TestDropRpc:
    def test_none_is_safe(self):
        from claudecode_discord_presence.main import _drop_rpc
        assert _drop_rpc(None) is None

    def test_clears_and_closes(self):
        from claudecode_discord_presence.main import _drop_rpc
        rpc = MagicMock()
        assert _drop_rpc(rpc) is None
        rpc.clear.assert_called_once()
        rpc.close.assert_called_once()

    def test_close_called_even_if_clear_raises(self):
        from claudecode_discord_presence.main import _drop_rpc
        rpc = MagicMock()
        rpc.clear.side_effect = OSError("ipc")
        assert _drop_rpc(rpc) is None
        rpc.close.assert_called_once()

    def test_no_raise_if_close_raises(self):
        from claudecode_discord_presence.main import _drop_rpc
        rpc = MagicMock()
        rpc.close.side_effect = OSError("ipc")
        assert _drop_rpc(rpc) is None  # must not raise
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_main.py::TestDropRpc -v`
Expected: FAIL — `ImportError: cannot import name '_drop_rpc'`

- [ ] **Step 3: Add the helper**

In `claudecode_discord_presence/main.py`, add this function directly above `def main()` (after `connect_rpc`):

```python
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
```

- [ ] **Step 4: Rewire the loop's RPC failure paths**

In `main()`, replace the three RPC branches (currently `main.py:171-197`) with:

```python
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
                # Drop the connection so the next loop reconnects instead of
                # leaving a stale presence shown.
                rpc = _drop_rpc(rpc)
                presence_active = False

        elif active and presence_active:
            try:
                rpc.update()
            except Exception:
                rpc = _drop_rpc(rpc)
                presence_active = False
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_main.py -v`
Expected: PASS (all existing tests plus `TestDropRpc` — the existing `TestMainLoopRpcErrors` assertions still hold).

- [ ] **Step 6: Commit**

```bash
git add claudecode_discord_presence/main.py tests/test_main.py
git commit -m "fix: close RPC socket on every failure path via _drop_rpc"
```

---

## Task 4: Lock-gated `main()` and dead-code removal

**Files:**
- Modify: `claudecode_discord_presence/main.py`
- Test: `tests/test_main.py`

**Interfaces:**
- Consumes: `InstanceLock`, `PID_FILE` from Task 1; `_drop_rpc` from Task 3.
- Produces: `main()` now takes the lock via `InstanceLock(PID_FILE)`, exits `0` if unavailable, and cleans up in `try/finally`. Deletes `is_process_alive`, `is_already_running`, `write_pid_file`, `remove_pid_file`, the signal handler, and `main.py`'s own `PID_FILE` definition.

- [ ] **Step 1: Update the test file — remove obsolete tests, add lock tests**

In `tests/test_main.py`:

1. Change the import block at the top so it no longer imports deleted names. Replace the existing `from claudecode_discord_presence.main import (...)` block with:

```python
from claudecode_discord_presence.main import (
    connect_rpc,
    find_latest_jsonl_mtime,
    get_claude_projects_dir,
    is_claude_running,
    is_session_active,
)
```

2. Delete these three whole test classes (their functions no longer exist): `TestIsProcessAlive`, `TestPidFileManagement`, `TestIsAlreadyRunning`.

3. Add a new class for the lock-gated `main()`:

```python
class TestMainSingleInstance:
    def test_exits_zero_when_lock_unavailable(self, monkeypatch):
        from claudecode_discord_presence import main as main_mod

        fake_lock = MagicMock()
        fake_lock.acquire.return_value = False
        monkeypatch.setattr(main_mod, "InstanceLock", lambda path: fake_lock)

        with pytest.raises(SystemExit) as exc:
            main_mod.main()

        assert exc.value.code == 0
        fake_lock.acquire.assert_called_once()

    def test_releases_lock_on_exit(self, monkeypatch):
        from claudecode_discord_presence import main as main_mod

        fake_lock = MagicMock()
        fake_lock.acquire.return_value = True
        monkeypatch.setattr(main_mod, "InstanceLock", lambda path: fake_lock)

        def _boom():
            raise RuntimeError("boom")

        # First loop iteration raises, so the finally-block cleanup must run.
        monkeypatch.setattr(main_mod, "is_claude_running", _boom)

        with pytest.raises(RuntimeError):
            main_mod.main()

        fake_lock.release.assert_called_once()
```

- [ ] **Step 2: Run tests to verify the new ones fail**

Run: `python -m pytest tests/test_main.py::TestMainSingleInstance -v`
Expected: FAIL — `AttributeError: ... does not have the attribute 'InstanceLock'` (not imported into `main` yet).

- [ ] **Step 3: Rewrite `main.py` top matter and `main()`**

a) Replace the `PID_FILE = ...` definition (`main.py:19`) — delete that line and add `InstanceLock`, `PID_FILE` to the imports. The import section should read:

```python
import shutil
import subprocess
import sys
import time
from pathlib import Path

from pypresence import Presence

from .single_instance import InstanceLock, PID_FILE
```

(Remove the now-unused `import os` and `import signal` — `os` is no longer used in `main.py` after the deletions below, and `signal` is gone. Keep `shutil`, `subprocess`, `sys`, `time`, `Path`. The `exceptions as rpc_exceptions` alias was already unused and should be dropped.)

b) Delete these four functions entirely: `write_pid_file`, `remove_pid_file`, `is_process_alive`, `is_already_running`.

c) Replace the whole `def main()` body with:

```python
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
```

Note: `rpc` is a local of `main()` (not a nested closure variable), so its latest value is visible in the `finally` block. The `is_claude_running()` exit path now just `return`s and lets `finally` do the cleanup.

- [ ] **Step 4: Run the full main test file**

Run: `python -m pytest tests/test_main.py -v`
Expected: PASS (obsolete classes gone; `TestMainSingleInstance`, `TestDropRpc`, and all remaining tests green).

- [ ] **Step 5: Commit**

```bash
git add claudecode_discord_presence/main.py tests/test_main.py
git commit -m "refactor: gate main() on atomic lock, drop is_process_alive/PID check-then-write"
```

---

## Task 5: `hook.py` lock-based probe

**Files:**
- Modify: `claudecode_discord_presence/hook.py`
- Test: `tests/test_hook.py`

**Interfaces:**
- Consumes: `InstanceLock`, `PID_FILE` from Task 1.
- Produces: `hook.main()` acquires a probe lock; if unavailable it returns without launching; otherwise it releases the probe and spawns the detached main process.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_hook.py`:

```python
from unittest.mock import MagicMock, patch

from claudecode_discord_presence import hook as hook_mod


def test_skips_launch_when_lock_held(monkeypatch):
    fake_lock = MagicMock()
    fake_lock.acquire.return_value = False
    monkeypatch.setattr(hook_mod, "InstanceLock", lambda path: fake_lock)

    with patch.object(hook_mod.subprocess, "Popen") as popen:
        hook_mod.main()

    popen.assert_not_called()
    fake_lock.release.assert_not_called()


def test_launches_when_lock_free(monkeypatch):
    fake_lock = MagicMock()
    fake_lock.acquire.return_value = True
    monkeypatch.setattr(hook_mod, "InstanceLock", lambda path: fake_lock)

    with patch.object(hook_mod.subprocess, "Popen") as popen:
        hook_mod.main()

    fake_lock.acquire.assert_called_once()
    fake_lock.release.assert_called_once()
    popen.assert_called_once()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_hook.py -v`
Expected: FAIL — `AttributeError: module 'claudecode_discord_presence.hook' has no attribute 'InstanceLock'`

- [ ] **Step 3: Rewrite `hook.py`**

Replace the entire contents of `claudecode_discord_presence/hook.py` with:

```python
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
```

- [ ] **Step 4: Run the hook tests**

Run: `python -m pytest tests/test_hook.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Run the full suite**

Run: `python -m pytest -v`
Expected: PASS — all tests across `test_single_instance.py`, `test_single_instance_integration.py`, `test_main.py`, and `test_hook.py` green.

- [ ] **Step 6: Commit**

```bash
git add claudecode_discord_presence/hook.py tests/test_hook.py
git commit -m "refactor: replace hook os.kill precheck with lock-based probe"
```

---

## Self-Review Notes

- **Spec coverage:** §1 primitive → Task 1; §2 `main()` rewrite + dead-code/signal removal → Task 4; §3 RPC leak → Task 3; §4 hook probe → Task 5; TDD anchor (N→1) → Task 2; obsolete-test cleanup → Task 4 Step 1. All spec sections mapped.
- **Ordering rationale:** primitive before its consumers; `_drop_rpc` (Task 3) before `main()` rewrite (Task 4) which references it in `finally`; `PID_FILE` is defined in `single_instance.py` (Task 1) and `main.py`'s copy is removed in Task 4, so both `main` and `hook` import the single definition.
- **Type consistency:** `InstanceLock(path).acquire() -> bool` / `.release() -> None` and `_drop_rpc(rpc) -> None` are used identically across Tasks 3–5.
- **`os` import:** removed from `main.py` in Task 4 because every remaining `os.*` user (`getpid`, `kill`) was deleted; `single_instance.py` owns all `os` usage now.
