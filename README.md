# claude-daemon

Persistent daemon wrapper for Claude Code. Runs a self-improving team of AI agents with individual identities, tools, memory, and autonomous initiative. Full feature parity across Telegram, Discord, HTTP API, and CLI with seamless cross-platform session handover.

## Features

- **Multi-Agent C-Suite** - 7 named agents (Johnny, Albert, Luna, Max, Penny, Jeremy, Sophie) with individual souls, roles, and domain ownership
- **MCP Server Pool (41 servers)** - Tiered shared pool of MCP servers available to all agents. Zero-config servers (fetch, git, time, memory, context7, playwright, ssh, tmux, etc.) are always on. Token-required servers auto-enable when you set the env var. Manage via `/mcp list`, `/mcp enable`, `/mcp disable`.
- **Per-Agent Model Routing** - Task-aware model selection: Albert/Luna/Max run Opus for coding and review. Johnny runs Sonnet for chat, Opus for planning. Penny/Jeremy/Sophie run Sonnet. Scheduled jobs run Haiku. Configurable per agent and per task type.
- **Graceful Model Degradation** - If Opus is rate-limited or unavailable, automatically falls back to Sonnet, then Haiku. Configurable fallback chain — no failed requests from transient capacity issues.
- **Auto-Parallel Execution** - Send multiple messages to the same agent — if busy, the daemon automatically spawns a parallel session. No `/spawn` needed. Also available as `/spawn` for explicit control.
- **Fuzzy Agent Matching** - Type `@jony` and it routes to `johnny`. Close-enough name typos resolved automatically with no error or fallback to the wrong agent.
- **Per-Agent Channels** - Bind Telegram groups or Discord channels to specific agents. Dedicated channels for Albert, Luna, etc.
- **Cross-Platform Sessions** - Start a conversation on Telegram, continue on Discord, pick up from CLI. Sessions follow the user, not the platform.
- **Mandatory Planning (Research→Plan→Execute→Verify→Report)** - For complex tasks, agents research context, plan with Opus, publish immediately, execute autonomously, verify output works, and report results. Critical instructions wrapped in `<important>` tags for reliable adherence.
- **Per-Agent Settings.json** - Autonomy-first permissions (all tools allowed) with deterministic deny rules blocking dangerous operations (`rm -rf`, `sudo`, force-push) at the CLI level. Extended thinking enabled by default. Configurable via `/thinking` and `/effort` commands.
- **Effort Level Control** - Two-dimensional control: model routing (Opus/Sonnet/Haiku) PLUS reasoning depth (low/medium/high/max). Scheduled tasks use low effort for speed; planning uses high effort for quality. Adjustable via `/effort` command.
- **Auto-Compact at 50%** - Prevents context degradation in long resumed sessions by triggering compaction at 50% context usage (CLI default waits much longer).
- **Domain Gotchas** - Per-agent failure-point documentation injected as high-priority context. Highest-signal content type per best practices research.
- **Managed Agents Backend** - Dual-backend execution: CLI subprocess for fast/cheap tasks (chat, heartbeats), Anthropic's Managed Agents API for long-running/complex tasks (planning, workflows, REM sleep). Automatic fallback to CLI if API fails. Control via `/backend` command.
- **Self-Evolution (EvolutionActuator)** - The improvement loop is now closed: weekly improvement plans generate targeted SOUL.md/AGENTS.md mutations. Safety guards (size check, protected sections, archive-before-write) prevent data loss. Starts in dry-run mode — proposals logged but not applied until you're confident. Evolution log tracks every mutation.
- **Code Optimization (Evo)** - Optional code optimization via tree search over hill-climbing. The weekly improvement plan identifies targets (`[OPTIMIZE:albert] Reduce test suite from 45s to under 30s`). Albert (or any code agent) runs [evo](https://github.com/evo-hq/evo) — an Apache 2.0 Claude Code plugin — which spawns N parallel agents in git worktrees, shares failure traces across them, measures each variant against a regression benchmark, keeps winners and discards losers. Automatically installed at daemon startup when `evo_enabled: true` (the default). No new API keys needed beyond Claude Code. Set `evo_enabled: false` to skip installation. Configurable via `evo_max_variants` and `evo_max_budget`.
- **Semantic Memory Search** - Vector embedding index (sqlite-vec) using Voyage AI's `voyage-code-3` model (1024 dims, best-in-class for code). Cosine similarity search across all memory, playbooks, reflections, and failure lessons. Hybrid search falls back to FTS5 keyword matching when semantic results are sparse. Incremental indexing via SHA-256 content hashing — only changed files are re-embedded. Configurable model, top-k, similarity threshold, and batch size. Graceful degradation — FTS5 still works without sqlite-vec or a Voyage API key.
- **Failure Analysis & Lesson Extraction** - Every failed agent task is auto-classified via Haiku ($0.02/failure): category, root cause, severity, actionable lesson. Lessons written to `shared/failure-lessons.md` for cross-agent learning. Recurring patterns surfaced in the weekly improvement plan.
- **Task Persistence** - Spawned background tasks survive daemon restarts. SQLite `task_queue` table tracks full lifecycle (pending → running → completed/failed). Stale tasks from crashed runs are auto-marked as failed on startup.
- **Crash Alerting & Watchdog** - systemd `Type=notify` with `WatchdogSec=120`. 60-second watchdog ping prevents restart on silent hangs. Crash detection on startup alerts you via Telegram/Discord. No more silent failures.
- **Self-Improvement Loop** - Weekly: agents self-assess, cross-agent learnings synthesised, improvement plan generated, evolution proposals applied, results delivered to you automatically.
- **Agent Heartbeats** - Autonomous recurring tasks: Penny audits costs at 8am, Jeremy scans security at 2am, Johnny sends morning briefings, Albert audits tech debt, Max runs quality retrospectives.
- **Workflow Engine** - Multi-step orchestration: sequential pipelines, parallel fan-out, and build-review loops (Albert builds, Luna styles, Max reviews, retry on failure)
- **Inter-Agent Communication** - Five communication modes: `[DELEGATE:name]` for one-shot handoffs, `[HELP:name]` for quick consultations, `[DISCUSS:name]` for multi-turn bilateral discussions, `[COUNCIL]` for full team deliberation, and `[OPTIMIZE:name]` to trigger evo code optimization. Agents have built-in guidance for when to use each mode. Council sessions produce synthesized decisions with rationale and action items. All discussions recorded to SQLite + markdown transcripts.
- **Shared Playbooks** - Lessons learned compound across the team via `shared/playbooks/`. Every agent reads them.
- **AI Command Center** - Multi-view glassmorphism dashboard: D3 force graph with pulsing agent nodes, live activity feed, agent fleet cards, per-agent chat with streaming responses, task queue, discussion transcripts, Chart.js analytics (cost/tokens/failures), filterable audit log, MCP settings panel, native Operations view (tasks / budgets / goals / approvals). All real-time via WebSocket — no polling.
- **Native Orchestration** - Built-in task queue, budget caps, goal tracking, and approval workflow — Paperclip-free by default. Atomic budget reservations (race-safe), two-pass enforcement where rejection beats approval threshold, re-enforced approvals that can't resurrect cancelled tasks, orphan task sweep on startup. Exposed via `/api/v1/*` and the Ops dashboard view.
- **CLI Chat** - `claude-daemon chat` opens an interactive terminal session with the daemon's agents. Route to a specific agent with `--agent albert`. Connects to the running daemon via the HTTP API — no separate process needed.
- **HTTP REST API** - Programmatic access, GitHub/Stripe webhooks, metrics endpoint, WebSocket event bus
- **Hardened Webhooks** - GitHub and Stripe webhooks verify HMAC-SHA256 signatures. Invalid requests get 403. Handlers run async (202 Accepted) so webhooks never block the HTTP server.
- **Resilient Heartbeats** - Circuit breaker pauses autonomous jobs after 3 consecutive failures. Auto-resumes on success. All delivery failures are logged — no silent drops.
- **Context Priority** - SOUL and steering instructions are never truncated. Low-priority blocks (vision, playbooks) are trimmed first when context budget is tight.
- **MCP Health Checks** - `/api/agents` reports per-server MCP status, detecting unresolved `${ENV_VAR}` placeholders in `tools.json` so misconfigured tools surface immediately.
- **DB Integrity** - SQLite `PRAGMA integrity_check` runs on startup. Corrupt databases are flagged in logs before any data is written.
- **Streaming Responses** - Live streaming to Telegram, Discord, and CLI chat with throttled message edits. Tokens appear within seconds instead of waiting for the full response.
- **SDK Persistent Sessions** - Uses the Claude Agent SDK (`@anthropic-ai/claude-agent-sdk`) to keep one persistent Claude process per agent. MCP servers and OAuth initialize once on first message (~12s). All subsequent messages reuse the warm session (~2-5s). Authenticated via `CLAUDE_CODE_OAUTH_TOKEN` from `claude setup-token` — uses your Claude Max/Pro subscription with no separate API billing. Falls back to one-shot subprocess mode if SDK is unavailable.
- **Three-Phase Dreaming** - Light sleep (signal detection), Deep sleep (nightly consolidation + per-agent memory compaction), REM sleep (weekly rewrite + self-reflection + improvement cycle)
- **Memory Validation** - REM sleep validates before overwriting MEMORY.md — rejects catastrophic data loss, logs diffs. Concurrent writes are serialized with a file lock (no silent data loss from parallel agents).
- **Full-Text Search** - FTS5-indexed conversation history for searching past interactions. Queries are automatically escaped so special characters never cause SQLite syntax errors.
- **Agent Hot-Reload** - Edit SOUL.md, IDENTITY.md, or AGENTS.md and changes take effect automatically within seconds. No restart needed. File watcher polls every 10s (configurable).
- **Safe Template Merge** - When the daemon code is updated, new `## sections` in SOUL.md/AGENTS.md templates are automatically appended to existing agent files without overwriting user customisations. Archive-before-write. Idempotent — no-op when already up to date.
- **Alert Webhooks** - Send heartbeat results, circuit breaker alerts, and update notifications to arbitrary HTTP endpoints (Slack incoming webhooks, PagerDuty, custom URLs). Not just Telegram/Discord.
- **Audit Log** - Every agent action recorded in a structured SQLite table: who did what, when, what it cost. Queryable via `/api/audit`. Covers messages, delegations, workflows, heartbeats, config reloads, and webhooks.
- **Agent Metrics** - Per-agent cost tracking, token usage, and performance metrics
- **Self-Updating Daemon** - The daemon auto-updates its own code (git pull + pip install) alongside the Claude CLI binary. No manual maintenance for editable git installs.
- **Service Files** - systemd and launchd support for true daemon operation

## Quick Install

### Prerequisites

- **Python 3.10+** and **Node.js 18+** (for Claude CLI and Agent SDK)
- **Claude CLI** installed and authenticated: `npm install -g @anthropic-ai/claude-code && claude /login`
- **Claude Max or Pro subscription** (uses OAuth — no separate API billing)

### Step 1: Install

```bash
curl -sSL https://raw.githubusercontent.com/dac-uk/claude-daemon/main/install.sh | bash
```

The script installs the Python package, Agent SDK, config templates, and system service. Idempotent — safe to run again.

### Step 2: Set up persistent sessions (recommended)

Persistent sessions keep one Claude process alive per agent. First message takes ~12s (one-time MCP/auth init), subsequent messages respond in ~2-5s instead of ~20s.

```bash
# Generate a headless OAuth token (uses your Max/Pro subscription, no extra cost)
claude setup-token

# Save it to the daemon's environment
~/.local/bin/claude-daemon env set CLAUDE_CODE_OAUTH_TOKEN=<paste-token-here>

# Restart the daemon to pick up the token
~/.local/bin/claude-daemon restart
```

**Without this step**, the daemon still works but spawns a fresh Claude process per message (~15-25s each). The interactive installer prompts for this token automatically.

### Step 3: Chat

```bash
~/.local/bin/claude-daemon chat
```

### Updating

```bash
# Manual update
./install.sh --update

# Or the daemon self-updates nightly via its scheduler
```

## Managing Environment Variables

You never need to manually edit `.env` files. Four ways to manage secrets and tokens:

**Via chat** (Telegram or Discord):
```
/setenv GITHUB_TOKEN ghp_abc123
/setenv SLACK_BOT_TOKEN xoxb-xxx
/getenv                          # See which vars are set (values masked)
```

**Via CLI:**
```bash
claude-daemon env list           # Show all vars with set/unset status
claude-daemon env set GITHUB_TOKEN=ghp_abc123
claude-daemon mcp list           # Show MCP server pool with tiers
claude-daemon mcp refresh        # Regenerate tools.json from env
```

**Via HTTP API:**
```bash
# List (values masked)
curl -H "Authorization: Bearer $KEY" http://localhost:8080/api/config/env

# Set
curl -X POST -H "Authorization: Bearer $KEY" \
  -d '{"key": "GITHUB_TOKEN", "value": "ghp_abc123"}' \
  http://localhost:8080/api/config/env
```

**Via install script** (interactive prompts on first run):
```bash
./install.sh    # Prompts for Telegram, Discord, GitHub, API key, dashboard
```

## Authentication & Persistent Sessions

The daemon supports two authentication modes:

### OAuth (Claude Max/Pro — recommended)

Uses your existing Claude subscription. No per-token API billing.

```bash
# 1. Log in to Claude (if not already)
claude /login

# 2. Generate a headless OAuth token for the daemon
claude setup-token

# 3. Save the token
claude-daemon env set CLAUDE_CODE_OAUTH_TOKEN=<paste-token-here>

# 4. Restart
claude-daemon restart
```

With the OAuth token set, the daemon uses the **Agent SDK** to keep persistent sessions per agent. First message to each agent takes ~12s (MCP initialization). Every message after that responds in ~2-5s.

**Without the token**, the daemon falls back to spawning a fresh `claude --print` subprocess per message (~15-25s each). Everything still works, just slower.

The token is valid for ~1 year. Regenerate with `claude setup-token` when it expires.

### API Key (pay-per-token)

If you prefer API billing over a subscription:

```bash
claude-daemon env set ANTHROPIC_API_KEY=sk-ant-...
claude-daemon restart
```

API key mode uses `--bare` for slightly faster subprocess startup but does not support SDK persistent sessions. Each message is billed per-token via the Anthropic API.

### Startup health check

When the daemon starts, it automatically scans all agents' MCP tool configurations for unresolved environment variables. If any are missing, it sends a notification to you via Telegram or Discord (whichever is configured) listing exactly what's missing and how to fix it:

```
Missing environment variables detected:

  - GITHUB_TOKEN (used by: albert, max, luna)
  - SLACK_BOT_TOKEN (used by: johnny)

Fix via chat:  /setenv VARIABLE_NAME your_value
Fix via CLI:   claude-daemon env set VARIABLE_NAME=your_value
```

This means you'll always know immediately if something is misconfigured — the daemon tells you proactively.

## MCP Server Pool

The daemon ships with **41 MCP servers** available as a shared pool to all agents. Servers are organized into three tiers:

| Tier | Description | In tools.json? |
|------|-------------|-----------------|
| **Tier 1 (zero-config)** | No tokens needed — always active | Yes |
| **Tier 2 (token-required)** | Auto-enables when env var is set | Yes (when configured) |
| **Tier 3 (disabled)** | Explicitly disabled by user | No |

### Available Servers

**Search & Web:**
| Server | Package | Env Var(s) | Tier |
|--------|---------|-----------|------|
| `fetch` | @modelcontextprotocol/server-fetch | *(none)* | T1 |
| `tavily` | tavily-mcp | `TAVILY_API_KEY` | T2 |
| `brave-search` | brave-search-mcp | `BRAVE_API_KEY` | T2 |
| `firecrawl` | firecrawl-mcp | `FIRECRAWL_API_KEY` | T2 |

**File & Data:**
| Server | Package | Env Var(s) | Tier |
|--------|---------|-----------|------|
| `filesystem` | @modelcontextprotocol/server-filesystem | *(none)* | T1 |
| `sqlite` | @modelcontextprotocol/server-sqlite | *(none)* | T1 |
| `excel` | excel-mcp-server | *(none)* | T1 |
| `markdownify` | markdownify-mcp | *(none)* | T1 |
| `postgres` | @modelcontextprotocol/server-postgres | `POSTGRES_URL` | T2 |
| `mongodb` | mcp-mongo-server | `MONGODB_URI` | T2 |
| `supabase` | @anthropic-ai/claude-code-supabase-mcp | `SUPABASE_ACCESS_TOKEN`, `SUPABASE_PROJECT_REF` | T2 |

**Developer:**
| Server | Package | Env Var(s) | Tier |
|--------|---------|-----------|------|
| `git` | @modelcontextprotocol/server-git | *(none)* | T1 |
| `context7` | @upstash/context7-mcp | *(none)* | T1 |
| `playwright` | @anthropic-ai/mcp-playwright | *(none)* | T1 |
| `computer-control` | @anthropic-ai/computer-use-mcp-server | *(none)* | T1 |
| `docker` | @modelcontextprotocol/server-docker | *(none)* | T1 |
| `codebase-memory` | codebase-memory-mcp | *(none)* | T1 |
| `tmux` | tmux-mcp | *(none)* | T1 |
| `github` | @anthropic-ai/claude-code-github-mcp | `GITHUB_TOKEN` | T2 |
| `sentry` | @sentry/mcp-server-sentry | `SENTRY_AUTH_TOKEN` | T2 |

**Productivity:**
| Server | Package | Env Var(s) | Tier |
|--------|---------|-----------|------|
| `slack` | @anthropic-ai/claude-code-slack-mcp | `SLACK_BOT_TOKEN`, `SLACK_TEAM_ID` | T2 |
| `gmail` | @anthropic-ai/claude-code-gmail-mcp | `GMAIL_OAUTH_CREDENTIALS` | T2 |
| `google-calendar` | @anthropic-ai/claude-code-google-calendar-mcp | `GCAL_OAUTH_CREDENTIALS` | T2 |
| `gdrive` | @modelcontextprotocol/server-gdrive | `GDRIVE_OAUTH_CREDENTIALS` | T2 |
| `notion` | @modelcontextprotocol/server-notion | `NOTION_API_KEY` | T2 |
| `linear` | linear-mcp-server | `LINEAR_API_KEY` | T2 |
| `obsidian` | obsidian-mcp | `OBSIDIAN_VAULT_PATH` | T2 |

**Analytics:**
| Server | Package | Env Var(s) | Tier |
|--------|---------|-----------|------|
| `snowflake` | mcp-snowflake-service | `SNOWFLAKE_ACCOUNT`, `SNOWFLAKE_USER`, `SNOWFLAKE_PASSWORD`, `SNOWFLAKE_WAREHOUSE` | T2 |
| `bigquery` | mcp-server-bigquery | `GOOGLE_APPLICATION_CREDENTIALS` | T2 |

**AI & Models:**
| Server | Package | Env Var(s) | Tier |
|--------|---------|-----------|------|
| `elevenlabs` | elevenlabs-mcp | `ELEVENLABS_API_KEY` | T2 |
| `huggingface` | @huggingface/mcp-server | `HF_TOKEN` | T2 |
| `replicate` | mcp-replicate | `REPLICATE_API_TOKEN` | T2 |

**Infrastructure:**
| Server | Package | Env Var(s) | Tier |
|--------|---------|-----------|------|
| `kubernetes` | mcp-k8s | *(none, uses kubeconfig)* | T1 |
| `ssh` | @aiondadotcom/mcp-ssh | *(none, reads ~/.ssh/config)* | T1 |
| `aws` | @aws-samples/mcp-server | `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_REGION` | T2 |
| `cloudflare` | @cloudflare/mcp-server-cloudflare | `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID` | T2 |
| `vercel` | @vercel/mcp | `VERCEL_TOKEN` | T2 |

**Utility:**
| Server | Package | Env Var(s) | Tier |
|--------|---------|-----------|------|
| `puppeteer` | @modelcontextprotocol/server-puppeteer | *(none)* | T1 |
| `time` | @modelcontextprotocol/server-time | *(none)* | T1 |
| `memory` | @modelcontextprotocol/server-memory | *(none)* | T1 |
| `sequential-thinking` | @modelcontextprotocol/server-sequential-thinking | *(none)* | T1 |

### Managing MCP Servers

**Enable a server** — just set its env var:
```
/setenv TAVILY_API_KEY tvly-abc123
/mcp refresh
```

**View all servers:**
```
/mcp list          # via Telegram/Discord
claude-daemon mcp list  # via CLI
GET /api/mcp       # via HTTP API
```

**Disable a server:**
```
/mcp disable snowflake
```

**Re-enable a server:**
```
/mcp enable snowflake
```

**Soft refresh** (regenerate tools.json without restart):
```
/mcp refresh
```

When you `/setenv` a token that enables an MCP server, the daemon prompts you to refresh.

### Adding New Servers in the Future

To add a new MCP server, add one entry to `MCP_SERVER_CATALOG` in `src/claude_daemon/agents/bootstrap.py` and its env vars to `KNOWN_ENV_VARS` in `env_manager.py`. It will automatically appear in `/mcp list` and be included in agents' tools.json when configured.

## Manual Setup

If you prefer to set things up yourself:

```bash
pip install -e ".[all]"           # All integrations
# pip install -e ".[managed]"     # Adds anthropic SDK for Managed Agents backend

mkdir -p ~/.config/claude-daemon
cp config.example.yaml ~/.config/claude-daemon/config.yaml
cp .env.example .env
# Edit .env with your bot tokens and MCP credentials

claude-daemon start --foreground   # Development (logs to terminal, Ctrl+C to stop)
claude-daemon start                # Background daemon (managed by systemd/launchd)
claude-daemon stop                 # Stop the daemon
claude-daemon restart              # Restart after config changes
claude-daemon status               # Check status
claude-daemon logs --follow        # View logs
claude-daemon chat                 # Interactive chat from terminal
claude-daemon chat --agent albert  # Chat with a specific agent
```

## The Agent Team

On first run, the daemon bootstraps a 7-agent C-suite team. Each agent has its own workspace, tools, memory, and heartbeat tasks.

| Agent | Title | Emoji | Chat | Planning | Scheduled | Domain |
|-------|-------|-------|------|----------|-----------|--------|
| **johnny** | CEO | 🎯 | Sonnet | Opus | Haiku | Orchestration, briefing, routing. Never codes. Council convener. |
| **albert** | CIO | 🧠 | Opus | Opus | Haiku | Architecture, backend, data models, APIs, services, business logic |
| **luna** | Head of Design | 🎨 | Opus | Opus | Haiku | All UI/views, layout, typography, colour, animation, design systems |
| **max** | CPO | 🔬 | Opus | Opus | Haiku | QA, product review, functional + visual testing, holistic quality |
| **penny** | CFO | 💰 | Sonnet | Opus | Haiku | Token spend, API costs, ROI analysis, financial modelling |
| **jeremy** | CRO | 🛡️ | Sonnet | Opus | Haiku | Fraud, cybersecurity, operational risk, compliance |
| **sophie** | CLO | ⚖️ | Sonnet | Opus | Haiku | Legal research, regulatory analysis, commercial counsel |

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
| `/setenv` | Set an environment variable (e.g. `/setenv GITHUB_TOKEN ghp_...`) |
| `/getenv` | Show which env vars are set/unset (values are masked) |
| `/mcp` | List all MCP servers with tier and status |
| `/mcp enable X` | Enable a disabled MCP server |
| `/mcp disable X` | Disable an MCP server |
| `/mcp refresh` | Regenerate MCP configs from current env vars |
| `/thinking on\|off` | Toggle extended thinking for all agents |
| `/effort low\|medium\|high\|max` | Set reasoning depth (low=fast/cheap, high=deep) |
| `/backend` | Show Managed Agents backend status |
| `/backend on\|off` | Enable/disable Managed Agents for configured task types |

Send any message to chat with the active agent (Johnny by default). Use `@agent_name` at the start of a message to address a specific agent.

## Native Orchestration

The daemon ships with a built-in orchestration layer — task queue, budget caps, goal tracking, and approval workflow — exposed via `/api/v1/*` and the Command Center's "Operations" view. You don't need Paperclip for any of this; the legacy Paperclip integration below is kept for existing deployments.

### What it covers

| Feature | Where it lives |
|---------|----------------|
| Task submission / cancel / inspect | `POST /api/v1/tasks`, `POST /api/v1/tasks/{id}/cancel`, `GET /api/v1/tasks/{id}` |
| Pending + recent task lists | `GET /api/v1/tasks/pending`, `GET /api/v1/tasks/recent` |
| Budget caps (global / agent / user / task_type × daily / weekly / monthly / lifetime) | `GET|POST|PUT|DELETE /api/v1/budgets` |
| Goals with parent / child + progress rollups | `GET|POST|PUT|DELETE /api/v1/goals`, `GET /api/v1/goals/{id}/progress` |
| Approval inbox | `GET /api/v1/approvals`, `POST /api/v1/approvals/{id}/approve|reject` |
| Paperclip heartbeat compat | `POST /api/paperclip/heartbeat` — adapts the old payload to `submit_task()` |

All endpoints authenticate with the daemon's Bearer key (`CLAUDE_DAEMON_API_KEY`).

### Task lifecycle

```
POST /api/v1/tasks
   │
   ├─ enforce_budget(): rejection beats approval_required
   │
   ├─ outcome == "rejected"           → 400 {status: "rejected", error}
   ├─ outcome == "approval_required"  → 202 {status: "pending_approval", task_id}
   │     ├─ task_queue row inserted directly as `pending_approval`
   │     ├─ approvals row created
   │     ├─ reservations released (freed during human wait)
   │     └─ `approval_requested` websocket event fired
   │
   └─ outcome == "allowed"            → 201 {status: "pending", task_id}
         ├─ reservations stashed on task metadata
         ├─ orchestrator.spawn_task() dispatched
         └─ on completion: apply_actual_spend() reconciles reserve→actual
             on cancel/fail: release_reservations() refunds in full
```

### Budgets

A budget has a scope, a limit, and a period. Multiple budgets stack: a task subject to both a global budget and a per-agent budget must fit inside *both*.

```bash
# $1.00/day global cap, $0.05 triggers approval
curl -H "Authorization: Bearer $KEY" -XPOST http://localhost:8080/api/v1/budgets \
  -d '{"scope":"global","limit_usd":1.00,"period":"daily","approval_threshold_usd":0.05}'

# $0.25/day per-agent cap for Albert
curl -H "Authorization: Bearer $KEY" -XPOST http://localhost:8080/api/v1/budgets \
  -d '{"scope":"agent","scope_value":"albert","limit_usd":0.25,"period":"daily"}'
```

Reservation is atomic: two concurrent submits against a $1 budget with $0.02 left will see exactly one succeed. Actual cost is reconciled after the task completes (delta = `actual_cost - reserved`), so a reservation never double-counts with the post-completion spend.

### Goals

Goals group related tasks. Attach a task to a goal via `goal_id` at submission time. Progress is computed on-demand:

```bash
curl -H "Authorization: Bearer $KEY" -XPOST http://localhost:8080/api/v1/goals \
  -d '{"title":"Ship billing v2","owner_agent":"albert","target_date":"2026-05-01"}'

curl -H "Authorization: Bearer $KEY" http://localhost:8080/api/v1/goals/1/progress
# {"total": 12, "completed": 7, "failed": 1, "running": 2, "pending": 2, "pct": 58.3, "total_cost": 2.14}
```

Deleting a parent nulls its children's `parent_goal_id` — children survive, just become top-level.

### Approvals

When a budget's `approval_threshold_usd` trips, the task enters `pending_approval` and surfaces in the approval inbox. Approve / reject are atomic and idempotent:

```bash
# Reviewer sees the queue
curl -H "Authorization: Bearer $KEY" http://localhost:8080/api/v1/approvals?pending=1

# Approve — re-runs enforcement (skipping the threshold); returns 409 if budget was drained during the wait
curl -H "Authorization: Bearer $KEY" -XPOST \
  http://localhost:8080/api/v1/approvals/42/approve \
  -d '{"approver":"alice"}'

# Reject — marks the task cancelled
curl -H "Authorization: Bearer $KEY" -XPOST \
  http://localhost:8080/api/v1/approvals/42/reject
```

Key semantics:

- **Re-enforcement on approve.** The user's approval isn't a budget bypass — the daemon re-checks every applicable budget before dispatch. If a hard cap was drained during the wait, approve returns 409 and the task stays `pending_approval`.
- **Atomic state transitions.** Each transition is guarded on the expected prior state (`WHERE status='pending_approval'`). If a task was cancelled between "user clicks approve" and dispatch, approve returns false and the approval is marked `stale` — no ghost revivals.
- **Cancel resolves the approval.** Cancelling a `pending_approval` task also rejects the linked approval row, so the inbox never orphans.
- **Rejection always wins.** If one applicable budget is exhausted and another only trips a threshold, the result is `rejected`, not `approval_required`.
- **Orphan sweep.** On daemon start, any `running` / `pending` task without a live in-memory worker is marked `failed` ("daemon restarted — orphan task") and its reservations are released. `pending_approval` rows are untouched — they're legitimately awaiting a human.

### Operations view

The Command Center's **Ops** tab shows everything above in one screen: task queue with filter chips (including amber `pending_approval`), circular budget gauges, goal progress cards, and the approval inbox. All panels update live over the websocket — no polling.

Events you can subscribe to from `/ws`:

| Event | When it fires |
|-------|---------------|
| `task_created` | On every submit (both `pending` and `pending_approval`) |
| `task_update` | On status transitions and completion |
| `task_cancelled` | On cancel |
| `budget_update` | On reservation / release / apply_actual_spend |
| `budget_exceeded` | When enforce_budget returns `rejected` |
| `goal_update` | On goal CRUD |
| `goal_progress` | When a linked task completes |
| `approval_requested` | `(approval_id, task_id, reason)` — fires on `pending_approval` |
| `approval_resolved` | `(approval_id, task_id, outcome, approver)` — fires on approve / reject |

### HTTP status codes

| Status | Meaning |
|--------|---------|
| 201 | Task accepted, dispatched |
| 202 | Task accepted, awaiting approval |
| 400 | Bad request or budget rejected outright |
| 404 | Task / approval / goal / budget not found |
| 409 | Approval already resolved, or budget drained during approval wait |
| 500 | DB persistence or dispatch failure |

## Paperclip Integration

[Paperclip](https://paperclip.ing/) is an open-source orchestration platform that organises AI agents into an autonomous company with goals, budgets, org charts, and governance. It is **no longer required** — the native orchestration layer above covers the same surface area and is the default. Paperclip integration is kept for users with existing deployments.

The daemon integrates with Paperclip in two complementary modes:

**Mode 1 — Polling (daemon pulls tasks):** The daemon polls Paperclip's API for pending tasks every N seconds, processes them through the agent team, and returns results with cost data.

**Mode 2 — Heartbeat webhook (Paperclip pushes tasks):** Paperclip POSTs tasks directly to your daemon's webhook endpoint. This is Paperclip's canonical "heartbeat" pattern — the daemon responds synchronously with the result.

Both modes run simultaneously. Heartbeat is lower latency; polling is the fallback.

### Install order

Install the daemon first (you only need Paperclip if you want the orchestration/budgeting layer). Then install Paperclip:

```bash
npx paperclipai onboard --yes    # Installs and starts Paperclip on http://localhost:3100
```

### Step-by-step setup

**Step 1 — Tell the daemon where Paperclip is:**

If Paperclip is running locally (the common case):
```bash
claude-daemon env set PAPERCLIP_URL=http://localhost:3100
```

If Paperclip is on a remote server, use that server's URL instead.

**Step 2 — Generate a Paperclip API key:**

In Paperclip's dashboard or via its API, create an API key for the daemon agent. The key is shown once at creation time — copy it immediately:
```bash
claude-daemon env set PAPERCLIP_API_KEY=pk_live_...
```

**Step 3 — (Optional) Set up the heartbeat webhook:**

For Paperclip to push tasks to the daemon (instead of the daemon polling), configure an HTTP agent in Paperclip:

| Field | Value |
|-------|-------|
| Webhook URL | `http://your-daemon-ip:8080/api/paperclip/heartbeat` |
| Auth Header | `Authorization: Bearer YOUR_DAEMON_API_KEY` |

Your daemon API key is in `~/.config/claude-daemon/.env` (`CLAUDE_DAEMON_API_KEY`). Check it with `claude-daemon env list`.

If both services run on the same machine, use `http://localhost:8080/api/paperclip/heartbeat`.

**Step 4 — Restart the daemon:**

```bash
claude-daemon restart
```

The daemon auto-registers as `claude-daemon` with Paperclip on startup.

### How agents map between systems

Paperclip sees the daemon as **one agent** ("claude-daemon") with capabilities `[code, analysis, general]`. Internally, the daemon's 7 agents handle different specialisations:

| Paperclip task field | Daemon routing |
|---------------------|----------------|
| `"agent": "albert"` | Routes directly to Albert (CIO — backend/architecture) |
| `"agent": "luna"` | Routes directly to Luna (design/UI) |
| `"agent": "max"` | Routes directly to Max (QA/product review) |
| No agent specified | Johnny (orchestrator) auto-routes based on content |

Example Paperclip task targeting a specific agent:
```json
{"prompt": "Review the auth module for security issues", "agent": "max"}
```

This is intentional — Paperclip manages goals, budgets, and governance at the company level; the daemon manages internal agent specialisation. You don't need to register 7 separate agents in Paperclip.

### Cost tracking

Every task completion reports cost data back to Paperclip so it can enforce budgets:
```json
{
  "result": "Review complete. Found 3 issues...",
  "agent": "max",
  "cost_usd": 0.12,
  "input_tokens": 2400,
  "output_tokens": 800
}
```

### Configuration reference

```yaml
integrations:
  paperclip:
    enabled: false              # Auto-enables when PAPERCLIP_URL is set
    poll_interval: 5            # Seconds between task polls
    task_limit: 5               # Max tasks per poll cycle
    startup_timeout: 30         # Seconds to wait for registration
    # URL and API key loaded from env vars:
    #   PAPERCLIP_URL — e.g. http://localhost:3100
    #   PAPERCLIP_API_KEY — generated in Paperclip's dashboard
```

## Remote Access

If the daemon and/or Paperclip run on a home server (e.g. Mac Studio), you can manage them from a laptop, phone, or any other device.

### Same network (LAN/Wi-Fi)

Access the dashboards directly by IP:

| Service | URL | What it shows |
|---------|-----|---------------|
| Daemon dashboard | `http://mac-studio-ip:8080/` | Agents, activity feed, chat, analytics |
| Paperclip dashboard | `http://mac-studio-ip:3100/` | Goals, budgets, org chart, governance |

Find your server's IP with `ifconfig | grep inet` (macOS) or `hostname -I` (Linux).

### Outside your network (mobile data, remote laptop)

**Option 1 — Tailscale (recommended):** Install [Tailscale](https://tailscale.com/) on your server and your devices. Access via the Tailscale IP (e.g. `http://100.x.x.x:8080/`). Encrypted, zero port forwarding, works from anywhere. Free for personal use.

**Option 2 — Cloudflare Tunnel:** `cloudflared tunnel` gives you a public HTTPS URL (e.g. `https://daemon.your-domain.com`) without opening router ports. Requires a Cloudflare account and domain.

**Option 3 — Port forwarding:** Open ports 8080 and 3100 on your router. Not recommended — no encryption, exposes services to the internet.

### Access from any device (with Tailscale)

| From | How |
|------|-----|
| Laptop browser | `http://100.x.x.x:8080/` (daemon) or `:3100` (Paperclip) |
| iPhone/Android browser | Same URLs — dashboards are responsive |
| Laptop terminal | `ssh user@100.x.x.x` then `claude-daemon chat` |
| Telegram/Discord | Works from anywhere (bots connect outbound, no port needed) |

Telegram and Discord don't require any port forwarding or VPN — they connect outbound from your server to the messaging platform's API.

## Channel Setup Guide

This section walks you through connecting the daemon to Telegram and/or Discord from scratch. You don't need both — pick whichever you use.

### How messages get routed

Every message goes through three checks in order:

1. **Is this channel bound to an agent?** If yes, that agent handles it. No @mention needed.
2. **Does the message start with `@agent_name`?** If yes, that specific agent handles it.
3. **Neither?** Johnny (the orchestrator) receives it and decides which agent should handle it.

**Tip:** Bind your main/general channel to `johnny`. That way Johnny orchestrates everything in that channel and you never need to @mention the bot — just type naturally.

---

### Telegram

#### Step 1: Create a Telegram bot

1. Open Telegram and search for **@BotFather**
2. Send `/newbot` and follow the prompts — pick a name and username
3. BotFather gives you a token that looks like: `7123456789:AAF1234567890abcdefghijklmnop`
4. Copy it

#### Step 2: Add the token

The install script (`./install.sh`) will prompt you for this token automatically. If you already ran the installer, add it manually:

Open `~/.config/claude-daemon/.env` and set:
```
TELEGRAM_BOT_TOKEN=7123456789:AAF1234567890abcdefghijklmnop
```

#### Step 3: Find your user ID (recommended)

Restricting who can talk to the bot prevents strangers from using it.

1. Open Telegram, search for **@userinfobot**, and start a chat
2. It replies with your numeric user ID (e.g. `123456789`)

#### Step 4: Create your channels

Create Telegram groups for each agent you want a dedicated channel for. You need the **chat ID** of each group:

1. Create a group in Telegram (e.g. "AI General", "Albert CIO", "Luna Design")
2. Add your bot to each group
3. Add **@userinfobot** to the group — it will post the group's chat ID (a negative number like `-1001234567890`)
4. Remove @userinfobot after noting the ID

#### Step 5: Configure

Open `~/.config/claude-daemon/config.yaml` and set up the `telegram` section:

```yaml
integrations:
  telegram:
    # Your Telegram user ID — only you can talk to the bot
    # Leave empty [] to allow everyone (not recommended)
    allowed_user_ids:
      - 123456789

    # Map each group to an agent. The bot responds to ALL messages
    # in these groups — no @mention needed.
    agent_channels:
      "-1001234567890": "johnny"     # Your main group — Johnny orchestrates
      "-1009876543210": "albert"     # Albert handles architecture/backend
      "-1001111111111": "luna"       # Luna handles UI/design
      "-1002222222222": "max"        # Max handles QA/testing
```

**That's it for Telegram.** Restart the daemon and send a message in any of those groups. The bot responds immediately — no @mention required.

On Telegram, the bot listens to **all messages** from authorised users in any group it's a member of. Even groups not listed in `agent_channels` work — they route through Johnny by default.

You can always override the channel binding by starting your message with `@agent_name`:
```
@luna redesign the login page     # Goes to Luna, even in Albert's group
```

---

### Discord

#### Step 1: Create a Discord bot

1. Go to the **Discord Developer Portal**: `discord.com/developers/applications`
2. Click **New Application**, give it a name, click **Create**
3. In the left sidebar, click **Bot**
4. Click **Reset Token** and copy the token — you'll only see it once
5. Scroll down to **Privileged Gateway Intents** and enable **Message Content Intent** (required)
6. Click **Save Changes**

#### Step 2: Invite the bot to your server

1. In the Developer Portal, click **OAuth2** in the left sidebar
2. Under **Scopes**, tick **bot**
3. Under **Bot Permissions**, tick:
   - Send Messages
   - Read Message History
   - Embed Links
4. Copy the generated URL at the bottom and open it in your browser
5. Select your server and click **Authorize**

#### Step 3: Add the token

The install script (`./install.sh`) will prompt you for this token automatically. If you already ran the installer, add it manually:

Open `~/.config/claude-daemon/.env` and set:
```
DISCORD_BOT_TOKEN=MTIzNDU2Nzg5MDEyMzQ1Njc4OQ.AbCdEf.GhIjKlMnOpQrStUvWxYz
```

#### Step 4: Get channel and server IDs

You need to enable **Developer Mode** in Discord to copy IDs:

1. Open Discord Settings (gear icon next to your name)
2. Go to **App Settings > Advanced**
3. Turn on **Developer Mode**

Now you can right-click things to copy their IDs:
- **Server ID**: Right-click the server name at the top of the channel list > **Copy Server ID**
- **Channel ID**: Right-click any channel > **Copy Channel ID**

#### Step 5: Configure

Open `~/.config/claude-daemon/config.yaml` and set up the `discord` section:

```yaml
integrations:
  discord:
    # Lock the bot to your server only
    allowed_guild_ids:
      - 987654321098765432           # Your server ID

    # Map channels to agents. The bot responds to ALL messages
    # in bound channels — no @mention needed.
    agent_channels:
      "1100000000000000000": "johnny"    # #general — Johnny orchestrates
      "1100000000000000001": "albert"    # #albert-cio — architecture/backend
      "1100000000000000002": "luna"      # #luna-design — UI/design
      "1100000000000000003": "max"       # #max-qa — testing/quality
      "1100000000000000004": "penny"     # #penny-finance — costs/budgets
      "1100000000000000005": "jeremy"    # #jeremy-security — security
      "1100000000000000006": "sophie"    # #sophie-legal — legal/regulatory

    # Channel for system alerts, heartbeat results, improvement plans
    alert_channel_ids:
      - "1100000000000000007"            # #agent-alerts
```

**Important:** On Discord, the bot **only responds** in channels that are either:
- Bound to an agent in `agent_channels` (responds to all messages, no @mention)
- Where someone @mentions the bot directly

If you want a channel where you can just type freely without @mentioning, **bind it to an agent** — even if it's `johnny` for general use.

DMs always work without any config.

#### Recommended Discord server layout

```
#general            → Bind to "johnny" (orchestrates, routes to specialists)
#albert-cio         → Bind to "albert" (architecture, backend, APIs)
#luna-design        → Bind to "luna" (UI, design systems, styling)
#max-qa             → Bind to "max" (testing, quality, reviews)
#penny-finance      → Bind to "penny" (costs, budgets, billing)
#jeremy-security    → Bind to "jeremy" (security, compliance, risk)
#sophie-legal       → Bind to "sophie" (legal, regulatory)
#agent-alerts       → Alert channel (automated notifications)
```

The same structure works with Telegram groups.

---

### Using both platforms

Sessions follow the user, not the platform. You can start a conversation with Albert on Telegram and continue it on Discord (or the HTTP API, or CLI). The agent remembers the context.

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
| `DIRECTIVE.md` | Team operating directive — injected as Tier 1 (never truncated) into all agents |
| `USER.md` | Your profile — fill in your name/role/style on first run; all agents read this |
| `playbooks/` | Cross-agent lessons learned (compounding knowledge) |
| `learnings.md` | Weekly synthesis of cross-agent insights |
| `events.md` | Auto-maintained agent activity log (inter-agent awareness) |
| `steer/` | Mid-task steering files (e.g. `steer/albert.md`) |
| `checklists/` | QA templates and quality gate checklists |
| `discussions/` | Inter-agent discussion transcripts (bilateral + council) |
| `template-archive/` | Timestamped backups of SOUL.md before template merges |

Edit any `.md` file directly to change an agent's behaviour. No restart needed. On daemon updates, new `## sections` from updated templates are merged into existing agent SOUL.md files without overwriting your customisations (archive-before-write, append-only).

## Self-Improvement Loop

Agents continuously learn and improve without being prompted:

**Weekly cycle (Sunday 5 AM):**
1. **REM Sleep** — Global MEMORY.md rewritten from weekly signals
2. **Per-Agent Self-Assessment** — Each agent evaluates their own performance: rating, strengths, struggles, skills to develop, tools needed, process improvements
3. **Cross-Agent Learning Synthesis** — All reflections aggregated into `shared/learnings.md`, injected into every agent's future context
4. **Improvement Plan** — Reads reflections + metrics + failure patterns + playbooks, generates prioritised proposals with owners and ROI
5. **Self-Evolution** — EvolutionActuator turns plan into targeted SOUL.md/AGENTS.md mutations. Archive-before-write, size guards, protected sections. Dry-run mode by default — set `evolution_dry_run: false` once you trust it.
6. **Proactive Delivery** — Improvement suggestions + evolution results sent to you via Slack/Telegram automatically

**Continuous failure learning:**
- Every failed agent task is auto-analyzed by the `FailureAnalyzer` (Haiku, ~$0.02)
- Failures classified: rate_limit, timeout, tool_error, logic_error, context_overflow, permission_denied
- Lessons extracted and written to `shared/failure-lessons.md`
- Recurring patterns surfaced in next week's improvement plan

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

Each step has a configurable **timeout** (default: 600s). If a step takes longer, it fails cleanly and the workflow stops — no hung pipelines.

Workflows accept an optional **cost cap** (`max_total_cost`). If the accumulated spend across all steps hits the cap, remaining steps are skipped and the workflow reports the breach. Set `max_total_cost=0` (the default) for unlimited.

```python
# Example via HTTP API
POST /api/workflow
{
  "steps": [
    {"agent": "albert", "prompt": "build the feature", "timeout": 300},
    {"agent": "luna",   "prompt": "style it: {prev_result}", "timeout": 120},
    {"agent": "max",    "prompt": "review: {prev_result}", "timeout": 120}
  ],
  "max_total_cost": 2.00   # Stop if total cost exceeds $2
}
```

## Inter-Agent Communication

Agents communicate using four modes, from lightweight to heavyweight:

| Tag | Mode | When to Use |
|-----|------|-------------|
| `[DELEGATE:name] message` | One-shot handoff | Clear task for another agent |
| `[HELP:name] question` | Quick consultation | Specific question, fact-check, sanity check |
| `[DISCUSS:name] topic` | Bilateral discussion | Align on approach, cross-domain uncertainty |
| `[COUNCIL] topic` | Full council | High-stakes, multi-domain, architecture decisions |

**Bilateral discussions** alternate turns between two agents until they converge (either agent says "CONSENSUS"), hit the cost cap, or exhaust the turn limit. Each agent sees the full growing transcript.

**Council sessions** convene all agents (or a specified subset). Each agent speaks once per round, seeing all previous responses. The orchestrator (Johnny) synthesizes a final decision with rationale, dissent notes, and action items.

All discussions are recorded to the `discussions` SQLite table and as markdown transcripts in `shared/discussions/`.

### Cost Controls

| Guard | Default |
|-------|---------|
| Per-discussion cost cap | $1.00 bilateral, $2.00 council |
| Turn limit | 6 bilateral, 2 rounds × N agents for council |
| Early convergence | "CONSENSUS" keyword stops early |
| Per-agent daily budget | Still enforced per-turn |
| Global toggle | `discussions_enabled: true` |

```yaml
# config.yaml
claude:
  discussions_enabled: true
  discussion_max_turns: 6
  discussion_max_cost: 1.00
  council_max_cost: 2.00
  council_max_rounds: 2
```

### Built-In Guidance

Every agent's system context includes a decision guide:
- Simple task for another agent → **DELEGATE**
- Quick question → **HELP**
- Need to align on approach → **DISCUSS**
- High-stakes or multi-domain → **COUNCIL**
- Unsure? Start with HELP, escalate to DISCUSS if needed

Discussion insights (stats, recent topics, convergence rates) feed into the weekly improvement cycle.

## AI Command Center

A multi-view glassmorphism dashboard serving as mission control for the agent fleet. Dark theme, real-time WebSocket updates, no polling.

```yaml
daemon:
  api_enabled: true
  api_port: 8080
  dashboard_enabled: true
  api_bind: "0.0.0.0"    # Accessible from Tailscale/ZeroTier/LAN
```

Open `http://<your-ip>:8080/` in any browser. Works on any device on your Tailscale or ZeroTier network.

### Views

| View | What it shows |
|------|---------------|
| **Overview** | D3 force graph (agent nodes with emoji, glow rings, pulse on busy), live activity feed, agent sidebar, metric cards |
| **Agents** | Fleet cards with status, model, cost, MCP health, heartbeat count. Click to open streaming output panel |
| **Chat** | Per-agent chat channels (team + individual agents). Live streaming responses via SSE. Cross-session message history |
| **Tasks** | Active task queue + inter-agent discussion transcripts (bilateral + council). Expandable cards with synthesis |
| **Ops** | Native orchestration: task queue (filter chips for pending / running / pending_approval / completed / failed / cancelled), budget gauges, goal cards, approval inbox. See [Native Orchestration](#native-orchestration). |
| **Analytics** | Chart.js visualizations: cost by agent, token usage (input/output), task outcomes, failure categories |
| **Activity** | Filterable audit log with pagination. Filter by agent, action type. Search details text |
| **Settings** | MCP server pool table (tier, status, description), system info |

### Preserved Features
- **Force graph**: 7 agent nodes with colour-coded status, emoji labels, role sublabels, pulsing animation when busy, glow rings on active agents
- **Click to expand**: Click any agent node or card to see their live thought stream in a slide-in panel
- **Event log**: Scrolling real-time log of agent status changes, task completions, auto-parallel events, discussions, delegations
- **WebSocket**: All updates via `/ws` — no polling (30s metrics refresh only)
- **Stats bar**: Active agents, session count, cost today

### WebSocket events

The `/ws` endpoint streams all dashboard events. Orchestration events are listed in the [Native Orchestration](#native-orchestration) section. General events:

| Event | When it fires |
|-------|---------------|
| `agent_status` | Agent goes busy (with prompt) or idle (with cost, duration) |
| `stream_delta` | Token chunk from agent output (live streaming) |
| `auto_parallel` | Parallel session auto-spawned for a busy agent |
| `metrics_tick` | Periodic (30s) per-agent cost/token summary |

### Architecture
12 files — CSS design system + 8 JS modules + HTML shell. D3.js and Chart.js loaded from CDN. No build step, no npm, no bundler.

## HTTP API

Enable with `api_enabled: true`. Exposes the daemon for external automation.

```
GET  /api/health              — Health check (always public)
GET  /api/agents              — List agents with roles, models, MCP status
GET  /api/status              — Daemon status and metrics
GET  /api/sessions            — Active Claude subprocesses
GET  /api/tasks               — Spawned background tasks
GET  /api/metrics             — Per-agent cost/token metrics
GET  /api/audit               — Structured audit log (filter by action, agent, paginate)
GET  /api/config/env          — List env vars with set/unset status (masked values)
POST /api/config/env          — Set an env var {"key": "...", "value": "..."}
GET  /api/mcp                 — List all MCP servers with tier and status
POST /api/mcp/enable          — Enable a disabled server {"server": "tavily"}
POST /api/mcp/disable         — Disable a server {"server": "snowflake"}
POST /api/mcp/refresh         — Regenerate tools.json for all agents
POST /api/settings/thinking   — Toggle extended thinking {"enabled": true/false}
POST /api/settings/effort     — Set effort level {"level": "low/medium/high/max"}
GET  /api/settings/backend    — Get Managed Agents backend status
POST /api/settings/backend    — Enable/disable Managed Agents {"enabled": true/false}
GET  /api/discussions         — Inter-agent discussion history (filter by type, initiator)
GET  /api/failures            — Failure analyses and patterns (filter by agent)
GET  /api/evolution           — Self-evolution mutation history (filter by agent)
POST /api/message             — Send a message to an agent (returns final result)
POST /api/message/stream      — Stream response token-by-token (Server-Sent Events)
POST /api/workflow            — Trigger build quality gate workflow (accepts max_cost)
POST /api/webhook/github      — GitHub webhook (→ Max/Albert/Johnny) — 202 Accepted
POST /api/webhook/stripe      — Stripe webhook (→ Penny) — 202 Accepted
POST /api/webhook/{source}    — Generic webhook (→ Johnny) — 202 Accepted

# Native Orchestration (see "Native Orchestration" section for full semantics)
POST /api/v1/tasks                  — Submit a task (201 pending | 202 pending_approval | 400 rejected)
GET  /api/v1/tasks/pending          — List pending + running + pending_approval tasks
GET  /api/v1/tasks/recent           — Most-recent tasks regardless of status
GET  /api/v1/tasks/{id}             — Inspect a single task (DB + live state)
POST /api/v1/tasks/{id}/cancel      — Cancel, release reservations, resolve approval
GET|POST|PUT|DELETE /api/v1/budgets — Budget CRUD
GET|POST|PUT|DELETE /api/v1/goals   — Goal CRUD
GET  /api/v1/goals/{id}/progress    — Aggregate progress from linked tasks
GET  /api/v1/approvals              — Approval inbox (?pending=1 for pending only)
POST /api/v1/approvals/{id}/approve — Re-enforces budget, dispatches (or 409 if drained)
POST /api/v1/approvals/{id}/reject  — Cancels linked task

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
| **Circuit breaker** | Heartbeat jobs pause after 3 consecutive failures; resume automatically on next success. State persists to `.circuit_breaker.json` — a paused job stays paused across daemon restarts. |
| **File-locked memory** | All MEMORY.md writes use `fcntl.LOCK_EX`. Concurrent agent writes are serialized — no silent overwrites or data loss when multiple agents update memory simultaneously. |
| **Fuzzy agent routing** | `@jony` resolves to `johnny` via difflib (similarity ≥ 0.6). Unknown names that are close enough auto-correct; genuinely unknown names fall through to the orchestrator gracefully. |
| **FTS5 safe search** | User search queries are escaped before hitting SQLite FTS5 — each word is quoted to neutralize special characters (`*`, `"`, `-`). No syntax errors from user input. |
| **Session race safety** | Concurrent session creation for the same user uses `INSERT OR IGNORE` + re-fetch. Two rapid parallel requests never produce duplicate conversations. |
| **Integration startup timeout** | Telegram and Discord connectors have a 30s startup timeout. A hung authentication attempt doesn't block the daemon from starting or serving other integrations. |
| **DB integrity** | `PRAGMA integrity_check` runs on startup before any schema init. Errors logged clearly. |
| **Log retention** | Daily agent logs older than `log_retention_days` (default: 30) are garbage-collected nightly. |
| **Context priority** | SOUL + steering always included. Low-priority blocks (vision, playbooks) trimmed first when tight. |
| **MCP health** | `/api/agents` includes `mcp_health` for each agent — detects unresolved `${ENV_VAR}` placeholders. |
| **MCP tiered pool** | 41 MCP servers in a shared pool. Zero-config servers always active; token-required auto-enable when vars set. Managed via `/mcp list/enable/disable/refresh`. |
| **Model fallback** | Rate-limited or unavailable models automatically retry with the next model in the chain (default: opus -> sonnet -> haiku). Configurable via `model_fallback_chain`. |
| **Agent hot-reload** | File watcher polls every 10s for changes to IDENTITY.md, SOUL.md, AGENTS.md. Edits take effect on the next message — no daemon restart needed. |
| **Alert webhooks** | Failures, circuit breaker events, and updates are POSTed as JSON to configured webhook URLs (Slack, PagerDuty, custom). Fire-and-forget with timeout — never blocks the scheduler. |
| **Audit log** | Every significant action (messages, delegations, workflows, heartbeats, config reloads, webhook receives) recorded in a structured `audit_log` SQLite table. Queryable via `GET /api/audit`. |
| **Startup env check** | On startup, all agents' MCP tools are scanned for unresolved `${ENV_VAR}` placeholders. Missing vars are reported via Telegram/Discord proactively — no silent tool failures. |
| **Schema migration** | On startup, `_migrate_schema()` detects and applies missing tables/columns. Upgrading the daemon never breaks the database. |
| **Daemon self-update** | Editable git installs: `git pull --ff-only` + `pip install -e ".[all]"` runs automatically alongside `claude update`. New dependencies are always installed. |
| **Correlation IDs** | Every agent call is tagged with a short UUID in logs for end-to-end tracing. |
| **Bearer auth** | All API endpoints (except `/api/health` and `/`) require `Authorization: Bearer <api_key>` when `api_key` is set. |
| **Agent name sanitization** | Agent names are validated against `^[a-z0-9_-]{1,30}$` before any filesystem operations. Prevents path traversal attacks via crafted agent names. |
| **Rate limiting on all paths** | The rate limiter in MessageRouter is enforced on both direct chat paths and the streaming (Telegram/Discord long-poll) paths. No bypass by using a different message delivery method. |
| **Zombie process prevention** | Timed-out Claude subprocesses are killed cleanly with `SIGTERM` followed by `await proc.wait()` to reap the zombie. No lingering undead processes accumulating over time. |
| **Session lock eviction** | Per-session asyncio locks are bounded (`max 500`). When the ceiling is hit, idle locks are evicted to prevent unbounded memory growth in long-running instances. |
| **Background task cleanup** | Spawned background tasks are tracked and cleaned up on each new spawn. Completed tasks are pruned (capped at 100 finished entries) so the task registry doesn't grow forever. |
| **Managed Agents fallback** | If the Managed Agents API fails (network, quota, beta issues), the daemon automatically falls back to CLI subprocess with a warning log. No user-visible failure for backend outages. |
| **Discussion cost caps** | Bilateral discussions capped at $1.00, council sessions at $2.00 by default. Early convergence detection ("CONSENSUS") stops discussions when agreement is reached. Per-agent daily budgets still enforced per-turn. |
| **Discussion transcripts** | All inter-agent discussions recorded to SQLite `discussions` table + markdown files in `shared/discussions/`. Full audit trail with participants, outcomes, costs, and turn-by-turn content. |
| **Crash alerting** | systemd `Type=notify` with `WatchdogSec=120`. On startup, detects unclean previous shutdown and alerts you via Telegram/Discord. 60-second watchdog ping prevents restart on silent hangs. |
| **Task persistence** | Spawned background tasks are persisted to SQLite `task_queue` before launching. On daemon restart, stale tasks are marked as failed. Full lifecycle tracking: pending → running → completed/failed. |
| **Failure post-mortems** | Every failed agent task is auto-analyzed via Haiku ($0.02): classified by category (timeout, rate_limit, tool_error, etc.), root cause extracted, actionable lesson written to `shared/failure-lessons.md`. Deduplicated by error hash. |
| **Evolution safety guards** | Self-evolution proposals are validated before application: size guard rejects <30% content reduction, `## Identity` and `## Values` sections in SOUL.md are protected (never removed), archive-before-write creates timestamped backup, dry-run mode is the default. |
| **Semantic search degradation** | If `sqlite-vec` is not installed, the `EmbeddingStore` gracefully disables itself. All methods return empty results and FTS5 keyword search continues working. Zero impact on existing functionality. |

## Architecture

### System Overview

```
┌──────────────────────────────────────────────────────────────────┐
│  Frontends                                                        │
│  Telegram · Discord · CLI chat · HTTP API · Webhooks · Dashboard │
└───────────────────────────┬──────────────────────────────────────┘
                            │
                    ┌───────▼───────┐
                    │    Daemon     │   Python async (aiohttp)
                    │  daemon.py    │   launchd/systemd service
                    └───────┬───────┘
                            │
              ┌─────────────▼─────────────┐
              │       Orchestrator        │   Routes messages to agents
              │     orchestrator.py       │   Processes delegation tags
              │                           │   [DELEGATE] [HELP] [DISCUSS]
              │  _resolve_agent()         │   [COUNCIL] [OPTIMIZE]
              │  send_to_agent()          │
              │  stream_to_agent()        │
              └─────────────┬─────────────┘
                            │
         ┌──────────────────▼──────────────────┐
         │          ProcessManager             │   Triple-backend execution
         │           process.py                │
         │                                     │
         │  1. SDK Bridge (preferred)          │   Persistent sessions
         │     └─ ~2-5s per message            │   via Agent SDK
         │  2. Managed Agents API              │   Anthropic-hosted
         │     └─ for long-running tasks       │   (optional, API key)
         │  3. CLI subprocess (fallback)       │   claude --print
         │     └─ ~15-25s per message          │   always available
         └──────────┬──────────────────────────┘
                    │
    ┌───────────────▼───────────────┐
    │       SDK Bridge Manager      │   Python ↔ Node.js NDJSON protocol
    │        sdk_bridge.py          │
    └───────────────┬───────────────┘
                    │ stdin/stdout
    ┌───────────────────▼───────────────────┐
    │       Node.js Bridge Process          │   @anthropic-ai/claude-agent-sdk
    │           sdk/bridge.js               │   v2 API (persistent sessions)
    │                                       │
    │  Sessions: Map<agent:model, SDK>      │   Keyed by (agent, model)
    │  johnny:sonnet → warm (chat)          │   MCP stays initialized
    │  johnny:opus   → warm (planning)      │   OAuth stays validated
    │  albert:opus   → warm (all tasks)     │   No per-message startup
    │  luna:opus     → warm (all tasks)     │
    │  max:opus      → warm (all tasks)     │
    │  penny:sonnet  → warm (chat/default)  │
    │  jeremy:sonnet → warm (chat/default)  │
    │  sophie:sonnet → warm (chat/default)  │
    │                                       │   8 warm sessions at startup
    └───────────────────┬───────────────────┘
                    │
    ┌───────────────▼───────────────┐
    │     Claude Code Processes     │   One per agent (persistent)
    │  MCP servers · OAuth · Model  │   Initialized once at startup
    │  41 servers available         │   Reused across all messages
    └───────────────┬───────────────┘
                    │
              ┌─────▼─────┐
              │ Anthropic  │   claude-sonnet-4-6 / opus / haiku
              │    API     │   ~3-4s round-trip per message
              └────────────┘
```

### Message Flow

Every agent gets its own persistent SDK session — not just Johnny. When you `chat --agent albert` or send a message in Albert's Telegram channel, the same fast path applies.

```
User types message (CLI / Telegram / Discord / API)
  │
  ├─ Platform handler sends to daemon
  │    CLI: POST /api/message/stream (SSE, http.client unbuffered)
  │    Telegram/Discord: bot handler → daemon.handle_message()
  │
  ├─ Daemon resolves target agent:
  │    CLI --agent albert → albert
  │    Telegram agent channel → bound agent
  │    @luna prefix → luna
  │    Default → johnny (orchestrator)
  │
  ├─ Orchestrator picks the model for task_type (chat=sonnet, planning=opus...)
  │
  ├─ Check: warm SDK session for (agent, model)?
  │    ├─ YES → send via SDK bridge (fast, ~2-5s)
  │    └─ NO  → subprocess fallback (correct model, ~15s)
  │
  │  Example routing:
  │    "hey albert" (chat, opus)     → albert:opus session ✓ (fast)
  │    "@johnny plan X" (planning)   → johnny:opus session ✓ (fast)
  │    "@johnny what's up" (chat)    → johnny:sonnet session ✓ (fast)
  │    "@penny plan budget" (plan)   → penny:opus NOT warm → subprocess
  │
  ├─ SDK bridge writes to the agent's SDKSession
  │    session.send(prompt) → session.stream()
  │
  ├─ Tokens stream back through the platform:
  │    CLI: SSE events → live word-by-word in terminal
  │    Telegram: throttled message edits (~1s intervals)
  │    Discord: throttled message edits
  │    API: SSE or buffered JSON response
  │
  └─ Result stored in SQLite + agent metrics updated
```

### Agent System

Each agent has an identity workspace at `~/.config/claude-daemon/agents/{name}/`:

```
agents/
├── johnny/          CEO · Orchestrator · Routes and delegates
│   ├── SOUL.md          Core identity, values, leadership style
│   ├── IDENTITY.md      Name, role, emoji, model routing
│   ├── AGENTS.md        Operating rules, communication tags
│   ├── MEMORY.md        Persistent memory (decisions, preferences)
│   ├── GOTCHAS.md       Failure-point documentation (high priority)
│   ├── REFLECTIONS.md   Self-assessment insights
│   ├── HEARTBEAT.md     Autonomous recurring tasks
│   ├── tools.json       MCP server configuration (41 servers)
│   └── settings.json    Permissions, thinking, deny rules
├── albert/          CIO · Backend, APIs, architecture
├── luna/            Head of Design · UI, frontend, animation
├── max/             CPO · QA, testing, product review
├── penny/           CFO · Cost tracking, financial analysis
├── jeremy/          CRO · Security, risk, compliance
└── sophie/          CLO · Legal research, regulatory
```

**Context injection** is split between static and dynamic:
- **Static** (set once at session creation): SOUL.md, IDENTITY.md, AGENTS.md, GOTCHAS.md, planning protocol, communication tags, memory
- **Dynamic** (injected per message): semantic memory matches, recent agent events, team learnings

### Scheduled Systems

```
Scheduler (APScheduler)
  ├── Dreaming
  │   ├── Light sleep: signal detection (continuous)
  │   ├── Deep sleep: nightly consolidation + memory compaction (4 AM)
  │   └── REM sleep: weekly rewrite + self-reflection (Sunday 5 AM)
  │
  ├── Self-Improvement
  │   ├── Agent self-assessments → cross-agent learning synthesis
  │   ├── Improvement plan generation → EvolutionActuator
  │   └── SOUL.md / AGENTS.md mutations (dry-run by default)
  │
  ├── Failure Analysis
  │   └── Auto-classify errors via Haiku → extract lessons → shared/
  │
  ├── Agent Heartbeats (from HEARTBEAT.md per agent)
  │   ├── Penny: cost audit (8 AM)
  │   ├── Jeremy: security scan (2 AM)
  │   ├── Johnny: morning briefing
  │   └── Albert: tech debt audit
  │
  ├── Memory Compaction (nightly per agent)
  │   └── EmbeddingStore reindex (sqlite-vec, Voyage AI)
  │
  └── Watchdog ping (60s) → sd_notify("WATCHDOG=1")
```

### Data Storage

```
~/.config/claude-daemon/
├── daemon.db          SQLite: conversations, messages, metrics, audit log, tasks
│   ├── FTS5 index     Full-text search over conversation history
│   └── agent_metrics  Per-agent cost, tokens, duration, success rate
├── agents/            Agent identity workspaces (see above)
├── shared/            Cross-agent shared state
│   ├── DIRECTIVE.md   Team operating directive (never truncated)
│   ├── events.md      Recent agent activity feed
│   ├── learnings.md   Cross-agent synthesized learnings
│   ├── playbooks/     Lessons learned (compounding knowledge)
│   ├── discussions/   Council/discussion transcripts (markdown)
│   └── failure-lessons.md   Extracted failure patterns
├── memory/            Durable memory (soul, daily logs)
├── config.yaml        Daemon configuration
└── .env               Secrets and tokens
```

### Authentication

Two modes, both handled transparently by ProcessManager:

| Mode | Env Var | Persistent Sessions | Billing |
|------|---------|-------------------|---------|
| **OAuth** (recommended) | `CLAUDE_CODE_OAUTH_TOKEN` | Yes (SDK bridge) | Claude Max/Pro subscription |
| **API Key** | `ANTHROPIC_API_KEY` | No (subprocess only) | Per-token API billing |

OAuth tokens are generated via `claude setup-token` and last ~1 year. The SDK bridge uses the token to maintain persistent Claude Code processes that stay warm between messages.

## Configuration

```yaml
daemon:
  log_level: INFO
  api_enabled: true
  api_port: 8080
  api_bind: "0.0.0.0"             # All interfaces (Tailscale/ZeroTier/LAN). Use 127.0.0.1 to restrict to localhost.
  dashboard_enabled: false         # Serve live agent graph at /
  agent_hot_reload: true           # Auto-detect agent config file changes
  agent_reload_interval: 10        # Seconds between file change polls
  alert_webhook_urls:              # URLs to receive alert POSTs
    - "https://hooks.slack.com/services/T.../B.../xxx"

claude:
  binary: claude
  max_concurrent: 5               # Parallel task limit (global)
  max_budget_per_message: 0.50
  per_agent_daily_budget: 0.0     # Max USD per agent per day. 0 = unlimited.
  model_fallback_chain:            # Fallback on rate limit (Opus -> Sonnet -> Haiku)
    - sonnet
    - haiku
  permission_mode: auto
  # disabled_mcp_servers:          # MCP servers to exclude even if configured
  #   - snowflake
  #   - bigquery
  managed_agents_enabled: false    # Opt-in (requires ANTHROPIC_API_KEY)
  managed_agents_task_types:       # Which task types route to Managed Agents API
    - planning
    - workflow
    - rem_sleep
    - improvement
  evolution_enabled: true          # Generate SOUL.md/AGENTS.md mutation proposals
  evolution_dry_run: true          # Log proposals but don't apply (safe default)
  evo_enabled: true                # Enable evo code optimization workflows
  evo_max_variants: 3              # Parallel variants per optimization run
  evo_max_budget: 2.00             # USD cost cap per evo optimization run

memory:
  daily_log: true
  compaction_threshold: 50000
  max_session_age_hours: 72
  dream_enabled: true
  self_improve: true               # Enable self-assessment cycle
  log_retention_days: 30           # Delete daily logs older than this
  embeddings_enabled: true         # Semantic search (requires sqlite-vec for full quality)
  embedding_model: voyage-code-3   # Voyage model (voyage-code-3, voyage-3, voyage-3-lite)
  embedding_dim: 1024              # Must match model dimensions
  embedding_top_k: 3               # Semantic matches injected into agent context
  embedding_similarity_threshold: 0.3  # Minimum score (0-1); below this is discarded
  embedding_chunk_size: 500        # Max chars per memory chunk

claude:
  discussions_enabled: true        # Enable multi-turn discussions and council
  discussion_max_turns: 6          # Max turns per bilateral discussion
  discussion_max_cost: 1.00        # USD cost cap per bilateral discussion
  council_max_cost: 2.00           # USD cost cap per council session
  council_max_rounds: 2            # Rounds per council (each agent speaks once per round)

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

# Managed Agents API (optional — enables long-running task routing)
ANTHROPIC_API_KEY=sk-ant-...

# Webhook signature secrets (set to enforce verification in production)
GITHUB_WEBHOOK_SECRET=whsec_...
STRIPE_WEBHOOK_SECRET=whsec_...
```

## Data Directory

```
~/.config/claude-daemon/
├── config.yaml
├── claude_daemon.db              # SQLite (conversations, FTS5, agent_metrics, task_queue, failure_analyses, evolution_log, discussions, memory_vec)
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
│   ├── DIRECTIVE.md              # Team operating directive (Tier 1, all agents)
│   ├── USER.md                   # Your context (all agents read)
│   ├── learnings.md              # Cross-agent insights (auto-generated)
│   ├── events.md                 # Agent activity log (auto-maintained)
│   ├── failure-lessons.md        # Auto-extracted from failed tasks
│   ├── evolution-log.md          # Record of self-applied prompt mutations
│   ├── evolution-archive/        # Timestamped backups before SOUL/AGENTS edits
│   ├── playbooks/                # Compounding lessons
│   │   ├── improvement-plan.md   # Weekly improvement proposals
│   │   ├── tech-debt.md          # Albert's findings
│   │   ├── quality-retro.md      # Max's findings
│   │   └── ...
│   ├── steer/                    # Mid-task redirection
│   ├── checklists/               # QA templates
│   └── template-archive/         # Backups before template merges
├── memory/                       # Global daemon memory
└── logs/
```
