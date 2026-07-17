# Phase B: Observability and Safety Net Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add file logging, `--status`/`--stop` subcommands (PID-matched stop sentinel), a shorter interruptible poll loop, and a test refactor that tests the real presence-reconciliation code.

**Architecture:** A new `logsetup.py` owns rotating file logging, configured only by the long-lived daemon so the log has a single writer. `main()` becomes an argparse dispatcher over `_cmd_status`, `_cmd_stop`, and `_run_daemon` (the renamed daemon loop). `--stop` writes a sentinel file containing the running daemon's PID; the daemon honors only its own PID and cleans stale sentinels. The 3-branch presence logic is extracted into `_reconcile_presence` so it can be tested directly instead of via a reimplemented loop.

**Tech Stack:** Python ≥3.10, stdlib `logging`/`logging.handlers.RotatingFileHandler`/`argparse` (no new dependencies), `pypresence` (unchanged), `pytest`.

## Global Constraints

- Python `>=3.10`; sole runtime dependency `pypresence>=4.0`. Logging/CLI use stdlib only — no new dependencies.
- Windows (x64) is the primary/tested platform; POSIX best-effort.
- **File logging is daemon-only.** `configure_logging()` is called only by `_run_daemon()`. `--status`/`--stop` print to stdout; `hook.py` writes failures to stderr. The rotating log must always have exactly one writer.
- The OS lock (`InstanceLock`) is the single-instance source of truth. The PID file is authoritative only while the lock is held.
- The STOP sentinel's content is the target daemon's PID string. The daemon acts on it only when it equals `os.getpid()`; a mismatched sentinel is stale — delete it and continue.
- `_reconcile_presence(active, presence_active, rpc)` returns `(rpc, presence_active)`; callers MUST assign both return values so the daemon's `finally` sees the latest `rpc`.
- All `print()` in the daemon path is replaced by logging. Phase A's `try/finally` cleanup structure and the atomic-lock gating are preserved.

## Constants (final values, introduced across tasks)

- `POLL_INTERVAL_SEC = 15` (was 60; changed in Task 5)
- `STOP_POLL_SEC = 2`
- `LOG_FILE = ~/.claude/claudecode-discord-presence.log`, `LOG_MAX_BYTES = 256*1024`, `LOG_BACKUP_COUNT = 2`
- `STOP_FILE = ~/.claude/claudecode-discord-presence.stop`

---

## File Structure

- Create: `claudecode_discord_presence/logsetup.py` — `LOG_FILE`, `configure_logging()`.
- Modify: `claudecode_discord_presence/single_instance.py` — add `STOP_FILE` constant.
- Modify: `claudecode_discord_presence/main.py` — STOP primitives, `_reconcile_presence`, argparse dispatch, `_cmd_status`, `_cmd_stop`, `_run_daemon`, logging.
- Modify: `claudecode_discord_presence/hook.py` — stderr on `Popen` failure.
- Create: `tests/test_logsetup.py`, `tests/test_cli.py`.
- Modify: `tests/test_main.py` — remove `TestMainLoopRpcErrors`, add `TestReconcilePresence`/`TestStopRequested`/`TestSleepUntilPoll`/`TestClearOwnStopSentinel`, update `TestMainSingleInstance`, fix `test_tasklist_oserror_returns_false`.

---

## Task 1: Logging setup module

**Files:**
- Create: `claudecode_discord_presence/logsetup.py`
- Test: `tests/test_logsetup.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `LOG_FILE: Path` = `~/.claude/claudecode-discord-presence.log`
  - `configure_logging(log_file: Path = LOG_FILE) -> logging.Logger` — idempotent; attaches a `RotatingFileHandler` (256KB×2) and a stderr `StreamHandler` to the package logger `"claudecode_discord_presence"` and returns it.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_logsetup.py`:

```python
import logging
from pathlib import Path

import pytest

from claudecode_discord_presence import logsetup


@pytest.fixture(autouse=True)
def _reset_logger():
    """Ensure a clean package logger before and after each test."""
    logger = logging.getLogger("claudecode_discord_presence")
    saved = logger.handlers[:]
    logger.handlers.clear()
    yield
    logger.handlers.clear()
    logger.handlers.extend(saved)


def test_configures_two_handlers(tmp_path):
    logger = logsetup.configure_logging(tmp_path / "x.log")
    kinds = {type(h).__name__ for h in logger.handlers}
    assert "RotatingFileHandler" in kinds
    assert "StreamHandler" in kinds


def test_idempotent(tmp_path):
    logsetup.configure_logging(tmp_path / "x.log")
    logger = logsetup.configure_logging(tmp_path / "x.log")
    assert len(logger.handlers) == 2  # not doubled


def test_writes_to_file(tmp_path):
    log_file = tmp_path / "x.log"
    logger = logsetup.configure_logging(log_file)
    logger.info("hello-marker")
    for h in logger.handlers:
        h.flush()
    assert "hello-marker" in log_file.read_text(encoding="utf-8")


def test_creates_parent_dir(tmp_path):
    log_file = tmp_path / "sub" / "deep" / "x.log"
    logsetup.configure_logging(log_file)
    assert log_file.parent.exists()


def test_log_file_default_path():
    assert logsetup.LOG_FILE.name == "claudecode-discord-presence.log"
    assert logsetup.LOG_FILE.parent.name == ".claude"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_logsetup.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'claudecode_discord_presence.logsetup'`

- [ ] **Step 3: Write the implementation**

Create `claudecode_discord_presence/logsetup.py`:

```python
"""Logging configuration for the daemon process.

Only the long-lived daemon calls configure_logging(). CLI subcommands print to
stdout and the hook writes to stderr, so the rotating log file always has
exactly one writer — avoiding multi-process rotation corruption on Windows.
"""

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_FILE = Path.home() / ".claude" / "claudecode-discord-presence.log"
LOG_MAX_BYTES = 256 * 1024
LOG_BACKUP_COUNT = 2

_LOGGER_NAME = "claudecode_discord_presence"


def configure_logging(log_file: Path = LOG_FILE) -> logging.Logger:
    """Configure and return the package logger (daemon-only, idempotent)."""
    logger = logging.getLogger(_LOGGER_NAME)
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    logger.propagate = False

    log_file.parent.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

    file_handler = RotatingFileHandler(
        log_file, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    stream_handler = logging.StreamHandler(sys.stderr)
    stream_handler.setFormatter(fmt)
    logger.addHandler(stream_handler)

    return logger
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_logsetup.py -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
git add claudecode_discord_presence/logsetup.py tests/test_logsetup.py
git commit -m "feat: add daemon-only rotating log configuration"
```

---

## Task 2: STOP sentinel primitives

**Files:**
- Modify: `claudecode_discord_presence/single_instance.py` (add `STOP_FILE`)
- Modify: `claudecode_discord_presence/main.py` (imports, `STOP_POLL_SEC`, `_stop_requested`, `_sleep_until_poll`, `_clear_own_stop_sentinel`)
- Test: `tests/test_main.py` (append classes), `tests/test_single_instance.py` (append one test)

**Interfaces:**
- Consumes: nothing new.
- Produces (in `main.py`, importable):
  - `STOP_FILE` (imported from `single_instance`)
  - `STOP_POLL_SEC = 2`
  - `_stop_requested() -> bool` — True iff `STOP_FILE` exists and its content == `os.getpid()`; a mismatched sentinel is unlinked and False is returned.
  - `_sleep_until_poll() -> bool` — sleeps up to `POLL_INTERVAL_SEC` in `STOP_POLL_SEC` chunks; returns True as soon as `_stop_requested()`.
  - `_clear_own_stop_sentinel() -> None` — unlink `STOP_FILE` only if it targets this PID.

- [ ] **Step 1: Add `STOP_FILE` to `single_instance.py`**

In `claudecode_discord_presence/single_instance.py`, directly below the `PID_FILE = ...` line (currently line 18):

```python
STOP_FILE = Path.home() / ".claude" / "claudecode-discord-presence.stop"
```

- [ ] **Step 2: Write the failing tests**

Append to `tests/test_single_instance.py`:

```python
def test_stop_file_default_path():
    from claudecode_discord_presence.single_instance import STOP_FILE
    assert STOP_FILE.name == "claudecode-discord-presence.stop"
    assert STOP_FILE.parent.name == ".claude"
```

Append to `tests/test_main.py` (the file already imports `os`, `MagicMock`):

```python
class TestStopRequested:
    def test_no_sentinel(self, tmp_path, monkeypatch):
        from claudecode_discord_presence import main as m
        monkeypatch.setattr(m, "STOP_FILE", tmp_path / "stop")
        assert m._stop_requested() is False

    def test_sentinel_own_pid(self, tmp_path, monkeypatch):
        from claudecode_discord_presence import main as m
        f = tmp_path / "stop"
        f.write_text(str(os.getpid()))
        monkeypatch.setattr(m, "STOP_FILE", f)
        assert m._stop_requested() is True

    def test_sentinel_other_pid_is_cleaned(self, tmp_path, monkeypatch):
        from claudecode_discord_presence import main as m
        f = tmp_path / "stop"
        f.write_text("999999")
        monkeypatch.setattr(m, "STOP_FILE", f)
        assert m._stop_requested() is False
        assert not f.exists()

    def test_sentinel_garbage(self, tmp_path, monkeypatch):
        from claudecode_discord_presence import main as m
        f = tmp_path / "stop"
        f.write_text("not-a-pid")
        monkeypatch.setattr(m, "STOP_FILE", f)
        assert m._stop_requested() is False


class TestSleepUntilPoll:
    def test_true_immediately_when_stop(self, tmp_path, monkeypatch):
        from claudecode_discord_presence import main as m
        f = tmp_path / "stop"
        f.write_text(str(os.getpid()))
        monkeypatch.setattr(m, "STOP_FILE", f)
        slept = []
        monkeypatch.setattr(m.time, "sleep", lambda s: slept.append(s))
        assert m._sleep_until_poll() is True
        assert slept == []

    def test_sleeps_full_interval_without_stop(self, tmp_path, monkeypatch):
        from claudecode_discord_presence import main as m
        monkeypatch.setattr(m, "STOP_FILE", tmp_path / "absent")
        slept = []
        monkeypatch.setattr(m.time, "sleep", lambda s: slept.append(s))
        assert m._sleep_until_poll() is False
        expected = len(range(0, m.POLL_INTERVAL_SEC, m.STOP_POLL_SEC))
        assert len(slept) == expected


class TestClearOwnStopSentinel:
    def test_removes_own(self, tmp_path, monkeypatch):
        from claudecode_discord_presence import main as m
        f = tmp_path / "stop"
        f.write_text(str(os.getpid()))
        monkeypatch.setattr(m, "STOP_FILE", f)
        m._clear_own_stop_sentinel()
        assert not f.exists()

    def test_keeps_other_pid(self, tmp_path, monkeypatch):
        from claudecode_discord_presence import main as m
        f = tmp_path / "stop"
        f.write_text("999999")
        monkeypatch.setattr(m, "STOP_FILE", f)
        m._clear_own_stop_sentinel()
        assert f.exists()
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `python -m pytest tests/test_main.py::TestStopRequested tests/test_single_instance.py::test_stop_file_default_path -v`
Expected: FAIL — `AttributeError: module 'claudecode_discord_presence.main' has no attribute 'STOP_FILE'` (and `_stop_requested` undefined).

- [ ] **Step 4: Implement in `main.py`**

Add `import os` to the imports (top of `main.py`, alongside the existing stdlib imports). Change the `single_instance` import line (currently `from .single_instance import InstanceLock, PID_FILE`) to:

```python
from .single_instance import InstanceLock, PID_FILE, STOP_FILE
```

Add `STOP_POLL_SEC = 2` next to the other constants (after `POLL_INTERVAL_SEC`). Then add these three functions above `_drop_rpc`:

```python
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
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_main.py::TestStopRequested tests/test_main.py::TestSleepUntilPoll tests/test_main.py::TestClearOwnStopSentinel tests/test_single_instance.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add claudecode_discord_presence/single_instance.py claudecode_discord_presence/main.py tests/test_main.py tests/test_single_instance.py
git commit -m "feat: add PID-matched stop sentinel primitives"
```

---

## Task 3: Extract `_reconcile_presence`

**Files:**
- Modify: `claudecode_discord_presence/main.py` (add `_reconcile_presence`, call it from the loop)
- Test: `tests/test_main.py` (remove `TestMainLoopRpcErrors`, add `TestReconcilePresence`)

**Interfaces:**
- Consumes: `connect_rpc`, `_drop_rpc`, `CLIENT_ID` (existing).
- Produces: `_reconcile_presence(active: bool, presence_active: bool, rpc) -> tuple[rpc, bool]` — one reconciliation step; callers must assign both return values.

- [ ] **Step 1: Replace the obsolete test class with direct tests**

In `tests/test_main.py`, delete the entire `class TestMainLoopRpcErrors:` (its `_run_one_iteration` reimplements the loop). Add:

```python
class TestReconcilePresence:
    def test_activates_and_shows_presence(self, monkeypatch):
        from claudecode_discord_presence import main as m
        mock_rpc = MagicMock()
        monkeypatch.setattr(m, "connect_rpc", lambda cid: mock_rpc)
        rpc, active = m._reconcile_presence(True, False, None)
        assert rpc is mock_rpc
        assert active is True
        mock_rpc.update.assert_called_once()

    def test_activation_update_failure_drops_rpc(self):
        from claudecode_discord_presence import main as m
        mock_rpc = MagicMock()
        mock_rpc.update.side_effect = BrokenPipeError()
        rpc, active = m._reconcile_presence(True, False, mock_rpc)
        assert rpc is None
        assert active is False

    def test_idle_clears_presence(self):
        from claudecode_discord_presence import main as m
        mock_rpc = MagicMock()
        rpc, active = m._reconcile_presence(False, True, mock_rpc)
        assert rpc is mock_rpc
        assert active is False
        mock_rpc.clear.assert_called_once()

    def test_idle_clear_failure_drops_rpc(self):
        from claudecode_discord_presence import main as m
        mock_rpc = MagicMock()
        mock_rpc.clear.side_effect = OSError()
        rpc, active = m._reconcile_presence(False, True, mock_rpc)
        assert rpc is None
        assert active is False

    def test_continue_updates(self):
        from claudecode_discord_presence import main as m
        mock_rpc = MagicMock()
        rpc, active = m._reconcile_presence(True, True, mock_rpc)
        assert rpc is mock_rpc
        assert active is True
        mock_rpc.update.assert_called_once()

    def test_continue_update_failure_drops_rpc(self):
        from claudecode_discord_presence import main as m
        mock_rpc = MagicMock()
        mock_rpc.update.side_effect = ConnectionResetError()
        rpc, active = m._reconcile_presence(True, True, mock_rpc)
        assert rpc is None
        assert active is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_main.py::TestReconcilePresence -v`
Expected: FAIL — `AttributeError: module 'claudecode_discord_presence.main' has no attribute '_reconcile_presence'`

- [ ] **Step 3: Add `_reconcile_presence` and call it from the loop**

In `main.py`, add this function just above `main()`:

```python
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
    return rpc, presence_active
```

In `main()`, replace the three `if/elif/elif` presence branches (currently `main.py:131-157`) with a single call:

```python
            active = is_session_active(projects_dir, IDLE_TIMEOUT_SEC)
            rpc, presence_active = _reconcile_presence(active, presence_active, rpc)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_main.py -v`
Expected: PASS (new `TestReconcilePresence` green; `TestMainSingleInstance` still green — the loop still calls `is_claude_running`/`_reconcile_presence` and cleans up in `finally`).

- [ ] **Step 5: Commit**

```bash
git add claudecode_discord_presence/main.py tests/test_main.py
git commit -m "refactor: extract _reconcile_presence and test it directly"
```

---

## Task 4: CLI subcommands and daemon split

**Files:**
- Modify: `claudecode_discord_presence/main.py` (rename `main`→`_run_daemon`; add `_parse_args`, `_cmd_status`, `_cmd_stop`, new `main`)
- Test: `tests/test_cli.py` (new), `tests/test_main.py` (retarget `TestMainSingleInstance` to `_run_daemon`)

**Interfaces:**
- Consumes: `InstanceLock`, `PID_FILE`, `STOP_FILE` (existing); `LOG_FILE` from `logsetup`.
- Produces:
  - `_run_daemon() -> None` — the daemon loop (this task keeps its body identical to the old `main()`; only the name changes).
  - `_parse_args(argv=None) -> argparse.Namespace` with boolean `.status` and `.stop`.
  - `_cmd_status() -> None`, `_cmd_stop() -> None`.
  - `main() -> None` — dispatches on args.

- [ ] **Step 1: Write the failing CLI tests**

Create `tests/test_cli.py`:

```python
import os

from claudecode_discord_presence import main as m
from claudecode_discord_presence.single_instance import InstanceLock


class TestParseArgs:
    def test_no_args(self):
        a = m._parse_args([])
        assert a.status is False and a.stop is False

    def test_status(self):
        assert m._parse_args(["--status"]).status is True

    def test_stop(self):
        assert m._parse_args(["--stop"]).stop is True


class TestCmdStatus:
    def test_not_running(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(m, "PID_FILE", tmp_path / "p.pid")
        monkeypatch.setattr(m, "LOG_FILE", tmp_path / "l.log")
        m._cmd_status()
        assert "not running" in capsys.readouterr().out

    def test_running(self, tmp_path, monkeypatch, capsys):
        pid_file = tmp_path / "p.pid"
        monkeypatch.setattr(m, "PID_FILE", pid_file)
        monkeypatch.setattr(m, "LOG_FILE", tmp_path / "l.log")
        held = InstanceLock(pid_file)
        assert held.acquire()
        try:
            m._cmd_status()
        finally:
            held.release()
        assert "running (pid" in capsys.readouterr().out


class TestCmdStop:
    def test_not_running_writes_no_sentinel(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(m, "PID_FILE", tmp_path / "p.pid")
        stop = tmp_path / "s.stop"
        monkeypatch.setattr(m, "STOP_FILE", stop)
        m._cmd_stop()
        assert "not running" in capsys.readouterr().out
        assert not stop.exists()

    def test_running_writes_pid_sentinel(self, tmp_path, monkeypatch, capsys):
        pid_file = tmp_path / "p.pid"
        stop = tmp_path / "s.stop"
        monkeypatch.setattr(m, "PID_FILE", pid_file)
        monkeypatch.setattr(m, "STOP_FILE", stop)
        held = InstanceLock(pid_file)
        assert held.acquire()
        try:
            m._cmd_stop()
        finally:
            held.release()
        assert stop.exists()
        assert stop.read_text().strip() == str(os.getpid())
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_cli.py -v`
Expected: FAIL — `AttributeError: module 'claudecode_discord_presence.main' has no attribute '_parse_args'`

- [ ] **Step 3: Implement the CLI in `main.py`**

Add `import argparse` to the imports. Add `LOG_FILE` to the logsetup import — add this line near the other imports:

```python
from .logsetup import LOG_FILE
```

Rename the existing `def main() -> None:` to `def _run_daemon() -> None:` (body unchanged). Then add, below `_run_daemon`:

```python
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
```

- [ ] **Step 4: Retarget `TestMainSingleInstance` to `_run_daemon`**

In `tests/test_main.py`, the two `TestMainSingleInstance` tests currently call `main_mod.main()`. Because `main()` now parses `sys.argv` (which under pytest is pytest's argv), they must target the daemon directly. Replace both `main_mod.main()` calls with `main_mod._run_daemon()`. The rest of each test is unchanged (lock-unavailable still `sys.exit(0)`; forced exception still triggers `finally` release).

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_cli.py tests/test_main.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add claudecode_discord_presence/main.py tests/test_cli.py tests/test_main.py
git commit -m "feat: add --status/--stop subcommands and split out _run_daemon"
```

---

## Task 5: Daemon integration — logging, stop wiring, faster loop

**Files:**
- Modify: `claudecode_discord_presence/main.py` (`_run_daemon` body, `_reconcile_presence` prints→logging, `POLL_INTERVAL_SEC`, module logger)
- Test: `tests/test_main.py` (update `TestMainSingleInstance`, add daemon-exit tests)

**Interfaces:**
- Consumes: `configure_logging` from `logsetup`; `_stop_requested`, `_sleep_until_poll`, `_clear_own_stop_sentinel`, `_reconcile_presence` (existing).
- Produces: `_run_daemon()` now logs via `configure_logging()`, exits on the stop sentinel, uses `POLL_INTERVAL_SEC = 15`, records an `exit_reason`, and clears its own sentinel in `finally`.

- [ ] **Step 1: Write the failing/updated tests**

In `tests/test_main.py`, replace the two `TestMainSingleInstance` tests with:

```python
class TestRunDaemonLifecycle:
    def test_returns_when_lock_unavailable(self, monkeypatch):
        from claudecode_discord_presence import main as m
        fake_lock = MagicMock()
        fake_lock.acquire.return_value = False
        monkeypatch.setattr(m, "InstanceLock", lambda path: fake_lock)
        monkeypatch.setattr(m, "configure_logging", lambda: m.logger)
        # Must not raise and must not enter the loop.
        m._run_daemon()
        fake_lock.acquire.assert_called_once()
        fake_lock.release.assert_not_called()

    def test_stop_sentinel_exits_and_cleans_up(self, monkeypatch):
        from claudecode_discord_presence import main as m
        fake_lock = MagicMock()
        fake_lock.acquire.return_value = True
        monkeypatch.setattr(m, "InstanceLock", lambda path: fake_lock)
        monkeypatch.setattr(m, "configure_logging", lambda: m.logger)
        monkeypatch.setattr(m, "_stop_requested", lambda: True)  # stop on first check
        cleared = []
        monkeypatch.setattr(m, "_clear_own_stop_sentinel", lambda: cleared.append(True))
        m._run_daemon()
        fake_lock.release.assert_called_once()
        assert cleared == [True]

    def test_releases_lock_on_exception(self, monkeypatch):
        from claudecode_discord_presence import main as m
        fake_lock = MagicMock()
        fake_lock.acquire.return_value = True
        monkeypatch.setattr(m, "InstanceLock", lambda path: fake_lock)
        monkeypatch.setattr(m, "configure_logging", lambda: m.logger)
        monkeypatch.setattr(m, "_stop_requested", lambda: False)

        def _boom():
            raise RuntimeError("boom")

        monkeypatch.setattr(m, "is_claude_running", _boom)
        with pytest.raises(RuntimeError):
            m._run_daemon()
        fake_lock.release.assert_called_once()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_main.py::TestRunDaemonLifecycle -v`
Expected: FAIL — `test_returns_when_lock_unavailable` fails because the current `_run_daemon` calls `sys.exit(0)` (raises `SystemExit`) and references no `m.logger`/`configure_logging`.

- [ ] **Step 3: Add the module logger and convert `_reconcile_presence` prints**

Add `import logging` and, below the imports, a module logger:

```python
from .logsetup import LOG_FILE, configure_logging
from . import __version__

logger = logging.getLogger("claudecode_discord_presence")
```

(Replace the Task-4 `from .logsetup import LOG_FILE` line with the two-name import above.)

In `_reconcile_presence`, replace the two `print(...)` calls:
- `print("Session active - presence shown.")` → `logger.info("session active - presence shown")`
- `print("Session idle - presence cleared.")` → `logger.info("session idle - presence cleared")`

- [ ] **Step 4: Rewrite `_run_daemon` and change the poll interval**

Change the constant: `POLL_INTERVAL_SEC = 60` → `POLL_INTERVAL_SEC = 15`.

Replace the entire `_run_daemon` body with:

```python
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
    finally:
        logger.info("exiting: %s", exit_reason)
        _drop_rpc(rpc)
        lock.release()
        _clear_own_stop_sentinel()
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_main.py -v`
Expected: PASS. Also confirm no stray `print(` remains in `main.py`:

Run: `grep -n "print(" claudecode_discord_presence/main.py`
Expected: only `_cmd_status`/`_cmd_stop` print lines (CLI output), none in `_run_daemon`/`_reconcile_presence`.

- [ ] **Step 6: Commit**

```bash
git add claudecode_discord_presence/main.py tests/test_main.py
git commit -m "feat: log daemon lifecycle, honor stop sentinel, poll every 15s"
```

---

## Task 6: Hook stderr logging and tasklist test fix

**Files:**
- Modify: `claudecode_discord_presence/hook.py` (log `Popen` failure to stderr)
- Test: `tests/test_hook.py` (add failure-logging test), `tests/test_main.py` (fix `test_tasklist_oserror_returns_false`)

**Interfaces:**
- Consumes: nothing new.
- Produces: `hook.main()` writes a one-line message to stderr when `subprocess.Popen` raises, instead of silently swallowing.

- [ ] **Step 1: Write the failing hook test**

Add to `tests/test_hook.py`:

```python
def test_popen_failure_writes_to_stderr(monkeypatch, capsys):
    fake_lock = MagicMock()
    fake_lock.acquire.return_value = True
    monkeypatch.setattr(hook_mod, "InstanceLock", lambda path: fake_lock)

    def _boom(*a, **k):
        raise OSError("python missing")

    monkeypatch.setattr(hook_mod.subprocess, "Popen", _boom)
    hook_mod.main()  # must not raise
    assert "failed to launch" in capsys.readouterr().err.lower()
```

- [ ] **Step 2: Fix the `test_tasklist_oserror_returns_false` test**

In `tests/test_main.py`, replace the existing `test_tasklist_oserror_returns_false` (its mock is asserted outside the platform guard, so on non-Windows the tasklist branch never runs) with a version that forces the Windows branch:

```python
    @patch("claudecode_discord_presence.main.subprocess.run")
    def test_tasklist_oserror_returns_false(self, mock_run, monkeypatch):
        """On Windows, an OSError from tasklist must yield False, not raise."""
        monkeypatch.setattr("claudecode_discord_presence.main.sys.platform", "win32")
        mock_run.side_effect = OSError("command not found")
        assert is_claude_running() is False
        mock_run.assert_called_once()
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `python -m pytest tests/test_hook.py::test_popen_failure_writes_to_stderr "tests/test_main.py::TestIsClaudeRunning::test_tasklist_oserror_returns_false" -v`
Expected: FAIL — hook currently swallows the error (no stderr output); the tasklist test now asserts `mock_run.assert_called_once()` which fails until the platform is correctly forced (it already is via monkeypatch, so this test should pass once applied — run it to confirm the RED is only the hook test if the tasklist branch already runs).

- [ ] **Step 4: Implement the hook stderr log**

Edit `claudecode_discord_presence/hook.py` — wrap the `subprocess.Popen(...)` call in try/except. Replace the `subprocess.Popen(` block with:

```python
    try:
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
    except OSError as exc:
        print(f"failed to launch presence daemon: {exc}", file=sys.stderr)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_hook.py tests/test_main.py -v`
Expected: PASS

- [ ] **Step 6: Run the full suite**

Run: `python -m pytest -q`
Expected: PASS across all test files. (Note: `tests/test_main.py::TestIsSessionActive::test_exact_boundary_timeout` is a known pre-existing Windows flake, unrelated to Phase B — if it is the only failure, re-run once to confirm.)

- [ ] **Step 7: Commit**

```bash
git add claudecode_discord_presence/hook.py tests/test_hook.py tests/test_main.py
git commit -m "feat: log hook launch failure to stderr; fix tasklist OSError test"
```

---

## Self-Review Notes

- **Spec coverage:** logging §1 → Task 1 + Task 5 (wiring); STOP primitives/§5 → Task 2; `_reconcile_presence` §3 → Task 3; CLI §2 → Task 4; daemon loop/§3 (interruptible wait, POLL=15, exit_reason, finally cleanup) → Task 5; hook §4 + tasklist test fix → Task 6; test matrix (`_stop_requested` PID cases, `--stop` no-daemon/running, stale-sentinel cleanup) → Tasks 2 & 4. All spec sections mapped.
- **Single-writer logging:** only `_run_daemon` calls `configure_logging` (Task 5); `_cmd_status`/`_cmd_stop` use `print` (Task 4); hook uses stderr `print` (Task 6). No CLI/hook path attaches the rotating handler.
- **STOP race closure:** `_cmd_stop` probes the lock before writing a sentinel (no sentinel when no daemon); `_stop_requested`/`_clear_own_stop_sentinel` gate on `os.getpid()`, so an old-PID sentinel is ignored+cleaned by a new daemon.
- **Type/name consistency:** `_reconcile_presence(active, presence_active, rpc) -> (rpc, presence_active)` used identically in Tasks 3 and 5; `_stop_requested`/`_sleep_until_poll`/`_clear_own_stop_sentinel` signatures stable across Tasks 2 and 5; `STOP_FILE`/`LOG_FILE`/`configure_logging` names consistent across tasks.
- **Test-argv hazard:** `TestMainSingleInstance` is retargeted from `main()` to `_run_daemon()` in Task 4 (because `main()` now parses `sys.argv`), then rewritten as `TestRunDaemonLifecycle` in Task 5 for the return-instead-of-exit behavior.
