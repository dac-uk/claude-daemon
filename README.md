# claude-daemon

Persistent daemon wrapper for Claude Code. Runs a self-improving team of AI agents with individual identities, tools, memory, and autonomous initiative. Full feature parity across Telegram, Discord, HTTP API, and CLI with seamless cross-platform session handover.

## Features

- **Multi-Agent C-Suite** - 7 named agents (Johnny, Albert, Luna, Max, Penny, Jeremy, Sophie) with individual souls, roles, and domain ownership
- **Per-Agent MCP Tools** - Each agent gets their own MCP server config (GitHub, Slack, Gmail, Google Calendar, Supabase) so they can actually interact with the world
- **Per-Agent Model Routing** - Core team runs Opus, support team runs Sonnet, scheduled tasks run Haiku. Configurable per agent.
- **Parallel Task Dispatch** - `/spawn` multiple tasks to the same agent — each runs concurrently in its own session. Fan-out work across agents simultaneously.
- **Per-Agent Channels** - Bind Telegram groups or Discord channels to specific agents. Dedicated channels for Albert, Luna, etc.
- **Cross-Platform Sessions** - Start a conversation on Telegram, continue on Discord, pick up from CLI. Sessions follow the user, not the platform.
- **Mandatory Planning** - For complex tasks, agents plan first (Opus), publish the plan immediately, then execute autonomously without waiting for approval.
- **Self-Improvement Loop** - Weekly: agents self-assess, cross-agent learnings synthesised, improvement plan generated, proposals delivered to you automatically.
- **Agent Heartbeats** - Autonomous recurring tasks: Penny audits costs at 8am, Jeremy scans security at 2am, Johnny sends morning briefings, Albert audits tech debt, Max runs quality retrospectives.
- **Workflow Engine** - Multi-step orchestration: sequential pipelines, parallel fan-out, and build-review loops (Albert builds, Luna styles, Max reviews, retry on failure)
- **Inter-Agent Delegation** - Agents can request help from other agents mid-task using `[DELEGATE:name]` tags
- **Shared Playbooks** - Lessons learned compound across the team via `shared/playbooks/`. Every agent reads them.
- **HTTP REST API** - Programmatic access, GitHub/Stripe webhooks, metrics endpoint
- **Streaming Responses** - Live streaming to Telegram and Discord with throttled message edits
- **Three-Phase Dreaming** - Light sleep (signal detection), Deep sleep (nightly consolidation + per-agent memory compaction), REM sleep (weekly rewrite + self-reflection + improvement cycle)
- **Memory Validation** - REM sleep validates before overwriting MEMORY.md — rejects catastrophic data loss, logs diffs
- **Full-Text Search** - FTS5-indexed conversation history for searching past interactions
- **Agent Metrics** - Per-agent cost tracking, token usage, and performance metrics
- **Service Files** - systemd and launchd support for true daemon operation

## Quick Start

```bash
pip install -e ".[all]"

mkdir -p ~/.config/claude-daemon
cp config.example.yaml ~/.config/claude-daemon/config.yaml
cp .env.example .env
# Edit .env with your bot tokens and MCP credentials

claude-daemon start --foreground   # Development
claude-daemon start                # Background daemon
claude-daemon status               # Check status
claude-daemon logs --follow        # View logs
```

## The Agent Team

On first run, the daemon bootstraps a 7-agent C-suite team. Each agent has its own workspace, tools, memory, and heartbeat tasks.

| Agent | Title | Emoji | Default Model | Domain |
|-------|-------|-------|---------------|--------|
| **johnny** | CEO | 🎯 | Opus | Orchestration, briefing, routing. Never codes. Council convener. |
| **albert** | CIO | 🧠 | Opus | Architecture, backend, data models, APIs, services, business logic |
| **luna** | Head of Design | 🎨 | Opus | All UI/views, layout, typography, colour, animation, design systems |
| **max** | CPO | 🔬 | Opus | QA, product review, functional + visual testing, holistic quality |
| **penny** | CFO | 💰 | Sonnet | Token spend, API costs, ROI analysis, financial modelling |
| **jeremy** | CRO | 🛡️ | Sonnet | Fraud, cybersecurity, operational risk, compliance |
| **sophie** | CLO | ⚖️ | Sonnet | Legal research, regulatory analysis, commercial counsel |

## Cross-Platform Session Sharing

Sessions follow the user, not the platform. Start a conversation on Telegram, continue on Discord, pick up from the CLI or HTTP API. Context, history, and Claude's `--resume` session are shared seamlessly.

```
Telegram → same session ← Discord
              ↕
           CLI / API
```

This means you can:
- Ask Albert a question on Telegram, get the answer on Discord
- Start a task via the HTTP API, monitor progress on Telegram
- Use whichever platform is most convenient — the agents don't care where you are

## Commands (Telegram + Discord)

All commands are available on **both** Telegram and Discord with full feature parity. On Telegram, use `/command` syntax. On Discord, use slash commands.

| Command | Description |
|---------|-------------|
| `/start` | Show help and available commands |
| `/agents` | List all agents with roles and models |
| `/newagent` | Create a new agent (name, role, emoji) |
| `/setagent` | Modify an agent (fields: role, emoji, model, soul, rules) |
| `/delagent` | Remove an agent from the registry |
| `/status` | Daemon status and session stats |
| `/memory` | View persistent memory (MEMORY.md) |
| `/soul` | View the active agent's soul |
| `/forget` | Clear current session across all platforms |
| `/session` | Current session info and cost |
| `/cost` | Your cumulative usage and costs (all platforms) |
| `/jobs` | List scheduled jobs and next run times |
| `/dream` | Trigger deep sleep memory consolidation |
| `/workflow` | Run build quality gate (Albert → Luna → Max review loop) |
| `/metrics` | Per-agent cost metrics for last 7 days |
| `/spawn` | Spawn a background task on an agent (runs in parallel) |
| `/tasks` | List all spawned background tasks and their status |

Send any message to chat with the active agent (Johnny by default). Use `@agent_name` at the start of a message to address a specific agent.

## Per-Agent Channels

Bind Telegram groups or Discord channels directly to an agent. Messages in bound channels go to that agent without needing `@agent_name`.

```yaml
integrations:
  telegram:
    agent_channels:
      "-1001234567890": "albert"     # Albert's dedicated group
      "-1009876543210": "luna"       # Luna's dedicated group

  discord:
    agent_channels:
      "1234567890123456": "albert"   # #albert-cio channel
      "9876543210987654": "luna"     # #luna-design channel
```

This is ideal for Discord servers where each agent gets their own channel. In agent-bound channels, the bot responds to all messages (no @mention needed).

## Parallel Task Dispatch

Give an agent multiple tasks and they work on them simultaneously:

```
/spawn albert refactor the auth service
/spawn albert build the payment API
/spawn luna redesign the settings page
/tasks                              # Check progress
```

Each spawned task gets its own Claude session — no blocking, no queue. Bounded by `max_concurrent` in config (default: 5).

## Planning Protocol

For multi-step or complex tasks, agents **always plan first**:

1. **Plan** using Opus-level reasoning — outline approach, steps, dependencies, risks
2. **Publish** the plan to you immediately (on whichever platform you're using)
3. **Execute** autonomously — do NOT wait for approval
4. **Update** you if the plan changes during execution

Simple single-step queries skip planning. This is enforced in every agent's system context.

## Agent Workspaces

Each agent workspace at `~/.config/claude-daemon/agents/{name}/` contains:

| File | Purpose |
|------|---------|
| `SOUL.md` | Personality, values, continuous improvement directives |
| `IDENTITY.md` | Name, role, emoji, model + MCP configuration |
| `AGENTS.md` | Operating rules, domain boundaries, planning protocol |
| `MEMORY.md` | Per-agent persistent memory (grows over time) |
| `HEARTBEAT.md` | Autonomous recurring tasks (cron + model + prompt) |
| `REFLECTIONS.md` | Self-assessment: strengths, weaknesses, skills to develop |
| `tools.json` | MCP server configuration for this agent |
| `memory/` | Daily activity logs |

Shared workspace at `~/.config/claude-daemon/shared/`:

| Path | Purpose |
|------|---------|
| `USER.md` | Your context — all agents read this |
| `playbooks/` | Cross-agent lessons learned (compounding knowledge) |
| `learnings.md` | Weekly synthesis of cross-agent insights |
| `events.md` | Auto-maintained agent activity log (inter-agent awareness) |
| `steer/` | Mid-task steering files (e.g. `steer/albert.md`) |
| `checklists/` | QA templates and quality gate checklists |

Edit any `.md` file directly to change an agent's behaviour. No restart needed.

## Self-Improvement Loop

Agents continuously learn and improve without being prompted:

**Weekly cycle (Sunday 5 AM):**
1. **REM Sleep** — Global MEMORY.md rewritten from weekly signals
2. **Per-Agent Self-Assessment** — Each agent evaluates their own performance: rating, strengths, struggles, skills to develop, tools needed, process improvements
3. **Cross-Agent Learning Synthesis** — All reflections aggregated into `shared/learnings.md`, injected into every agent's future context
4. **Improvement Plan** — Reads reflections + metrics + playbooks, generates prioritised proposals with owners and ROI
5. **Proactive Delivery** — Improvement suggestions sent to you via Slack/Telegram automatically

**Ongoing research heartbeats:**
- Johnny: Weekly strategy review (Fri), monthly initiative planning (1st)
- Albert: Weekly tech debt audit (Wed), fortnightly architecture review
- Luna: Weekly design system audit (Thu)
- Max: Weekly quality retrospective (Fri)
- Penny: Monthly cost optimisation (1st)
- Jeremy: Monthly threat landscape (15th)
- Sophie: Monthly regulatory review (1st)

**Knowledge compounding:** Every playbook written to `shared/playbooks/` is indexed and shown to all agents. Luna solves a dark mode problem → writes playbook → Albert reads it when doing related work.

## MCP Tool Assignments

| Agent | MCP Servers | Why |
|-------|-------------|-----|
| **Johnny** | Slack, Gmail, Google Calendar, GitHub | Communications, scheduling, project oversight |
| **Albert** | GitHub, Supabase, Slack | Code, databases, progress updates |
| **Luna** | GitHub, Slack | UI code, design reviews |
| **Max** | GitHub, Supabase, Slack | PR reviews, quality metrics, bug reports |
| **Penny** | Supabase, Gmail, Slack | Financial data, cost reports, invoices |
| **Jeremy** | GitHub, Supabase, Slack | Security scanning, audit queries, risk alerts |
| **Sophie** | Gmail, Slack | Legal correspondence, compliance monitoring |

## Workflow Engine

Multi-step agent orchestration triggered via `/workflow` or `POST /api/workflow`:

- **Pipeline**: Sequential steps where each agent receives the previous result
- **Parallel**: Fan-out to multiple agents simultaneously, collect all results
- **Review Loop**: Build-review cycle with retry (Albert builds, Max reviews, fix loop)

## HTTP API

Enable with `api_enabled: true`. Exposes the daemon for external automation.

```
GET  /api/health              — Health check (always public)
GET  /api/agents              — List agents with roles, models, MCP status
GET  /api/status              — Daemon status and metrics
GET  /api/metrics             — Per-agent cost/token metrics
POST /api/message             — Send a message to an agent
POST /api/workflow            — Trigger build quality gate workflow
POST /api/webhook/github      — GitHub webhook (→ Max/Albert/Johnny)
POST /api/webhook/stripe      — Stripe webhook (→ Penny)
POST /api/webhook/{source}    — Generic webhook (→ Johnny)
```

Auth: `Authorization: Bearer <api_key>` header.

## Architecture

```
Telegram / Discord / CLI / HTTP API / Webhooks
              |
     (cross-platform session lookup by user_id)
              |
              v
     MessageRouter (normalize, rate-limit, route)
              |
    _resolve_agent()
    - Per-channel binding (agent_channels config)
    - Explicit: @albert or /luna prefix
    - Default: johnny (orchestrator)
              |
     WorkflowEngine (for multi-step tasks)
     - Pipeline / Parallel / Review Loop
              |
     spawn_task() for background parallel work
              |
              v
  Orchestrator.send_to_agent(agent, model, mcp_config)
     → [DELEGATE:name] tag processing for inter-agent calls
              |
    agent.build_system_context()
    SOUL + IDENTITY + AGENTS + Planning Protocol +
    USER + TOOLS + MEMORY + REFLECTIONS +
    Steering + Events + Playbooks + Learnings
              |
              v
  ProcessManager (model, mcp_config, --resume)
              |
              v
  ClaudeResponse → SQLite + FTS5 → agent_metrics → daily log → user

  Scheduler (APScheduler)
    - Dreaming: deep sleep (4 AM), REM sleep (Sunday 5 AM)
    - Improvement: self-assessments, learning synthesis, improvement plan
    - Agent heartbeats: from HEARTBEAT.md (research, audits, reports)
    - Per-agent memory compaction (nightly)
```

## Configuration

```yaml
daemon:
  log_level: INFO
  api_enabled: true
  api_port: 8080

claude:
  binary: claude
  max_concurrent: 5               # Parallel task limit
  max_budget_per_message: 0.50
  permission_mode: auto

memory:
  daily_log: true
  compaction_threshold: 50000
  max_session_age_hours: 72
  dream_enabled: true
  self_improve: true               # Enable self-assessment cycle

integrations:
  telegram:
    enabled: true
    allowed_user_ids: []
    polling: true
    agent_channels:                # Optional per-agent groups
      "-1001234567890": "albert"

  discord:
    enabled: true
    allowed_guild_ids: []
    agent_channels:                # Optional per-agent channels
      "1234567890123456": "albert"
```

Environment variables (`.env`):
```
TELEGRAM_BOT_TOKEN=your_token
DISCORD_BOT_TOKEN=your_token
CLAUDE_DAEMON_API_KEY=your_api_key
GITHUB_TOKEN=ghp_...
SLACK_BOT_TOKEN=xoxb-...
SUPABASE_ACCESS_TOKEN=sbp_...
```

## Data Directory

```
~/.config/claude-daemon/
├── config.yaml
├── claude_daemon.db              # SQLite (conversations, FTS5, agent_metrics)
├── agents/
│   ├── johnny/
│   │   ├── SOUL.md               # Identity + improvement directives
│   │   ├── IDENTITY.md           # Role, model, MCP config
│   │   ├── AGENTS.md             # Operating rules, planning protocol
│   │   ├── MEMORY.md             # Per-agent persistent memory
│   │   ├── HEARTBEAT.md          # Autonomous cron tasks
│   │   ├── REFLECTIONS.md        # Self-assessment (auto-generated weekly)
│   │   ├── tools.json            # MCP server config
│   │   └── memory/               # Daily logs
│   ├── albert/
│   ├── luna/
│   └── ...
├── shared/
│   ├── USER.md                   # Your context (all agents read)
│   ├── learnings.md              # Cross-agent insights (auto-generated)
│   ├── events.md                 # Agent activity log (auto-maintained)
│   ├── playbooks/                # Compounding lessons
│   │   ├── improvement-plan.md   # Weekly improvement proposals
│   │   ├── tech-debt.md          # Albert's findings
│   │   ├── quality-retro.md      # Max's findings
│   │   └── ...
│   ├── steer/                    # Mid-task redirection
│   └── checklists/               # QA templates
├── memory/                       # Global daemon memory
└── logs/
```
