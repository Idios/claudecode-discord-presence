# CLAUDE.md — Instructions for Claude Code Agents

This file provides context for Claude Code agents working with this repository.

## What This Tool Does

claudecode-discord-presence is a lightweight Python tool that displays "Playing 🦀ClaudeCode🦀" on the user's Discord profile while a Claude Code session is active. It monitors `~/.claude/projects/` for recently updated `.jsonl` files and connects to Discord via local RPC. It auto-starts via a Claude Code SessionStart hook and auto-exits once no monitored tool is running. It also monitors **Codex** and **Zed** by default (all sharing the built-in client ID).

## Supported Platforms

- **Windows (x64) + Claude Code CLI + Discord desktop app** — supported and tested (incl. CI).
- **macOS / Linux** — experimental/unverified. The test suite runs in CI on these OSes, but Discord integration and process detection are not verified. Claude Code may run as a `node` process, so `pgrep -x claude` can fail; override with the `CCDP_CLAUDE_PROCESS_NAME` environment variable.
- **NOT supported**: Claude Code Web (claude.ai/code), IDE extensions (VS Code, JetBrains), Discord browser/mobile.

## If the User Wants to Set Up This Tool

When a user shares this repository URL and asks you to set it up, follow the steps in the "Quick Setup" section of README.md. Important guidelines:

- **Always ask for user confirmation** before cloning, installing, or modifying settings.
- When editing `~/.claude/settings.json`, **merge** the hooks config into existing settings — do not overwrite the file.
- The Discord desktop app must be running for the tool to work.
- After setup, **restart Claude Code** for the hook to take effect.
- The tool starts automatically on session start, clears presence when idle, and exits once Claude Code itself is no longer running — no manual process management needed.
- If the tool exits immediately on macOS/Linux, the Claude Code process name may differ. Ask the user to run `ps aux | grep -i claude` and set the `CCDP_CLAUDE_PROCESS_NAME` environment variable accordingly (do not edit the source).

## If the User Wants to Uninstall This Tool

Follow the "Uninstallation" section of README.md:
1. Remove the hook entry from `~/.claude/settings.json`
2. `pip uninstall claudecode-discord-presence`
3. Remove the cloned repository directory
4. Leftover files (`~/.claude/claudecode-discord-presence.pid`, `.pid.lock`, `.stop`) are removed automatically on normal exit; delete any residue manually if needed. The log file `~/.claude/claudecode-discord-presence.log` (plus up to 2 rotated backups) is never auto-removed — delete it too if desired

## Project Structure

```
claudecode_discord_presence/
  __init__.py         # Version (single source; pyproject reads it dynamically)
  main.py             # CLI dispatch, daemon loop, Discord RPC, --status/--stop
  hook.py             # SessionStart hook entry — launches the daemon in background
  single_instance.py  # Atomic OS lock (InstanceLock), PID_FILE / STOP_FILE
  logsetup.py         # Daemon-only rotating file logging
tests/                # Unit + integration tests (incl. N-hooks -> 1-process)
```

## Development Commands

```bash
pip install -e .              # Install in editable mode
python -m pytest -v           # Run tests
python -m claudecode_discord_presence.main  # Run directly (foreground)
python -m claudecode_discord_presence.hook  # Simulate hook (launches background process)
```

## Key Design Decisions

- **SessionStart hook** launches the daemon; the daemon exits on its own. No SessionEnd hook is used because Claude Code may not fire it on abnormal exit.
- **No HTTP daemon** — file polling only, for simplicity.
- **pypresence** is the sole runtime dependency (Discord RPC).
- Session activity is detected by `.jsonl` file modification times.
- Poll interval is 15s; idle timeout is 10 minutes (clears presence only). The main tunables (poll interval, idle timeout, exit-confirm count, process name) are `CCDP_*` env vars.
- Exit is a single condition: **no monitored tool's process** present for `EXIT_CONFIRM_COUNT` (default 3) consecutive polls.
- **Multi-tool support**: the daemon monitors Claude Code plus Codex and Zed by default. Each tool has a `key`, `client_id`, `process_name`, and optional `sessions_dir`. All three share the built-in Claude Code client ID unless a `CCDP_<KEY>_CLIENT_ID` overrides it. `resolve_tools()` builds the list, and `resolve_active_tool()` picks the running tool with the most recent activity (file-based activity wins over process-only fallback). The "Playing X" text comes from the Discord app name, so a distinct display text per tool needs a distinct Discord client ID.

## Invariants and Known Traps

Read this before changing lifecycle, locking, or process detection — these guardrails exist because a prior version accumulated 33 orphaned processes.

- **Single-instance is guaranteed by an atomic OS lock** (`InstanceLock` in `single_instance.py`), held for the process lifetime — NOT by the PID file's text content. Never reintroduce a "check then write PID" pattern (it is a TOCTOU race).
- **Never use `os.kill(pid, 0)` on Windows** — Python's `os.kill` there calls `TerminateProcess`, which *kills* the target. There is no liveness polling; the OS lock handles single-instance, and it is released automatically on any exit.
- **Keep the `N hooks -> exactly 1 process` integration test green** (`tests/test_single_instance_integration.py`). It is the regression anchor for the original bug.
- **Logging is daemon-only (single writer).** `configure_logging()` is called only by the daemon; the hook and `--status`/`--stop` print to stderr/stdout. Do not attach the rotating file handler from a second process (Windows rotation corruption).
- **The STOP sentinel is PID-matched**: `--stop` writes the running daemon's PID; the daemon acts only on its own PID and deletes stale/foreign sentinels.
- **Exit is one debounced condition** (no monitored tool present for N consecutive polls). Do not add ad-hoc exit paths; a transient process-check failure must not kill a live session.
- **Process detection must be exact** (a tasklist row that *starts with* the image name), not a loose substring — and the name comes from `CCDP_<KEY>_PROCESS_NAME` / platform default, never a hardcoded edit.
