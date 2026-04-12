# claude-daemon

Persistent daemon wrapper for Claude Code. Runs a self-improving team of AI agents with individual identities, tools, memory, and autonomous initiative. Full feature parity across Telegram, Discord, HTTP API, and CLI with seamless cross-platform session handover.

## Features

- **Multi-Agent C-Suite** - 7 named agents (Johnny, Albert, Luna, Max, Penny, Jeremy, Sophie) with individual souls, roles, and domain ownership
- **Per-Agent MCP Tools** - Each agent gets their own MCP server config (GitHub, Slack, Gmail, Google Calendar, Supabase) so they can actually interact with the world
- **Per-Agent Model Routing** - Core team runs Opus, support team runs Sonnet, scheduled tasks run Haiku. Configurable per agent.
- **Auto-Parallel Execution** - Send multiple messages to the same agent — if busy, the daemon automatically spawns a parallel session. No `/spawn` needed. Also available as `/spawn` for explicit control.
- **Per-Agent Channels** - Bind Telegram groups or Discord channels to specific agents. Dedicated channels for Albert, Luna, etc.
- **Cross-Platform Sessions** - Start a conversation on Telegram, continue on Discord, pick up from CLI. Sessions follow the user, not the platform.
- **Mandatory Planning** - For complex tasks, agents plan first (Opus), publish the plan immediately, then execute autonomously without waiting for approval.
- **Self-Improvement Loop** - Weekly: agents self-assess, cross-agent learnings synthesised, improvement plan generated, proposals delivered to you automatically.
- **Agent Heartbeats** - Autonomous recurring tasks: Penny audits costs at 8am, Jeremy scans security at 2am, Johnny sends morning briefings, Albert audits tech debt, Max runs quality retrospectives.
- **Workflow Engine** - Multi-step orchestration: sequential pipelines, parallel fan-out, and build-review loops (Albert builds, Luna styles, Max reviews, retry on failure)
- **Inter-Agent Delegation** - Agents can request help from other agents mid-task using `[DELEGATE:name]` tags
- **Shared Playbooks** - Lessons learned compound across the team via `shared/playbooks/`. Every agent reads them.
- **Live Agent Dashboard** - Browser-based D3 force graph showing all agents, real-time status, streaming thought output, and event log. Accessible over Tailscale/ZeroTier.
- **HTTP REST API** - Programmatic access, GitHub/Stripe webhooks, metrics endpoint, WebSocket event bus
- **Hardened Webhooks** - GitHub and Stripe webhooks verify HMAC-SHA256 signatures. Invalid requests get 403. Handlers run async (202 Accepted) so webhooks never block the HTTP server.
- **Resilient Heartbeats** - Circuit breaker pauses autonomous jobs after 3 consecutive failures. Auto-resumes on success. All delivery failures are logged — no silent drops.
- **Context Priority** - SOUL and steering instructions are never truncated. Low-priority blocks (vision, playbooks) are trimmed first when context budget is tight.
- **MCP Health Checks** - `/api/agents` reports per-server MCP status, detecting unresolved `${ENV_VAR}` placeholders in `tools.json` so misconfigured tools surface immediately.
- **DB Integrity** - SQLite `PRAGMA integrity_check` runs on startup. Corrupt databases are flagged in logs before any data is written.
- **Streaming Responses** - Live streaming to Telegram and Discord with throttled message edits
- **Three-Phase Dreaming** - Light sleep (signal detection), Deep sleep (nightly consolidation + per-agent memory compaction), REM sleep (weekly rewrite + self-reflection + improvement cycle)
- **Memory Validation** - REM sleep validates before overwriting MEMORY.md — rejects catastrophic data loss, logs diffs
- **Full-Text Search** - FTS5-indexed conversation history for searching past interactions
- **Agent Metrics** - Per-agent cost tracking, token usage, and performance metrics
- **Service Files** - systemd and launchd support for true daemon operation

## Quick Install

```bash
# One-line install (clones repo, installs deps, configures service)
curl -sSL https://raw.githubusercontent.com/dac-uk/claude-daemon/main/install.sh | bash
```

Or if you've already cloned the repo:

```bash
./install.sh
```

The script handles everything: Python package, config templates, systemd/launchd service. Idempotent — safe to run again. After install, edit `~/.config/claude-daemon/.env` with your tokens and you're live.

## Manual Setup

If you prefer to set things up yourself:

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

## Channel Setup Guide

### How routing works

Messages flow through a routing chain:

1. **Channel binding** — if the message arrives in a channel mapped to a specific agent in config, that agent handles it directly
2. **@agent addressing** — `@albert build the API` routes to Albert regardless of which channel you're in
3. **Auto-routing** — if neither of the above, Johnny (the orchestrator) decides who should handle it

In **DMs**, the bot always listens. In **groups/channels**, the bot only responds when mentioned or when the channel is bound to an agent.

### Telegram setup

**1. Create a bot** via [@BotFather](https://t.me/BotFather) on Telegram. Copy the token to your `.env`:

```
TELEGRAM_BOT_TOKEN=bot123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11
```

**2. Main channel (Johnny / orchestrator):** Just DM the bot or add it to any group. Unbound groups route through Johnny, who auto-routes to the right agent. No config needed.

**3. Direct agent channels:** Create a Telegram group per agent, add the bot, then get the chat ID. The easiest way to find it: add [@userinfobot](https://t.me/userinfobot) to the group, or send a message and check the bot's log output. Group IDs are negative numbers.

```yaml
# ~/.config/claude-daemon/config.yaml
integrations:
  telegram:
    allowed_user_ids: []           # Empty = allow all (restrict in production)
    agent_channels:
      "-1001234567890": "albert"   # "Albert CIO" group → Albert handles all messages
      "-1009876543210": "luna"     # "Luna Design" group → Luna handles all messages
      "-1001111111111": "max"      # "Max QA" group → Max handles all messages
      "-1002222222222": "penny"    # "Penny Finance" group → Penny handles all messages
```

**4. Restrict access** (recommended): Set `allowed_user_ids` to your Telegram user ID(s). Only listed users can interact with the bot.

```yaml
    allowed_user_ids:
      - 123456789                  # Your Telegram user ID
```

In any channel (bound or not), you can still override with `@agent_name`:

```
@luna redesign the login page     # Routes to Luna even in Albert's group
```

### Discord setup

**1. Create a bot** in the [Discord Developer Portal](https://discord.com/developers/applications). Enable MESSAGE CONTENT intent. Copy the token to your `.env`:

```
DISCORD_BOT_TOKEN=MTIzNDU2Nzg5MDEyMzQ1Njc4OQ.AbCdEf.GhIjKlMnOpQrStUvWxYz
```

**2. Invite the bot** to your server with the OAuth2 URL from the portal. It needs: Send Messages, Read Message History, and Embed Links permissions.

**3. Main channel (Johnny / orchestrator):** In any channel, @mention the bot. Unbound channels route through Johnny. In DMs, the bot always listens — no mention needed.

**4. Direct agent channels:** Create text channels for each agent. Right-click the channel → Copy Channel ID (enable Developer Mode in Discord settings first).

```yaml
# ~/.config/claude-daemon/config.yaml
integrations:
  discord:
    allowed_guild_ids: []          # Empty = allow all guilds
    agent_channels:
      "1100000000000000001": "albert"   # #albert-cio → Albert
      "1100000000000000002": "luna"     # #luna-design → Luna
      "1100000000000000003": "max"      # #max-qa → Max
      "1100000000000000004": "penny"    # #penny-finance → Penny
      "1100000000000000005": "jeremy"   # #jeremy-security → Jeremy
      "1100000000000000006": "sophie"   # #sophie-legal → Sophie
```

**5. Alert channel** (optional): Designate a channel for heartbeat results, improvement plans, and system notifications:

```yaml
    alert_channel_ids:
      - "1100000000000000007"      # #agent-alerts channel
```

**6. Restrict to your server** (recommended):

```yaml
    allowed_guild_ids:
      - 987654321098765432         # Your server ID
```

In agent-bound channels, the bot responds to **all messages** — no @mention needed. In unbound channels, @mention the bot to talk to Johnny, or use `@agent_name` to address a specific agent.

### Recommended server layout

For a full Discord setup with all agents:

```
#general            → Unbound (mention bot → Johnny routes)
#albert-cio         → Bound to albert (architecture, backend)
#luna-design        → Bound to luna (UI, design systems)
#max-qa             → Bound to max (testing, quality)
#penny-finance      → Bound to penny (costs, budgets)
#jeremy-security    → Bound to jeremy (security, compliance)
#sophie-legal       → Bound to sophie (legal, regulatory)
#agent-alerts       → Alert channel (heartbeat results, improvements)
```

The same structure works with Telegram groups — one group per agent, plus one general group for Johnny.

## Auto-Parallel Execution

Just send messages naturally. If an agent is already processing, the daemon **automatically** starts a parallel session instead of making you wait:

```
@albert refactor the auth service     # Starts processing
@albert build the payment API         # Agent busy → auto-parallel session
@albert review the database schema    # Agent busy → another parallel session
```

No special commands needed. The daemon detects busy agents and spawns fresh sessions transparently. Bounded by `max_concurrent` in config (default: 5).

For explicit background tasks, `/spawn` is still available:

```
/spawn albert refactor the auth service
/spawn luna redesign the settings page
/tasks                              # Check progress
```

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

**Circuit breaker:** If a heartbeat job fails 3 times consecutively, it is paused and a warning is logged. It auto-resumes once a run succeeds again. This prevents a broken credential or unreachable service from flooding logs.

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

## Live Agent Dashboard

A browser-based dashboard showing all agents as a live force graph with real-time status, streaming output, and event log.

```yaml
daemon:
  api_enabled: true
  api_port: 8080
  dashboard_enabled: true
  api_bind: "0.0.0.0"    # Accessible from Tailscale/ZeroTier/LAN
```

Open `http://<your-ip>:8080/` in any browser. Works on any device on your Tailscale or ZeroTier network.

Features:
- **Force graph**: 7 agent nodes with colour-coded status (idle/busy), pulsing animation when active
- **Click to expand**: Click any agent node to see their live thought stream in the side panel
- **Event log**: Scrolling log of heartbeat results, task completions, auto-parallel events
- **WebSocket**: Real-time updates via `/ws` — no polling
- **Stats bar**: Active sessions, agent count, cost today

The dashboard uses D3.js (loaded from CDN). No build step, no npm, no bundler — just a single HTML file.

## HTTP API

Enable with `api_enabled: true`. Exposes the daemon for external automation.

```
GET  /api/health              — Health check (always public)
GET  /api/agents              — List agents with roles, models, MCP status
GET  /api/status              — Daemon status and metrics
GET  /api/sessions            — Active Claude subprocesses
GET  /api/tasks               — Spawned background tasks
GET  /api/metrics             — Per-agent cost/token metrics
POST /api/message             — Send a message to an agent
POST /api/workflow            — Trigger build quality gate workflow
POST /api/webhook/github      — GitHub webhook (→ Max/Albert/Johnny) — 202 Accepted
POST /api/webhook/stripe      — Stripe webhook (→ Penny) — 202 Accepted
POST /api/webhook/{source}    — Generic webhook (→ Johnny) — 202 Accepted
WS   /ws                      — WebSocket event bus (live dashboard)
```

Auth: `Authorization: Bearer <api_key>` header (or `?key=` query param for WebSocket).

### Webhook Signature Verification

GitHub and Stripe webhooks are verified before processing. Set secrets in your environment:

```
GITHUB_WEBHOOK_SECRET=whsec_...      # GitHub webhook secret from repo/org settings
STRIPE_WEBHOOK_SECRET=whsec_...      # Stripe webhook endpoint secret
```

- **GitHub**: verifies `X-Hub-Signature-256` header (HMAC-SHA256). Requests without a valid signature get `403 Forbidden`.
- **Stripe**: verifies `Stripe-Signature` header using the `t=...,v1=...` format. Invalid signatures get `403 Forbidden`.
- If no secret is configured, verification is skipped (useful for development). Set the secret to enforce it in production.

All webhook handlers return `202 Accepted` immediately and process asynchronously — so slow agent responses never block the webhook server.

## Reliability & Security

| Feature | Behaviour |
|---------|-----------|
| **Webhook auth** | GitHub verifies `X-Hub-Signature-256`; Stripe verifies `t=...,v1=...` — both HMAC-SHA256. 403 on failure. |
| **Async webhooks** | All webhook handlers return 202 immediately; agent processing happens in a background task. |
| **Circuit breaker** | Heartbeat jobs pause after 3 consecutive failures; resume automatically on next success. |
| **DB integrity** | `PRAGMA integrity_check` runs on startup before any schema init. Errors logged clearly. |
| **Log retention** | Daily agent logs older than `log_retention_days` (default: 30) are garbage-collected nightly. |
| **Context priority** | SOUL + steering always included. Low-priority blocks (vision, playbooks) trimmed first when tight. |
| **MCP health** | `/api/agents` includes `mcp_health` for each agent — detects unresolved `${ENV_VAR}` placeholders. |
| **Correlation IDs** | Every agent call is tagged with a short UUID in logs for end-to-end tracing. |
| **Bearer auth** | All API endpoints (except `/api/health` and `/`) require `Authorization: Bearer <api_key>` when `api_key` is set. |

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
  api_bind: "0.0.0.0"             # All interfaces (Tailscale/ZeroTier/LAN). Use 127.0.0.1 to restrict to localhost.
  dashboard_enabled: false         # Serve live agent graph at /

claude:
  binary: claude
  max_concurrent: 5               # Parallel task limit (global)
  max_budget_per_message: 0.50
  permission_mode: auto

memory:
  daily_log: true
  compaction_threshold: 50000
  max_session_age_hours: 72
  dream_enabled: true
  self_improve: true               # Enable self-assessment cycle
  log_retention_days: 30           # Delete daily logs older than this

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
    alert_channel_ids:             # Channel IDs for heartbeat alerts and improvements
      - "1234567890123456"
```

Environment variables (`.env`):
```
TELEGRAM_BOT_TOKEN=your_token
DISCORD_BOT_TOKEN=your_token
CLAUDE_DAEMON_API_KEY=your_api_key
GITHUB_TOKEN=ghp_...
SUPABASE_ACCESS_TOKEN=sbp_...

# Webhook signature secrets (set to enforce verification in production)
GITHUB_WEBHOOK_SECRET=whsec_...
STRIPE_WEBHOOK_SECRET=whsec_...
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
