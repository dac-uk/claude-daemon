# claude-daemon

Persistent daemon wrapper for Claude Code. Runs a named team of AI agents with individual identities, memory, and model routing. Connects to Telegram, Discord, and Paperclip. Auto-updates nightly, runs cron jobs, and consolidates memory while you sleep.

## Features

- **Multi-Agent C-Suite** - 7 named agents (Johnny, Albert, Luna, Max, Penny, Jeremy, Sophie) with individual souls, roles, and domain ownership
- **Per-Agent Model Routing** - Core team runs Opus, support team runs Sonnet, scheduled tasks run Haiku. Configurable per agent.
- **Streaming Responses** - Live streaming to Telegram and Discord with throttled message edits
- **Three-Phase Dreaming** - Light sleep (per-session signal detection), Deep sleep (nightly consolidation), REM sleep (weekly MEMORY.md rewrite + self-reflection)
- **Persistent Sessions** - Conversations survive restarts via Claude Code's `--resume`
- **Three-Tier Memory** - Working memory, SQLite conversation store, and durable markdown (MEMORY.md + daily logs + REFLECTIONS.md)
- **SOUL.md Identity** - Each agent has a personality file injected into every prompt
- **Dynamic Agent Management** - Create, modify, or remove agents via chat commands at runtime
- **Agent Addressing** - Address agents directly with `@albert` or `/luna` in any message
- **Scheduler** - APScheduler cron for auto-updates, memory compaction, custom jobs, and agent heartbeats
- **Telegram Bot** - Full streaming chat, agent switching, memory inspection, cost tracking
- **Discord Bot** - Slash commands, streaming responses, DM support
- **Paperclip** - REST API polling integration
- **Service Files** - systemd and launchd support for true daemon operation

## Quick Start

```bash
# Install core
pip install -e .

# Install with Telegram support
pip install -e ".[telegram]"

# Install with Discord support
pip install -e ".[discord]"

# Install everything
pip install -e ".[all]"

# Configure (copy and edit)
mkdir -p ~/.config/claude-daemon
cp config.example.yaml ~/.config/claude-daemon/config.yaml
cp .env.example .env
# Edit .env with your bot tokens

# Start in foreground (development)
claude-daemon start --foreground

# Start as background daemon
claude-daemon start

# Check status
claude-daemon status

# View logs
claude-daemon logs --follow
```

## The Agent Team

On first run, the daemon bootstraps a 7-agent C-suite team. Each agent has its own workspace at `~/.config/claude-daemon/agents/{name}/` and reads a shared `USER.md` for user context.

| Agent | Title | Emoji | Default Model | Domain |
|-------|-------|-------|---------------|--------|
| **johnny** | CEO | 🎯 | Opus | Orchestration, briefing, routing. Never codes. Council convener. |
| **albert** | CIO | 🧠 | Opus | Architecture, backend, data models, APIs, services, business logic |
| **luna** | Head of Design | 🎨 | Opus | All UI/views, layout, typography, colour, animation, design systems |
| **max** | CPO | 🔬 | Opus | QA, product review, functional + visual testing, holistic quality |
| **penny** | CFO | 💰 | Sonnet | Token spend, API costs, ROI analysis, financial modelling |
| **jeremy** | CRO | 🛡️ | Sonnet | Fraud, cybersecurity, operational risk, compliance |
| **sophie** | CLO | ⚖️ | Sonnet | Legal research, regulatory analysis, commercial counsel |

**Routing rules:**
- Johnny (CEO) orchestrates by default — he routes, never codes
- Address any agent directly: `@albert refactor the auth service` or `/luna fix the dark mode contrast`
- Johnny convenes the council internally; escalates to you only for capital >£500, legal exposure, public commitments, or deadlocks

## Agent Workspaces

Each agent workspace at `~/.config/claude-daemon/agents/{name}/` contains:

| File | Purpose |
|------|---------|
| `SOUL.md` | Personality, values, beliefs, communication style |
| `IDENTITY.md` | Name, role, emoji, model configuration |
| `AGENTS.md` | Operating rules, domain boundaries, procedures |
| `MEMORY.md` | Per-agent persistent memory (grows over time) |
| `memory/` | Daily activity logs |

Shared workspace at `~/.config/claude-daemon/shared/`:

| Path | Purpose |
|------|---------|
| `USER.md` | Your context — all agents read this |
| `playbooks/` | Cross-agent lessons learned |
| `steer/` | Mid-task steering files (e.g. `steer/albert.md`) |
| `reflections/` | Post-task reflections from all agents |
| `checklists/` | QA templates and quality gate checklists |

Edit any `.md` file directly to change an agent's behaviour. No restart needed — files are read on each message.

## Model Routing

Model is configured per agent in `IDENTITY.md`:

```
Name: albert
Role: CIO
Emoji: 🧠
Model: opus
Planning-Model: opus
Chat-Model: opus
Scheduled-Model: haiku
```

| Field | Used for |
|-------|---------|
| `Model` | Default for most tasks |
| `Planning-Model` | Architecture and strategy work |
| `Chat-Model` | Interactive conversation |
| `Scheduled-Model` | Heartbeat and cron tasks |

Change any agent's model via chat: `/setagent penny model opus`

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
claude-daemon agents                                  List all agents
claude-daemon agents create <name> [--role] [--emoji] [--orchestrator]
```

## Telegram Commands

| Command | Description |
|---------|-------------|
| `/agents` | List all agents with roles and models |
| `/newagent name role [emoji]` | Create a new agent |
| `/setagent name field value` | Modify an agent (fields: role, emoji, model, soul, rules) |
| `/delagent name` | Remove an agent from the registry |
| `/status` | Daemon status and session stats |
| `/memory` | View persistent memory (MEMORY.md) |
| `/soul` | View the active agent's soul |
| `/forget` | Clear current session, start fresh |
| `/session` | Current session info and cost |
| `/cost` | Your cumulative usage and costs |
| `/jobs` | List scheduled jobs and next run times |
| `/dream` | Trigger deep sleep memory consolidation now |

Send any message to chat with the active agent (Johnny by default). Use `@agent_name` or `/agent_name` at the start of a message to address a specific agent.

## Dynamic Agent Management

Agents can be created, modified, or removed at runtime via chat or Telegram commands — no restart required.

```
# Create a new agent
/newagent analyst Data Analyst 📊

# Change an agent's model
/setagent penny model opus

# Change an agent's role
/setagent albert role Lead Engineer

# Update an agent's soul inline
/setagent johnny soul I am Johnny, CEO. I route work and never write code.

# Remove an agent
/delagent analyst
```

Agent workspace files (`SOUL.md`, `IDENTITY.md`, `AGENTS.md`) are preserved on deletion. Re-add the agent later and it picks up where it left off.

## Architecture

```
Telegram / Discord / Paperclip
           |
           v
     MessageRouter
   (normalize, rate-limit, route)
           |
           v
  ClaudeDaemon.handle_message()
           |
    _resolve_agent()
    - Explicit: @albert or /luna prefix
    - Default: johnny (CEO/orchestrator)
           |
           v
  Orchestrator.send_to_agent(agent, model)
           |
    agent.build_system_context()
    SOUL + IDENTITY + AGENTS rules +
    USER context + MEMORY + REFLECTIONS
           |
           v
  ProcessManager.send_message(model_override)
    claude --print --output-format stream-json
           --resume <session_id>
           --model <agent model>
           --append-system-prompt <context>
           |
           v
  ClaudeResponse → SQLite store → daily log → user
```

## Memory System

**Three tiers:**

1. **Working Memory** — Recent conversation history injected into each prompt
2. **SQLite Store** — Full conversation history, session metadata, costs, per-user stats
3. **Durable Markdown** — Long-lived files read/written by Claude itself:
   - `MEMORY.md` — Persistent preferences, facts, decisions
   - `REFLECTIONS.md` — Self-improvement learnings
   - `memory/YYYY-MM-DD.md` — Daily activity logs

**Three-phase dreaming:**

| Phase | Trigger | What happens |
|-------|---------|-------------|
| Light Sleep | After each conversation | Signal detection — extracts HIGH/MEDIUM/LOW priority items |
| Deep Sleep | 4 AM daily (cron) | Consolidates daily signals into durable memory |
| REM Sleep | Sunday 5 AM (cron) | Full MEMORY.md rewrite + self-reflection → REFLECTIONS.md |

Trigger manually: `claude-daemon memory dream` or `/dream` in Telegram.

## Scheduled Jobs

| Job | Default | Description |
|-----|---------|-------------|
| `auto_update` | 3 AM daily | Check and install Claude Code updates |
| `memory_compaction` | 4 AM daily | Deep sleep — consolidate daily logs into memory |
| `auto_dream` | Sunday 5 AM | REM sleep — full memory rewrite + self-reflection |
| `session_cleanup` | Every 6h | Archive expired conversations |
| `heartbeat` | Every 30 min | Health status log (uses Haiku) |

Custom jobs in `config.yaml`:

```yaml
scheduler:
  custom_jobs:
    - id: daily_standup
      cron: "0 9 * * 1-5"
      prompt: "Review recent git commits and write a standup summary"
      target_platform: telegram
      target_chat_id: "12345678"

    - id: weekly_review
      cron: "0 17 * * 5"
      prompt: "Summarize what we accomplished this week"
      target_platform: discord
      target_channel_id: "987654321"
```

## Configuration

```yaml
# ~/.config/claude-daemon/config.yaml

daemon:
  log_level: INFO

claude:
  binary: claude
  max_concurrent: 3
  max_budget_per_message: 0.50   # USD cost cap per message
  permission_mode: auto           # auto, default, bypassPermissions

memory:
  daily_log: true
  compaction_threshold: 50000    # Characters before auto-compaction
  max_session_age_hours: 72
  dream_enabled: true

integrations:
  telegram:
    enabled: true
    allowed_user_ids: []          # Empty = allow all
    polling: true

  discord:
    enabled: false
    allowed_guild_ids: []

  paperclip:
    enabled: false
    poll_interval: 5
```

Environment variables (`.env`):

```
TELEGRAM_BOT_TOKEN=your_token
DISCORD_BOT_TOKEN=your_token
PAPERCLIP_URL=https://your-paperclip-instance.com
PAPERCLIP_API_KEY=your_key
```

## Install as a Service

**systemd (Linux):**
```bash
claude-daemon install-service --systemd
systemctl --user enable --now claude-daemon
```

**launchd (macOS):**
```bash
claude-daemon install-service --launchd
launchctl load ~/Library/LaunchAgents/com.claude-daemon.plist
```

## Data Directory

Everything lives in `~/.config/claude-daemon/`:

```
~/.config/claude-daemon/
├── config.yaml              # Main configuration
├── claude-daemon.pid        # PID file (while running)
├── claude_daemon.db         # SQLite conversation store
├── agents/                  # Agent workspaces
│   ├── johnny/
│   │   ├── SOUL.md
│   │   ├── IDENTITY.md
│   │   ├── AGENTS.md
│   │   ├── MEMORY.md
│   │   └── memory/
│   ├── albert/
│   ├── luna/
│   └── ...
├── shared/                  # Cross-agent shared files
│   ├── USER.md              # Your context (all agents read this)
│   ├── playbooks/
│   ├── steer/
│   ├── reflections/
│   └── checklists/
├── memory/                  # Global daemon memory
│   ├── SOUL.md
│   ├── MEMORY.md
│   ├── REFLECTIONS.md
│   └── YYYY-MM-DD.md
└── logs/
    └── daemon.log
```
