# Phase C: Project Health Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add cross-OS CI, declare Windows-only support with env-based configuration, make the exit condition single + debounced, single-source the version, and align README/CLAUDE.md with the implementation (incl. an Invariants & Known Traps section).

**Architecture:** No new runtime modules. `main.py` gains an `_env_int` helper and `_claude_process_name()` so all tunables come from `CCDP_*` environment variables (no source editing); `is_claude_running` detection is tightened from a loose substring match to a line-prefix match; the daemon loop exits only after `EXIT_CONFIRM_COUNT` consecutive absences. A GitHub Actions matrix runs the whole suite (incl. the lock integration test) on Windows/Ubuntu/macOS. Docs are corrected to match.

**Tech Stack:** Python ≥3.10 stdlib (`os`, `subprocess`, `argparse`, `logging`), `pypresence` (unchanged), GitHub Actions, `pytest`.

## Global Constraints

- Python `>=3.10`; sole runtime dependency `pypresence>=4.0`; no new runtime dependencies.
- Platform policy: **Windows is Supported/Tested; macOS/Linux are Experimental/Unverified.** Do NOT build a POSIX platform layer (out of scope). POSIX process detection stays as-is, commented experimental.
- Configuration is via `CCDP_*` environment variables (`CCDP_POLL_INTERVAL_SEC`, `CCDP_IDLE_TIMEOUT_SEC`, `CCDP_EXIT_CONFIRM_COUNT`, `CCDP_CLAUDE_PROCESS_NAME`). No source-editing setup path anywhere in docs. `CLIENT_ID` stays a constant.
- Single exit condition: the Claude process being absent for `EXIT_CONFIRM_COUNT` consecutive polls. Idle only clears presence; it does not exit.
- Version is single-sourced from `claudecode_discord_presence.__version__` via `pyproject.toml` `dynamic = ["version"]`.
- The rotating log stays daemon-only (Phase B invariant — do not add logging to CLI/hook).
- Default values (verbatim): `POLL_INTERVAL_SEC=15`, `STOP_POLL_SEC=2`, `IDLE_TIMEOUT_SEC=600`, `EXIT_CONFIRM_COUNT=3`, `SUBPROCESS_TIMEOUT_SEC=10`, `CLIENT_ID="1488214388920815667"`.

---

## File Structure

- Modify: `claudecode_discord_presence/main.py` — `_env_int`, env constants, `_claude_process_name()`, hardened `is_claude_running`, debounced `_run_daemon`.
- Modify: `pyproject.toml` — dynamic version.
- Create: `.github/workflows/ci.yml` — 3-OS test matrix.
- Modify: `tests/test_main.py` — deterministic boundary test; env/detection/debounce tests.
- Create: `tests/test_ci_workflow.py` — guards the CI workflow file's shape.
- Modify: `README.md`, `CLAUDE.md` — docs alignment.

---

## Task 1: Make the flaky boundary test deterministic

**Files:**
- Modify: `tests/test_main.py` (`TestIsSessionActive::test_exact_boundary_timeout`)

**Interfaces:**
- Consumes: `is_session_active` (existing). Produces: nothing.

- [ ] **Step 1: Replace the flaky test with a deterministic one**

In `tests/test_main.py`, replace the existing `test_exact_boundary_timeout` method with:

```python
    def test_exact_boundary_timeout(self, tmp_path: Path, monkeypatch):
        """At the exact boundary (age == timeout_sec), the session is NOT active.

        The clock and the file mtime are both pinned so the result cannot flip
        on sub-millisecond skew between time.time() and the filesystem (the old
        Windows flake).
        """
        from claudecode_discord_presence import main as m
        f = tmp_path / "session.jsonl"
        f.write_text("{}")
        os.utime(f, (1000.0, 1000.0))
        monkeypatch.setattr(m.time, "time", lambda: 1100.0)  # age == 100s exactly
        assert m.is_session_active(tmp_path, 100) is False  # age not < timeout
        assert m.is_session_active(tmp_path, 101) is True   # age < timeout
```

- [ ] **Step 2: Run it repeatedly to confirm determinism**

Run: `python -m pytest "tests/test_main.py::TestIsSessionActive::test_exact_boundary_timeout" -v` five times (`for /l %i in (1,1,5) do ...` or just run the line 5×).
Expected: PASS every time (previously ~1 in 5 failed).

- [ ] **Step 3: Run the session-active test class**

Run: `python -m pytest tests/test_main.py::TestIsSessionActive -v`
Expected: PASS (all).

- [ ] **Step 4: Commit**

```bash
git add tests/test_main.py
git commit -m "test: make boundary timeout test deterministic (pin clock and mtime)"
```

---

## Task 2: Env-based configuration and hardened process detection

**Files:**
- Modify: `claudecode_discord_presence/main.py`
- Test: `tests/test_main.py`

**Interfaces:**
- Consumes: nothing new.
- Produces:
  - `_env_int(name: str, default: int) -> int` — positive int from env `name`, else `default` (unset/non-int/≤0 → default).
  - Module constants `POLL_INTERVAL_SEC`, `IDLE_TIMEOUT_SEC`, `EXIT_CONFIRM_COUNT` derived via `_env_int`.
  - `_claude_process_name() -> str` — `CCDP_CLAUDE_PROCESS_NAME` if set, else `claude.exe`/`claude` by platform.
  - `is_claude_running() -> bool` — win32 match is now line-prefix based (no false positives from substrings/headers).

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_main.py` a new class (and extend detection tests). Append:

```python
class TestEnvInt:
    def test_unset_returns_default(self, monkeypatch):
        from claudecode_discord_presence import main as m
        monkeypatch.delenv("CCDP_TESTVAL", raising=False)
        assert m._env_int("CCDP_TESTVAL", 42) == 42

    def test_valid_int(self, monkeypatch):
        from claudecode_discord_presence import main as m
        monkeypatch.setenv("CCDP_TESTVAL", "7")
        assert m._env_int("CCDP_TESTVAL", 42) == 7

    def test_non_integer_returns_default(self, monkeypatch):
        from claudecode_discord_presence import main as m
        monkeypatch.setenv("CCDP_TESTVAL", "not-a-number")
        assert m._env_int("CCDP_TESTVAL", 42) == 42

    def test_non_positive_returns_default(self, monkeypatch):
        from claudecode_discord_presence import main as m
        monkeypatch.setenv("CCDP_TESTVAL", "0")
        assert m._env_int("CCDP_TESTVAL", 42) == 42
        monkeypatch.setenv("CCDP_TESTVAL", "-3")
        assert m._env_int("CCDP_TESTVAL", 42) == 42


class TestClaudeProcessName:
    def test_default_by_platform(self, monkeypatch):
        from claudecode_discord_presence import main as m
        monkeypatch.delenv("CCDP_CLAUDE_PROCESS_NAME", raising=False)
        expected = "claude.exe" if sys.platform == "win32" else "claude"
        assert m._claude_process_name() == expected

    def test_env_override(self, monkeypatch):
        from claudecode_discord_presence import main as m
        monkeypatch.setenv("CCDP_CLAUDE_PROCESS_NAME", "custom-proc")
        assert m._claude_process_name() == "custom-proc"
```

Then REPLACE the three existing tests in `TestIsClaudeRunning` (`test_tasklist_oserror_returns_false`, `test_tasklist_empty_stdout`, `test_tasklist_info_message_no_match`) with this set (updates them to the new resolver + hardened matching, using one consistent mocking idiom):

```python
    @patch("claudecode_discord_presence.main.subprocess.run")
    def test_tasklist_oserror_returns_false(self, mock_run, monkeypatch):
        """An OSError from tasklist yields False, not a raise."""
        monkeypatch.setattr("claudecode_discord_presence.main.sys.platform", "win32")
        mock_run.side_effect = OSError("command not found")
        assert is_claude_running() is False
        mock_run.assert_called_once()

    @patch("claudecode_discord_presence.main.subprocess.run")
    def test_tasklist_empty_stdout(self, mock_run, monkeypatch):
        """Empty tasklist output returns False."""
        monkeypatch.setattr("claudecode_discord_presence.main.sys.platform", "win32")
        monkeypatch.setenv("CCDP_CLAUDE_PROCESS_NAME", "claude.exe")
        mock_run.return_value = MagicMock(stdout="", returncode=0)
        assert is_claude_running() is False

    @patch("claudecode_discord_presence.main.subprocess.run")
    def test_tasklist_info_message_no_match(self, mock_run, monkeypatch):
        """The 'INFO: No tasks' message must not count as a match."""
        monkeypatch.setattr("claudecode_discord_presence.main.sys.platform", "win32")
        monkeypatch.setenv("CCDP_CLAUDE_PROCESS_NAME", "claude.exe")
        mock_run.return_value = MagicMock(
            stdout="INFO: No tasks are running which match the specified criteria.",
            returncode=0,
        )
        assert is_claude_running() is False

    @patch("claudecode_discord_presence.main.subprocess.run")
    def test_tasklist_matches_process_row(self, mock_run, monkeypatch):
        """A real tasklist row starting with the image name returns True."""
        monkeypatch.setattr("claudecode_discord_presence.main.sys.platform", "win32")
        monkeypatch.setenv("CCDP_CLAUDE_PROCESS_NAME", "claude.exe")
        mock_run.return_value = MagicMock(
            stdout="claude.exe                    1234 Console                1     50,000 K",
            returncode=0,
        )
        assert is_claude_running() is True

    @patch("claudecode_discord_presence.main.subprocess.run")
    def test_tasklist_substring_is_not_a_match(self, mock_run, monkeypatch):
        """A line merely CONTAINING the name (not starting with it) is not a match."""
        monkeypatch.setattr("claudecode_discord_presence.main.sys.platform", "win32")
        monkeypatch.setenv("CCDP_CLAUDE_PROCESS_NAME", "claude.exe")
        mock_run.return_value = MagicMock(
            stdout="some-wrapper-for-claude.exe    9999 Console                1     10,000 K",
            returncode=0,
        )
        assert is_claude_running() is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_main.py::TestEnvInt tests/test_main.py::TestClaudeProcessName -v`
Expected: FAIL — `AttributeError: module ... has no attribute '_env_int'` / `_claude_process_name`.

- [ ] **Step 3: Implement in `main.py`**

Add `_env_int` immediately after the imports/logger (above the constants block), then change the constants and add `_claude_process_name`. Replace the current constants block (`main.py:21-26`) and add the helper so this region reads:

```python
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
```

Then replace `is_claude_running` (`main.py:72-` through the end of the function) with:

```python
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
```

Note: the old module-level `CLAUDE_PROCESS_NAME` constant is removed — `_claude_process_name()` replaces it. Grep to confirm no remaining reference: `grep -n "CLAUDE_PROCESS_NAME" claudecode_discord_presence/` should show only `CCDP_CLAUDE_PROCESS_NAME` and `_claude_process_name`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_main.py -v`
Expected: PASS (TestEnvInt, TestClaudeProcessName, and the five TestIsClaudeRunning tests green).

- [ ] **Step 5: Commit**

```bash
git add claudecode_discord_presence/main.py tests/test_main.py
git commit -m "feat: env-configurable settings and hardened process detection"
```

---

## Task 3: Single debounced exit condition

**Files:**
- Modify: `claudecode_discord_presence/main.py` (`_run_daemon` loop)
- Test: `tests/test_main.py`

**Interfaces:**
- Consumes: `EXIT_CONFIRM_COUNT`, `is_claude_running`, `_stop_requested`, `_sleep_until_poll`, `_reconcile_presence` (existing).
- Produces: `_run_daemon` exits only after `EXIT_CONFIRM_COUNT` consecutive `is_claude_running()==False` polls.

- [ ] **Step 1: Write the failing tests**

Add to `TestRunDaemonLifecycle` in `tests/test_main.py`:

```python
    def test_exits_after_consecutive_absences(self, monkeypatch):
        from claudecode_discord_presence import main as m
        fake_lock = MagicMock()
        fake_lock.acquire.return_value = True
        monkeypatch.setattr(m, "InstanceLock", lambda path: fake_lock)
        monkeypatch.setattr(m, "configure_logging", lambda: m.logger)
        monkeypatch.setattr(m, "_stop_requested", lambda: False)
        monkeypatch.setattr(m, "_sleep_until_poll", lambda: False)
        monkeypatch.setattr(m, "_clear_own_stop_sentinel", lambda: None)
        monkeypatch.setattr(m, "_reconcile_presence", lambda a, p, r: (r, p))
        monkeypatch.setattr(m, "EXIT_CONFIRM_COUNT", 2)
        # Absent on every poll: must exit after exactly 2 checks.
        gone = MagicMock(side_effect=[False, False])
        monkeypatch.setattr(m, "is_claude_running", gone)
        m._run_daemon()
        assert gone.call_count == 2
        fake_lock.release.assert_called_once()

    def test_transient_absence_does_not_exit(self, monkeypatch):
        from claudecode_discord_presence import main as m
        fake_lock = MagicMock()
        fake_lock.acquire.return_value = True
        monkeypatch.setattr(m, "InstanceLock", lambda path: fake_lock)
        monkeypatch.setattr(m, "configure_logging", lambda: m.logger)
        monkeypatch.setattr(m, "_stop_requested", lambda: False)
        monkeypatch.setattr(m, "_sleep_until_poll", lambda: False)
        monkeypatch.setattr(m, "_clear_own_stop_sentinel", lambda: None)
        monkeypatch.setattr(m, "_reconcile_presence", lambda a, p, r: (r, p))
        monkeypatch.setattr(m, "EXIT_CONFIRM_COUNT", 2)
        # False, then True (resets), then two consecutive False -> exits on the 4th check.
        seq = MagicMock(side_effect=[False, True, False, False])
        monkeypatch.setattr(m, "is_claude_running", seq)
        m._run_daemon()
        assert seq.call_count == 4
        fake_lock.release.assert_called_once()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_main.py::TestRunDaemonLifecycle::test_exits_after_consecutive_absences tests/test_main.py::TestRunDaemonLifecycle::test_transient_absence_does_not_exit -v`
Expected: FAIL — current loop exits on the FIRST absence, so `test_transient_absence_does_not_exit` fails (`call_count` is 1, and/or `StopIteration` is avoided but the resets aren't honored).

- [ ] **Step 3: Implement the debounce in `_run_daemon`**

In `_run_daemon`, add `missed = 0` just before the `try:` (next to `exit_reason = "unknown"`), and replace the `if not is_claude_running(): ... break` block with the counter. The loop body becomes:

```python
    missed = 0
    try:
        while True:
            if _stop_requested():
                exit_reason = "stop requested"
                break
            if is_claude_running():
                missed = 0
            else:
                missed += 1
                if missed >= EXIT_CONFIRM_COUNT:
                    exit_reason = "claude gone"
                    break
            active = is_session_active(projects_dir, IDLE_TIMEOUT_SEC)
            rpc, presence_active = _reconcile_presence(active, presence_active, rpc)
            if _sleep_until_poll():
                exit_reason = "stop requested"
                break
```

(Everything else in `_run_daemon` — logging, `except`/`finally` — is unchanged.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_main.py::TestRunDaemonLifecycle -v`
Expected: PASS (both new tests plus the existing lifecycle tests).

- [ ] **Step 5: Commit**

```bash
git add claudecode_discord_presence/main.py tests/test_main.py
git commit -m "feat: exit only after EXIT_CONFIRM_COUNT consecutive absences"
```

---

## Task 4: Single-source the version

**Files:**
- Modify: `pyproject.toml`
- Test: `tests/test_version.py` (new)

**Interfaces:**
- Consumes: `claudecode_discord_presence.__version__`. Produces: nothing.

- [ ] **Step 1: Write the failing test**

Create `tests/test_version.py`:

```python
import importlib.metadata

from claudecode_discord_presence import __version__


def test_metadata_version_matches_dunder():
    """Installed package metadata must match __init__.__version__ (single source)."""
    assert importlib.metadata.version("claudecode-discord-presence") == __version__
```

- [ ] **Step 2: Edit `pyproject.toml` to use a dynamic version**

In `pyproject.toml`, under `[project]` remove the line `version = "0.1.0"` and add `dynamic = ["version"]`. Then add a new section:

```toml
[tool.setuptools.dynamic]
version = {attr = "claudecode_discord_presence.__version__"}
```

The `[project]` table's version line becomes:

```toml
dynamic = ["version"]
```

- [ ] **Step 3: Reinstall so metadata reflects the dynamic version, then run the test**

Run: `pip install -e . -q`
Expected: succeeds with no error (proves the `dynamic`/`[tool.setuptools.dynamic]` config is valid — a malformed dynamic version fails the build here).

Run: `python -m pytest tests/test_version.py -v`
Expected: PASS (metadata `0.1.0` == `__version__` `0.1.0`).

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml tests/test_version.py
git commit -m "build: single-source version via dynamic metadata"
```

---

## Task 5: CI workflow (3-OS matrix)

**Files:**
- Create: `.github/workflows/ci.yml`
- Test: `tests/test_ci_workflow.py` (new)

**Interfaces:**
- Consumes: nothing. Produces: nothing (infra).

- [ ] **Step 1: Write the failing guard test**

Create `tests/test_ci_workflow.py`:

```python
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CI = REPO_ROOT / ".github" / "workflows" / "ci.yml"


def test_ci_workflow_exists():
    assert CI.is_file()


def test_ci_covers_three_os_and_runs_pytest():
    text = CI.read_text(encoding="utf-8")
    for os_name in ("windows-latest", "ubuntu-latest", "macos-latest"):
        assert os_name in text, f"CI matrix missing {os_name}"
    assert "pytest" in text
    assert "3.10" in text
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m pytest tests/test_ci_workflow.py -v`
Expected: FAIL — `test_ci_workflow_exists` fails (file does not exist yet).

- [ ] **Step 3: Create the workflow**

Create `.github/workflows/ci.yml`:

```yaml
name: CI

on:
  push:
  pull_request:

jobs:
  test:
    strategy:
      fail-fast: false
      matrix:
        os: [windows-latest, ubuntu-latest, macos-latest]
    runs-on: ${{ matrix.os }}
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.10"
      - name: Install package
        run: pip install -e .
      - name: Install pytest
        run: pip install pytest
      - name: Run tests
        run: python -m pytest -v
```

- [ ] **Step 4: Run the guard test to verify it passes**

Run: `python -m pytest tests/test_ci_workflow.py -v`
Expected: PASS (2 tests). (The workflow itself is validated for real when it runs on push.)

- [ ] **Step 5: Commit**

```bash
git add .github/workflows/ci.yml tests/test_ci_workflow.py
git commit -m "ci: add Windows/Ubuntu/macOS test matrix"
```

---

## Task 6: Align README.md with the implementation

**Files:**
- Modify: `README.md`

**Interfaces:** docs only — no code.

- [ ] **Step 1: Add a CI badge under the title**

Replace:

```markdown
# claudecode-discord-presence

Show your Claude Code session as Discord Rich Presence status.
```

with:

```markdown
# claudecode-discord-presence

![CI](https://github.com/Idios/claudecode-discord-presence/actions/workflows/ci.yml/badge.svg)

Show your Claude Code session as Discord Rich Presence status.
```

- [ ] **Step 2: Fix the idle/exit description (intro + How It Works)**

Replace the intro sentence:

```markdown
When Claude Code is actively running, your Discord profile displays "Playing 🦀ClaudeCode🦀". The presence is automatically cleared and the process exits when the session goes idle.
```

with:

```markdown
When Claude Code is actively running, your Discord profile displays "Playing 🦀ClaudeCode🦀". While a session is idle the presence is cleared, and the background process exits automatically once Claude Code itself is no longer running.
```

Replace the How It Works code block:

```
Claude Code starts
  → SessionStart hook → launches background process
    → polls .jsonl files → Discord RPC → "Playing 🦀ClaudeCode🦀"

Claude Code stops
  → .jsonl files stop updating → 10 min idle → process exits automatically
```

with:

```
Claude Code starts
  → SessionStart hook → launches background process
    → polls .jsonl files → Discord RPC → "Playing 🦀ClaudeCode🦀"

Session idle (no .jsonl updates for a while)
  → presence cleared (process keeps monitoring)

Claude Code process gone (confirmed over several polls)
  → process exits automatically
```

- [ ] **Step 3: Fix the hook registration (schema + console script)**

Replace the step-3 JSON block:

```json
   {
     "hooks": {
       "SessionStart": [
         {
           "type": "command",
           "command": "python -m claudecode_discord_presence.hook"
         }
       ]
     }
   }
```

with:

```json
   {
     "hooks": {
       "SessionStart": [
         {
           "hooks": [
             {
               "type": "command",
               "command": "claudecode-discord-presence-hook"
             }
           ]
         }
       ]
     }
   }
```

And replace step 4:

```markdown
4. **Restart Claude Code**. The tool will automatically start in the background when a session begins, and exit when idle for 10 minutes.
```

with:

```markdown
4. **Restart Claude Code**. The tool starts automatically in the background when a session begins, and exits automatically once Claude Code is no longer running.
```

- [ ] **Step 4: Replace the Configuration section (env vars, not source editing)**

Replace the entire Configuration section:

```markdown
## Configuration

Edit constants in `claudecode_discord_presence/main.py`:

| Constant | Default | Description |
|----------|---------|-------------|
| `POLL_INTERVAL_SEC` | `60` | How often to check for session activity (seconds) |
| `IDLE_TIMEOUT_SEC` | `600` | Time without updates before auto-exit (seconds) |
| `CLIENT_ID` | `1488214388920815667` | Discord Application ID |
```

with:

```markdown
## Configuration

Set environment variables before the tool starts (no source editing required). Invalid values fall back to the default.

| Environment variable | Default | Description |
|----------------------|---------|-------------|
| `CCDP_POLL_INTERVAL_SEC` | `15` | How often to check for session activity (seconds) |
| `CCDP_IDLE_TIMEOUT_SEC` | `600` | Idle time (no `.jsonl` updates) before the presence is cleared (seconds) |
| `CCDP_EXIT_CONFIRM_COUNT` | `3` | Consecutive polls with Claude Code absent before the process exits |
| `CCDP_CLAUDE_PROCESS_NAME` | `claude.exe` (Windows) | Process name to detect (override if yours differs) |

You can also inspect and control a running instance:

```bash
claudecode-discord-presence --status   # report whether the daemon is running
claudecode-discord-presence --stop      # ask a running daemon to stop
```
```

- [ ] **Step 5: Replace the Platform Support section (Windows-only)**

Replace the entire Platform Support section (from `## Platform Support` through the end of "Troubleshooting process detection", i.e. up to but not including `## License`) with:

```markdown
## Platform Support

This tool is **built and tested for Windows** with the Claude Code CLI and the Discord desktop app running locally.

| Platform | Status | Process detection | Notes |
|----------|--------|-------------------|-------|
| Windows (x64) | **Supported / Tested** | `tasklist` for `claude.exe` | Requires Git Bash (included with Git for Windows) for hooks |
| macOS / Linux | **Experimental / Unverified** | `pgrep` for `claude` | The test suite runs in CI on these OSes, but Discord integration and process detection are unverified. Claude Code may run as a `node` process; override the name with `CCDP_CLAUDE_PROCESS_NAME`. |

### Not supported

- **Claude Code Web (claude.ai/code)** — no local process to detect
- **IDE extensions (VS Code, JetBrains)** — process name differs from standalone CLI
- **Discord browser/mobile** — Rich Presence requires the Discord desktop app

### Troubleshooting process detection

If the tool exits, the process name may differ on your platform. Check with:

```bash
# Windows
tasklist | findstr -i claude

# macOS/Linux
ps aux | grep -i claude
```

If the process name is different, set `CCDP_CLAUDE_PROCESS_NAME` to the correct name (do not edit the source).
```

- [ ] **Step 6: Fix the Uninstallation PID note**

Replace the step-4 block of Uninstallation:

```markdown
4. **Remove the PID file** (if it exists):
   ```bash
   # macOS/Linux
   rm -f ~/.claude/claudecode-discord-presence.pid

   # Windows (PowerShell)
   Remove-Item -Force ~/.claude/claudecode-discord-presence.pid
   ```
```

with:

```markdown
4. **Leftover files** (`~/.claude/claudecode-discord-presence.pid`, `.pid.lock`, `.stop`) are normally removed automatically when the process exits. If any remain after an abnormal shutdown, they are harmless and can be deleted manually.
```

- [ ] **Step 7: Verify no source-editing guidance remains, then commit**

Run: `grep -n "CLAUDE_PROCESS_NAME in\|Edit constants\|10 min idle\|10 minutes" README.md`
Expected: no matches (all source-edit / stale-idle guidance removed). `CCDP_CLAUDE_PROCESS_NAME` references are fine.

```bash
git add README.md
git commit -m "docs: align README with implementation (Windows-only, env config, hook schema)"
```

---

## Task 7: Align CLAUDE.md and add Invariants & Known Traps

**Files:**
- Modify: `CLAUDE.md`

**Interfaces:** docs only — no code.

- [ ] **Step 1: Update Supported Platforms**

Replace:

```markdown
## Supported Platforms

- **Windows (x64) + Claude Code CLI + Discord desktop app** — tested and supported.
- **macOS / Linux** — untested. Process detection uses `pgrep -x claude`; the process name may differ.
- **NOT supported**: Claude Code Web (claude.ai/code), IDE extensions (VS Code, JetBrains), Discord browser/mobile.
```

with:

```markdown
## Supported Platforms

- **Windows (x64) + Claude Code CLI + Discord desktop app** — supported and tested (incl. CI).
- **macOS / Linux** — experimental/unverified. The test suite runs in CI on these OSes, but Discord integration and process detection are not verified. Claude Code may run as a `node` process, so `pgrep -x claude` can fail; override with the `CCDP_CLAUDE_PROCESS_NAME` environment variable.
- **NOT supported**: Claude Code Web (claude.ai/code), IDE extensions (VS Code, JetBrains), Discord browser/mobile.
```

- [ ] **Step 2: Fix the source-edit lines in setup/uninstall**

Replace the setup bullet:

```markdown
- If the tool exits immediately on macOS/Linux, the Claude Code process name may differ. Ask the user to run `ps aux | grep -i claude` and update `CLAUDE_PROCESS_NAME` in `main.py` accordingly.
```

with:

```markdown
- If the tool exits immediately on macOS/Linux, the Claude Code process name may differ. Ask the user to run `ps aux | grep -i claude` and set the `CCDP_CLAUDE_PROCESS_NAME` environment variable accordingly (do not edit the source).
```

Replace the uninstall step 4:

```markdown
4. Remove `~/.claude/claudecode-discord-presence.pid` if it exists
```

with:

```markdown
4. Leftover files (`~/.claude/claudecode-discord-presence.pid`, `.pid.lock`, `.stop`) are removed automatically on normal exit; delete any residue manually if needed
```

- [ ] **Step 3: Update Project Structure and Key Design Decisions**

Replace the Project Structure block:

```
claudecode_discord_presence/
  __init__.py    # Version
  main.py        # Polling loop, Discord RPC, PID management, auto-exit
  hook.py        # Hook entry point — launches main.py in background
tests/
  test_main.py   # Unit tests for session detection and PID logic
```

with:

```
claudecode_discord_presence/
  __init__.py         # Version (single source; pyproject reads it dynamically)
  main.py             # CLI dispatch, daemon loop, Discord RPC, --status/--stop
  hook.py             # SessionStart hook entry — launches the daemon in background
  single_instance.py  # Atomic OS lock (InstanceLock), PID_FILE / STOP_FILE
  logsetup.py         # Daemon-only rotating file logging
tests/                # Unit + integration tests (incl. N-hooks -> 1-process)
```

Replace the entire Key Design Decisions section:

```markdown
## Key Design Decisions

- **SessionStart hook** launches the process; **idle detection** stops it. No SessionEnd hook is used because Claude Code may not fire it on abnormal exit.
- **PID file** (`~/.claude/claudecode-discord-presence.pid`) prevents duplicate instances.
- **No HTTP daemon** — uses file polling only for simplicity.
- **pypresence** is the sole dependency for Discord RPC.
- Session activity is detected by checking `.jsonl` file modification times.
- Idle timeout is 10 minutes; polling interval is 1 minute.
```

with:

```markdown
## Key Design Decisions

- **SessionStart hook** launches the daemon; the daemon exits on its own. No SessionEnd hook is used because Claude Code may not fire it on abnormal exit.
- **No HTTP daemon** — file polling only, for simplicity.
- **pypresence** is the sole runtime dependency (Discord RPC).
- Session activity is detected by `.jsonl` file modification times.
- Poll interval is 15s; idle timeout is 10 minutes (clears presence only). All tunables are `CCDP_*` env vars.
- Exit is a single condition: the Claude Code process absent for `EXIT_CONFIRM_COUNT` (default 3) consecutive polls.

## Invariants and Known Traps

Read this before changing lifecycle, locking, or process detection — these guardrails exist because a prior version accumulated 33 orphaned processes.

- **Single-instance is guaranteed by an atomic OS lock** (`InstanceLock` in `single_instance.py`), held for the process lifetime — NOT by the PID file's text content. Never reintroduce a "check then write PID" pattern (it is a TOCTOU race).
- **Never use `os.kill(pid, 0)` on Windows** — Python's `os.kill` there calls `TerminateProcess`, which *kills* the target. There is no liveness polling; the OS lock handles single-instance, and it is released automatically on any exit.
- **Keep the `N hooks -> exactly 1 process` integration test green** (`tests/test_single_instance_integration.py`). It is the regression anchor for the original bug.
- **Logging is daemon-only (single writer).** `configure_logging()` is called only by the daemon; the hook and `--status`/`--stop` print to stderr/stdout. Do not attach the rotating file handler from a second process (Windows rotation corruption).
- **The STOP sentinel is PID-matched**: `--stop` writes the running daemon's PID; the daemon acts only on its own PID and deletes stale/foreign sentinels.
- **Exit is one debounced condition** (Claude absent for N consecutive polls). Do not add ad-hoc exit paths; a transient process-check failure must not kill a live session.
- **Process detection must be exact** (a tasklist row that *starts with* the image name), not a loose substring — and the name comes from `CCDP_CLAUDE_PROCESS_NAME` / platform default, never a hardcoded edit.
```

- [ ] **Step 4: Verify and commit**

Run: `grep -n "PID file.*prevents\|update .CLAUDE_PROCESS_NAME. in\|Idle timeout is 10 minutes; polling interval is 1 minute" CLAUDE.md`
Expected: no matches (stale guarantee/source-edit/lifecycle lines gone).

Run: `python -m pytest -q`
Expected: PASS (full suite — this is the last task; everything green, incl. the new CI guard and version tests).

```bash
git add CLAUDE.md
git commit -m "docs: align CLAUDE.md; add Invariants and Known Traps section"
```

---

## Self-Review Notes

- **Spec coverage:** CI §1 → Task 5 (+ flaky prereq Task 1); platform policy §2 → Task 2 (env + detection) & docs Tasks 6/7; lifecycle §3 → Task 3; packaging §4 → Task 4 (version) & Task 6 (hook schema/console-script); docs §5 → Tasks 6 (README) & 7 (CLAUDE.md incl. Invariants). All spec sections mapped.
- **Ordering:** flaky fix (1) before CI (5) so the Windows job is green; `EXIT_CONFIRM_COUNT` defined in Task 2, consumed in Task 3; docs (6,7) after the code they describe.
- **Type/name consistency:** `_env_int(name, default) -> int`, `_claude_process_name() -> str`, `EXIT_CONFIRM_COUNT`, `CCDP_*` names identical across tasks and docs. `CLAUDE_PROCESS_NAME` module constant is removed in Task 2 and every consumer updated in the same task.
- **Env-at-import caveat:** `POLL_INTERVAL_SEC`/`IDLE_TIMEOUT_SEC`/`EXIT_CONFIRM_COUNT` are resolved from env at import; `_env_int` is unit-tested directly, and Task 3 tests monkeypatch `EXIT_CONFIRM_COUNT` on the module. `_claude_process_name()` reads env per-call so detection tests use `monkeypatch.setenv` with no reload.
- **Docs completeness:** every docs step gives exact old→new text, and Tasks 6/7 end with a grep that proves the stale guidance (source-edit, "10 min idle exit", "PID file prevents duplicates") is gone.
