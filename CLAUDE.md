# CLAUDE.md — claude-daemon project configuration

## Overview

This is a multi-agent daemon system. Agent identities, routing rules, and
workflows are NOT defined here — they live in per-agent workspace files at
`~/.config/claude-daemon/agents/{name}/`. Do not hardcode agent names, roles,
or routing in this file.

## Architecture

- Agents are bootstrapped dynamically from `src/claude_daemon/agents/bootstrap.py`
- Each agent has: SOUL.md, IDENTITY.md, AGENTS.md, MEMORY.md, HEARTBEAT.md, tools.json
- The orchestrator routes messages to agents based on @name addressing or auto-routing
- Workflows execute multi-step pipelines via WorkflowEngine
- The ImprovementPlanner runs weekly self-assessment and improvement cycles

## Development

```bash
pip install -e ".[dev]"
pytest tests/
```

## Testing

All code changes must pass `pytest tests/` before commit. Tests cover:
- Agent identity, model routing, heartbeat parsing, MCP config
- Workflow engine (pipeline, parallel, review loop)
- Memory validation, FTS5 search, agent metrics
- Conversation store, config loading, message routing

## Code Style

- Python 3.10+, type hints everywhere
- `ruff` for formatting (line-length 100)
- Async/await for all I/O operations
- Dataclasses over dicts for structured data
