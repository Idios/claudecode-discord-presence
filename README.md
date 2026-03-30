# claudecode-discord-presence

Show your Claude Code session as Discord Rich Presence status.

When Claude Code is actively running, your Discord profile displays "Playing 🦀ClaudeCode🦀". The presence is automatically cleared and the process exits when the session goes idle.

## How It Works

Claude Code writes session logs to `~/.claude/projects/` as `.jsonl` files. This tool polls those files and shows Discord Rich Presence when any file has been updated within the last 10 minutes.

```
Claude Code starts
  → SessionStart hook → launches background process
    → polls .jsonl files → Discord RPC → "Playing 🦀ClaudeCode🦀"

Claude Code stops
  → .jsonl files stop updating → 10 min idle → process exits automatically
```

No HTTP server, no daemon manager — just a hook, a polling loop, and auto-exit.

## Requirements

- Python 3.10+
- [Discord](https://discord.com) desktop app running
- [Claude Code](https://claude.ai/code) installed

## Setup

### Quick Setup (for Claude Code users)

If you are reading this in a Claude Code session, follow these steps to set up the tool. **Ask the user for confirmation before running each step.**

1. **Clone the repository** to a location outside the current project:
   ```bash
   git clone https://github.com/Idios/claudecode-discord-presence.git ~/claudecode-discord-presence
   ```

2. **Install the package:**
   ```bash
   pip install -e ~/claudecode-discord-presence
   ```

3. **Register the Claude Code hook** by adding the following to `~/.claude/settings.json`. If the file already exists, merge the `hooks` section into the existing configuration:
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

4. **Restart Claude Code**. The tool will automatically start in the background when a session begins, and exit when idle for 10 minutes.

5. **Verify**: The user should see "Playing 🦀ClaudeCode🦀" on their Discord profile while a Claude Code session is active.

### Manual Installation

```bash
git clone https://github.com/Idios/claudecode-discord-presence.git
cd claudecode-discord-presence
pip install -e .
```

Then register the hook as described in step 3 above, or run manually:

```bash
python -m claudecode_discord_presence.main
```

## Configuration

Edit constants in `claudecode_discord_presence/main.py`:

| Constant | Default | Description |
|----------|---------|-------------|
| `POLL_INTERVAL_SEC` | `60` | How often to check for session activity (seconds) |
| `IDLE_TIMEOUT_SEC` | `600` | Time without updates before auto-exit (seconds) |
| `CLIENT_ID` | `1488214388920815667` | Discord Application ID |

## Running Tests

```bash
pip install pytest
python -m pytest -v
```

## Uninstallation

1. **Remove the hook** from `~/.claude/settings.json`: delete the `SessionStart` entry containing `claudecode_discord_presence.hook`.

2. **Uninstall the package:**
   ```bash
   pip uninstall claudecode-discord-presence
   ```

3. **Remove the cloned repository:**
   ```bash
   # macOS/Linux
   rm -rf ~/claudecode-discord-presence

   # Windows (PowerShell)
   Remove-Item -Recurse -Force ~/claudecode-discord-presence
   ```

4. **Remove the PID file** (if it exists):
   ```bash
   # macOS/Linux
   rm -f ~/.claude/claudecode-discord-presence.pid

   # Windows (PowerShell)
   Remove-Item -Force ~/.claude/claudecode-discord-presence.pid
   ```

## Platform Support

This tool is designed for the **Claude Code desktop app (CLI)** running locally with the **Discord desktop app**.

| Platform | Status | Process detection | Notes |
|----------|--------|-------------------|-------|
| Windows (x64) | Tested | `tasklist` for `claude.exe` | Requires Git Bash (included with Git for Windows) for hooks |
| macOS | Untested | `pgrep` for `claude` | Process name may differ — verify with `pgrep -x claude` |
| Linux | Untested | `pgrep` for `claude` | Process name may differ — verify with `pgrep -x claude` |

### Not supported

- **Claude Code Web (claude.ai/code)** — no local process to detect
- **IDE extensions (VS Code, JetBrains)** — process name differs from standalone CLI
- **Discord browser/mobile** — Rich Presence requires the Discord desktop app

### Troubleshooting process detection

If the tool exits immediately, the process name may differ on your platform. Check with:

```bash
# Windows
tasklist | findstr -i claude

# macOS/Linux
ps aux | grep -i claude
```

If the process name is different, update `CLAUDE_PROCESS_NAME` in `claudecode_discord_presence/main.py`.

## License

MIT
