# claude-daemon

Persistent daemon wrapper for Claude Code. Runs a named team of AI agents with individual identities, memory, and model routing. Connects to Telegram, Discord, and Paperclip. Auto-updates nightly, runs cron jobs, and consolidates memory while you sleep.

## Features

- **Multi-Agent C-Suite** - 7 named agents (Johnny, Albert, Luna, Max, Penny, Jeremy, Sophie) with individual souls, roles, and domain ownership
- **Per-Agent MCP Tools** - Each agent gets their own MCP server config (GitHub, Slack, Gmail, Google Calendar, Supabase) so they can actually interact with the world
- **Per-Agent Model Routing** - Core team runs Opus, support team runs Sonnet, scheduled tasks run Haiku. Configurable per agent.
- **Agent Heartbeats** - Each agent has autonomous recurring tasks defined in `HEARTBEAT.md` — Penny audits costs at 8am, Jeremy scans security at 2am, Johnny sends morning briefings at 9am
- **Workflow Engine** - Multi-step orchestration: sequential pipelines, parallel fan-out, and build-review loops (Albert builds, Luna styles, Max reviews, retry on failure)
- **HTTP REST API** - Programmatic access for external automation, GitHub/Stripe webhooks, and custom integrations
- **Streaming Responses** - Live streaming to Telegram and Discord with throttled message edits
- **Three-Phase Dreaming** - Light sleep (per-session signal detection), Deep sleep (nightly consolidation), REM sleep (weekly MEMORY.md rewrite + self-reflection)
- **Memory Validation** - REM sleep validates before overwriting MEMORY.md — rejects catastrophic data loss, logs diffs
- **Full-Text Search** - FTS5-indexed conversation history for searching past interactions
- **Agent Metrics** - Per-agent cost tracking, token usage, and performance metrics in SQLite
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
| `IDENTITY.md` | Name, role, emoji, model + MCP configuration |
| `AGENTS.md` | Operating rules, domain boundaries, procedures |
| `MEMORY.md` | Per-agent persistent memory (grows over time) |
| `HEARTBEAT.md` | Autonomous recurring tasks (cron + model + prompt) |
| `tools.json` | MCP server configuration for this agent |
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

Model and MCP tools are configured per agent in `IDENTITY.md`:

```
Name: albert
Role: CIO
Emoji: 🧠
Model: opus
Planning-Model: opus
Chat-Model: opus
Scheduled-Model: haiku
MCP-Config: tools.json
```

| Field | Used for |
|-------|---------|
| `Model` | Default for most tasks |
| `Planning-Model` | Architecture and strategy work |
| `Chat-Model` | Interactive conversation |
| `Scheduled-Model` | Heartbeat and cron tasks |
| `MCP-Config` | Path to MCP server config JSON in agent workspace |

Change any agent's model via chat: `/setagent penny model opus`

## MCP Tool Assignments

Each agent gets a `tools.json` in their workspace defining which MCP servers they can use:

| Agent | MCP Servers | Why |
|-------|-------------|-----|
| **Johnny** | Slack, Gmail, Google Calendar, GitHub | Communications, scheduling, project oversight |
| **Albert** | GitHub, Supabase, Slack | Code, databases, progress updates |
| **Luna** | GitHub, Slack | UI code, design reviews |
| **Max** | GitHub, Supabase, Slack | PR reviews, quality metrics, bug reports |
| **Penny** | Supabase, Gmail, Slack | Financial data, cost reports, invoices |
| **Jeremy** | GitHub, Supabase, Slack | Security scanning, audit queries, risk alerts |
| **Sophie** | Gmail, Slack | Legal correspondence, compliance monitoring |

To configure, edit `~/.config/claude-daemon/agents/{name}/tools.json` with your actual credentials.

## Agent Heartbeats

Each agent has autonomous recurring tasks in `HEARTBEAT.md`:

```markdown
## Morning Briefing
Cron: 0 9 * * *
Model: sonnet
Check Gmail for urgent overnight emails. Check Google Calendar for today's meetings.
Compile a concise briefing and send to Dave via Slack.

## Weekly Financial Report
Cron: 0 8 * * 1
Model: sonnet
Pull this week's API costs from Supabase. Compare to last week. Flag anomalies.
```

The scheduler parses these on startup and registers them as cron jobs. Each runs as that agent with their full identity, memory, and MCP tools.

## Workflow Engine

Multi-step agent orchestration with three execution patterns:

- **Pipeline**: Sequential steps where each agent receives the previous result
- **Parallel**: Fan-out to multiple agents simultaneously, collect all results
- **Review Loop**: Build-review cycle (Albert builds, Max reviews, retry on failure)

The build quality gate from Johnny's operating rules:
1. Albert builds the backend
2. Luna builds the UI
3. Max reviews both for quality
4. If Max says FAIL: route bugs to correct owner, repeat

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

## HTTP API

Enable with `api_enabled: true` in config. Exposes the daemon for external automation.

```
GET  /api/health              — Health check (always public)
GET  /api/agents              — List agents with roles, models, MCP status
GET  /api/status              — Daemon status and metrics
POST /api/message             — Send a message to an agent
POST /api/webhook/github      — GitHub webhook (routes to Max/Albert/Johnny)
POST /api/webhook/stripe      — Stripe webhook (routes to Penny)
POST /api/webhook/{source}    — Generic webhook (routes to Johnny)
```

Authentication via `Authorization: Bearer <api_key>` header. Set in config or `CLAUDE_DAEMON_API_KEY` env var.

```yaml
daemon:
  api_enabled: true
  api_port: 8080
  api_key: "your-secret-key"
```

## Architecture

```
Telegram / Discord / Paperclip / HTTP API / Webhooks
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
     WorkflowEngine (for multi-step tasks)
     - Pipeline / Parallel / Review Loop
           |
           v
  Orchestrator.send_to_agent(agent, model, mcp_config)
           |
    agent.build_system_context()
    SOUL + IDENTITY + AGENTS rules +
    USER context + MEMORY + REFLECTIONS
           |
           v
  ProcessManager.send_message(model, mcp_config)
    claude --print --output-format stream-json
           --resume <session_id>
           --model <agent model>
           --mcp-config <agent tools.json>
           --append-system-prompt <context>
           |
           v
  ClaudeResponse → SQLite + FTS5 → agent_metrics → daily log → user

  Scheduler (APScheduler)
    - Builtin jobs: update, deep sleep, REM sleep, cleanup
    - Agent heartbeats: parsed from HEARTBEAT.md, runs as agent
    - Custom jobs: YAML-defined cron + prompt
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
│   │   ├── HEARTBEAT.md
│   │   ├── tools.json
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
