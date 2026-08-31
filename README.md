# claudecode-discord-presence

![CI](https://github.com/Idios/claudecode-discord-presence/actions/workflows/ci.yml/badge.svg)

Show your Claude Code (or Codex / Zed) session as Discord Rich Presence status.

When any monitored tool (Claude Code, Codex, or Zed) is running, your Discord profile displays "Playing 🦀ClaudeCode🦀". While a session is idle the presence is cleared, and the background process exits automatically once none of the monitored tools is running. Codex and Zed are monitored by default — no client-ID setup required (see "Showing Codex and Zed" below).

## How It Works

Claude Code writes session logs to `~/.claude/projects/` as `.jsonl` files. This tool polls those files and shows Discord Rich Presence when any file has been recently updated.

```
Claude Code starts
  → SessionStart hook → launches background process
    → polls .jsonl files → Discord RPC → "Playing 🦀ClaudeCode🦀"

Session idle (no .jsonl updates for a while)
  → presence cleared (process keeps monitoring)

Claude Code process gone (confirmed over several polls)
  → process exits automatically
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

4. **Restart Claude Code**. The tool starts automatically in the background when a session begins, and exits automatically once Claude Code is no longer running.

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

Set environment variables before the tool starts (no source editing required). Invalid values fall back to the default.

| Environment variable | Default | Description |
|----------------------|---------|-------------|
| `CCDP_POLL_INTERVAL_SEC` | `15` | How often to check for session activity (seconds) |
| `CCDP_IDLE_TIMEOUT_SEC` | `600` | Idle time (no `.jsonl` updates) before the presence is cleared (seconds) |
| `CCDP_EXIT_CONFIRM_COUNT` | `3` | Consecutive polls with no monitored tool running before the process exits |
| `CCDP_CLAUDE_PROCESS_NAME` | `claude.exe` (Windows) | Claude Code process name to detect (override if yours differs) |
| `CCDP_CLAUDE_CLIENT_ID` | built-in ID | Discord client ID for Claude Code |
| `CCDP_CLAUDE_SESSIONS_DIR` | `~/.claude/projects` | Directory polled for Claude Code activity (empty = process-only) |
| `CCDP_CODEX_CLIENT_ID` | built-in ID | Discord client ID for Codex (optional override for a distinct presence) |
| `CCDP_CODEX_PROCESS_NAME` | `codex.exe` / `codex` | Codex process name to detect |
| `CCDP_CODEX_SESSIONS_DIR` | `~/.codex/sessions` | Directory polled for Codex activity (empty = process-only) |
| `CCDP_ZED_CLIENT_ID` | built-in ID | Discord client ID for Zed (optional override for a distinct presence) |
| `CCDP_ZED_PROCESS_NAME` | `Zed.exe` / `zed` | Zed process name to detect |
| `CCDP_ZED_SESSIONS_DIR` | *(unset)* | Directory polled for Zed activity (empty = process-only) |

## Showing Codex and Zed

Codex and Zed are monitored **by default**, sharing the built-in Discord client ID — so the presence text stays "Playing 🦀ClaudeCode🦀" regardless of which tool is active. This needs no configuration: if any of Claude Code, Codex, or Zed is running, the presence is shown.

If you later want a distinct "Playing …" text per tool, note that Discord ties that text to the **Discord application** you connect as (it is not set in this tool's code). Create a Discord application per tool (Discord Developer Portal → Applications → New Application, named "Codex" / "Zed") and copy its **Application ID** into `CCDP_CODEX_CLIENT_ID` / `CCDP_ZED_CLIENT_ID`:

```powershell
$env:CCDP_CODEX_CLIENT_ID = "1234567890123456789"
$env:CCDP_ZED_CLIENT_ID   = "9876543210987654321"
```

Only one presence is shown at a time. A file-based tool whose session was most recently updated wins; a process-only tool (Zed by default) is the fallback. The daemon exits once **none** of the tools' processes are running. If a tool's process name or session directory differs on your system, override it with the matching `CCDP_*` variable. Set a `*_SESSIONS_DIR` to an empty string to treat that tool as process-only (presence shown for as long as the process runs, with no idle clearing).

You can also inspect and control a running instance:

```bash
claudecode-discord-presence --status   # report whether the daemon is running
claudecode-discord-presence --stop      # ask a running daemon to stop
```

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

4. **Leftover files** (`~/.claude/claudecode-discord-presence.pid`, `.pid.lock`, `.stop`) are normally removed automatically when the process exits. If any remain after an abnormal shutdown, they are harmless and can be deleted manually.

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

## License

MIT
