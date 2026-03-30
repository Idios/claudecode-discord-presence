# CLAUDE.md — Instructions for Claude Code Agents

This file provides context for Claude Code agents working with this repository.

## What This Tool Does

claudecode-discord-presence is a lightweight Python tool that displays "Playing 🦀ClaudeCode🦀" on the user's Discord profile while a Claude Code session is active. It monitors `~/.claude/projects/` for recently updated `.jsonl` files and connects to Discord via local RPC. It auto-starts via a Claude Code SessionStart hook and auto-exits when no session is active.

## Supported Platforms

- **Windows (x64) + Claude Code CLI + Discord desktop app** — tested and supported.
- **macOS / Linux** — untested. Process detection uses `pgrep -x claude`; the process name may differ.
- **NOT supported**: Claude Code Web (claude.ai/code), IDE extensions (VS Code, JetBrains), Discord browser/mobile.

## If the User Wants to Set Up This Tool

When a user shares this repository URL and asks you to set it up, follow the steps in the "Quick Setup" section of README.md. Important guidelines:

- **Always ask for user confirmation** before cloning, installing, or modifying settings.
- When editing `~/.claude/settings.json`, **merge** the hooks config into existing settings — do not overwrite the file.
- The Discord desktop app must be running for the tool to work.
- After setup, **restart Claude Code** for the hook to take effect.
- The tool starts automatically on session start and exits when idle — no manual process management needed.
- If the tool exits immediately on macOS/Linux, the Claude Code process name may differ. Ask the user to run `ps aux | grep -i claude` and update `CLAUDE_PROCESS_NAME` in `main.py` accordingly.

## If the User Wants to Uninstall This Tool

Follow the "Uninstallation" section of README.md:
1. Remove the hook entry from `~/.claude/settings.json`
2. `pip uninstall claudecode-discord-presence`
3. Remove the cloned repository directory
4. Remove `~/.claude/claudecode-discord-presence.pid` if it exists

## Project Structure

```
claudecode_discord_presence/
  __init__.py    # Version
  main.py        # Polling loop, Discord RPC, PID management, auto-exit
  hook.py        # Hook entry point — launches main.py in background
tests/
  test_main.py   # Unit tests for session detection and PID logic
```

## Development Commands

```bash
pip install -e .              # Install in editable mode
python -m pytest -v           # Run tests
python -m claudecode_discord_presence.main  # Run directly (foreground)
python -m claudecode_discord_presence.hook  # Simulate hook (launches background process)
```

## Key Design Decisions

- **SessionStart hook** launches the process; **idle detection** stops it. No SessionEnd hook is used because Claude Code may not fire it on abnormal exit.
- **PID file** (`~/.claude/claudecode-discord-presence.pid`) prevents duplicate instances.
- **No HTTP daemon** — uses file polling only for simplicity.
- **pypresence** is the sole dependency for Discord RPC.
- Session activity is detected by checking `.jsonl` file modification times.
- Idle timeout is 10 minutes; polling interval is 1 minute.
