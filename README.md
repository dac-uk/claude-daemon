# claude-daemon

Persistent daemon wrapper for Claude Code. Auto-updates nightly, runs cron jobs, manages memory across sessions, and connects to Telegram, Discord, and Paperclip.

Does everything OpenClaw did minus the bloat.

## Features

- **Persistent Sessions** - Conversations survive daemon restarts via Claude Code's `--resume`
- **Three-Tier Memory** - Working memory, SQLite conversation store, and durable markdown logs (MEMORY.md + daily logs)
- **Auto-Dream** - Weekly memory consolidation extracts patterns and preferences into persistent memory
- **Scheduler** - APScheduler-based cron for auto-updates, memory compaction, and custom jobs
- **Nightly Auto-Update** - Checks for and installs Claude Code updates at 3 AM
- **Telegram Bot** - Chat with Claude from Telegram with full session persistence
- **Discord Bot** - Slash commands and DM support with typing indicators
- **Paperclip** - Multi-agent orchestration via REST API polling
- **Service Files** - systemd and launchd support for true daemon operation

## Quick Start

```bash
# Install core
pip install -e .

# Install with Telegram support
pip install -e ".[telegram]"

# Install everything
pip install -e ".[all]"

# Start in foreground (development)
claude-daemon start --foreground

# Start as background daemon
claude-daemon start

# Check status
claude-daemon status

# View logs
claude-daemon logs --follow
```

## Configuration

```bash
# Create config directory
mkdir -p ~/.config/claude-daemon

# Copy example config
cp config.example.yaml ~/.config/claude-daemon/config.yaml

# Set up secrets
cp .env.example .env
# Edit .env with your bot tokens
```

## CLI Commands

```
claude-daemon start [--foreground] [--config PATH]   Start the daemon
claude-daemon stop                                    Stop the daemon
claude-daemon restart                                 Restart the daemon
claude-daemon status                                  Show daemon status
claude-daemon logs [--follow] [--lines N]             View logs
claude-daemon config [--edit]                         Show/edit configuration
claude-daemon memory [show|compact|dream]             Memory management
claude-daemon update [--check-only]                   Check for updates
claude-daemon jobs                                    List scheduled jobs
claude-daemon install-service [--systemd|--launchd]   Install OS service
```

## Architecture

```
Telegram/Discord/Paperclip
        |
        v
  MessageRouter (normalize, route, format)
        |
        v
  ClaudeDaemon.handle_message()
    |-- WorkingMemory.build_context() -> MEMORY.md + daily logs + summary
    |-- ConversationStore.add_message()
        |
        v
  ProcessManager.send_message()
    claude --print --output-format json --resume <session_id> --append-system-prompt <ctx>
        |
        v
  ClaudeResponse -> store in SQLite -> append daily log -> send back to user
```

## Scheduled Jobs

| Job | Default | Description |
|-----|---------|-------------|
| auto_update | 3 AM daily | Check and install Claude Code updates |
| memory_compaction | 4 AM daily | Summarize sessions, write daily logs |
| auto_dream | Sunday 5 AM | Consolidate weekly memory into MEMORY.md |
| session_cleanup | Every 6h | Archive expired conversations |
| heartbeat | Every 30 min | Health status log |

Custom jobs can be defined in `config.yaml`:

```yaml
scheduler:
  custom_jobs:
    - id: daily_standup
      cron: "0 9 * * 1-5"
      prompt: "Review recent git commits and write a standup summary"
      target_platform: telegram
      target_chat_id: "12345678"
```

## Memory System

- **MEMORY.md** - Persistent cross-session notes, preferences, facts
- **memory/YYYY-MM-DD.md** - Daily activity logs
- **SQLite** - Conversation history, session metadata, summaries
- **Auto-Dream** - Weekly consolidation via Claude summarization
