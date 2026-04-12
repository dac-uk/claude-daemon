"""Configuration loading for claude-daemon.

Precedence (lowest to highest): defaults < YAML config < .env file < env vars.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

from claude_daemon.utils import paths


def _env(key: str, default: Any = None, cast: type = str) -> Any:
    """Read a CLAUDE_DAEMON_ prefixed env var with optional type cast."""
    val = os.environ.get(f"CLAUDE_DAEMON_{key}", os.environ.get(key))
    if val is None:
        return default
    if cast is bool:
        return val.lower() in ("1", "true", "yes")
    return cast(val)


@dataclass
class DaemonConfig:
    """All daemon configuration in one place."""

    # Core
    data_dir: Path = field(default_factory=paths.config_dir)
    log_level: str = "INFO"

    # Claude Code
    claude_binary: str = "claude"
    max_concurrent_sessions: int = 5
    max_budget_per_message: float = 0.50
    default_model: str | None = None
    permission_mode: str = "auto"
    process_timeout: int = 300
    mcp_config: str | None = None  # Path to MCP server config JSON
    streaming_enabled: bool = True  # Use stream-json for interactive responses
    per_agent_daily_budget: float = 0.0  # USD per agent per day (0 = unlimited)
    model_fallback_chain: list[str] = field(default_factory=lambda: ["sonnet", "haiku"])
    model_retry_delay: float = 2.0  # Seconds between fallback retries
    model_max_retries: int = 2  # 0 disables model fallback
    stream_idle_timeout_ms: int = 600_000  # ms before idle stream is killed (default 90s is too short for Opus thinking)
    auto_compact_pct: int = 50  # Auto-compact at this % of context window (CLI default waits much longer)
    thinking_enabled: bool = True  # alwaysThinkingEnabled in per-agent settings.json
    default_effort: str = ""  # Override effort level for all tasks (empty = use per-task-type mapping)
    agent_deny_rules: list[str] = field(default_factory=list)  # Extra deny rules appended to defaults

    # Memory
    daily_log_enabled: bool = True
    compaction_threshold: int = 50_000
    max_session_age_hours: int = 72
    dream_enabled: bool = True
    max_context_chars: int = 5000
    max_memory_chars: int = 3000
    log_retention_days: int = 30  # Garbage collect daily logs older than this
    self_improve: bool = True  # Enable reflexion/self-improvement feedback loop

    # Scheduler
    update_cron: str = "0 3 * * *"
    compaction_cron: str = "0 4 * * *"
    dream_cron: str = "0 5 * * 0"
    heartbeat_interval: int = 1800
    custom_jobs: list[dict] = field(default_factory=list)

    # MCP server pool
    disabled_mcp_servers: list[str] = field(default_factory=list)  # Explicitly disabled servers

    # Agent hot-reload
    agent_hot_reload: bool = True  # Auto-detect config file changes
    agent_reload_interval: int = 10  # Seconds between file change polls

    # HTTP API
    api_enabled: bool = False
    api_port: int = 8080
    api_bind: str = "0.0.0.0"  # Bind address (0.0.0.0 = all interfaces inc. Tailscale/ZeroTier)
    api_key: str = ""  # Bearer token for API auth (empty = no auth)
    dashboard_enabled: bool = False  # Serve live agent dashboard at /
    github_webhook_secret: str = ""  # GitHub webhook signing secret
    stripe_webhook_secret: str = ""  # Stripe webhook signing secret

    # Alert webhooks
    alert_webhook_urls: list[str] = field(default_factory=list)  # URLs to POST alerts to
    alert_webhook_timeout: int = 10  # HTTP timeout for webhook calls

    # Rate limiting
    rate_limit_per_user: int = 20  # Messages per minute per user
    rate_limit_window: int = 60  # Window in seconds

    # Integrations (tokens from env, not YAML)
    telegram_token: str | None = None
    telegram_allowed_users: list[int] = field(default_factory=list)
    telegram_polling: bool = True
    telegram_agent_channels: dict[str, str] = field(default_factory=dict)  # chat_id -> agent_name
    discord_token: str | None = None
    discord_allowed_guilds: list[int] = field(default_factory=list)
    discord_agent_channels: dict[str, str] = field(default_factory=dict)  # channel_id -> agent_name
    discord_alert_channel_ids: list[str] = field(default_factory=list)  # channel_ids for heartbeat/alert delivery
    paperclip_url: str | None = None
    paperclip_api_key: str | None = None
    paperclip_poll_interval: int = 5

    # Derived paths
    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def memory_dir(self) -> Path:
        return self.data_dir / "memory"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "daemon.db"

    @property
    def pid_path(self) -> Path:
        return paths.pid_path()

    @property
    def soul_path(self) -> Path:
        return self.data_dir / "SOUL.md"

    @property
    def reflections_path(self) -> Path:
        return self.data_dir / "REFLECTIONS.md"

    @classmethod
    def load(cls, config_path: Path | None = None) -> DaemonConfig:
        """Load config from YAML, .env, and environment variables."""
        # Load .env first so env vars are available
        for dotenv in [Path(".env"), paths.config_dir() / ".env"]:
            if dotenv.exists():
                load_dotenv(dotenv)

        # Load YAML config
        yaml_data: dict[str, Any] = {}
        candidates = [
            config_path,
            Path("config.yaml"),
            Path("config.yml"),
            paths.config_dir() / "config.yaml",
            paths.config_dir() / "config.yml",
        ]
        for candidate in candidates:
            if candidate and candidate.exists():
                with open(candidate) as f:
                    yaml_data = yaml.safe_load(f) or {}
                break

        daemon_cfg = yaml_data.get("daemon", {})
        claude_cfg = yaml_data.get("claude", {})
        memory_cfg = yaml_data.get("memory", {})
        sched_cfg = yaml_data.get("scheduler", {})
        integ_cfg = yaml_data.get("integrations", {})
        tg_cfg = integ_cfg.get("telegram", {})
        dc_cfg = integ_cfg.get("discord", {})
        pc_cfg = integ_cfg.get("paperclip", {})

        data_dir_str = _env("DATA_DIR") or daemon_cfg.get("data_dir")
        data_dir = Path(os.path.expanduser(data_dir_str)) if data_dir_str else paths.config_dir()

        return cls(
            data_dir=data_dir,
            log_level=_env("LOG_LEVEL") or daemon_cfg.get("log_level", "INFO"),
            claude_binary=claude_cfg.get("binary", "claude"),
            max_concurrent_sessions=int(claude_cfg.get("max_concurrent", 3)),
            max_budget_per_message=float(claude_cfg.get("max_budget_per_message", 0.50)),
            default_model=claude_cfg.get("model"),
            permission_mode=claude_cfg.get("permission_mode", "auto"),
            process_timeout=int(claude_cfg.get("process_timeout", 300)),
            mcp_config=claude_cfg.get("mcp_config"),
            streaming_enabled=claude_cfg.get("streaming", True),
            per_agent_daily_budget=float(claude_cfg.get("per_agent_daily_budget", 0.0)),
            model_fallback_chain=claude_cfg.get("model_fallback_chain", ["sonnet", "haiku"]),
            model_retry_delay=float(claude_cfg.get("model_retry_delay", 2.0)),
            model_max_retries=int(claude_cfg.get("model_max_retries", 2)),
            stream_idle_timeout_ms=int(claude_cfg.get("stream_idle_timeout_ms", 600_000)),
            auto_compact_pct=int(claude_cfg.get("auto_compact_pct", 50)),
            thinking_enabled=claude_cfg.get("thinking_enabled", True),
            default_effort=claude_cfg.get("default_effort", ""),
            agent_deny_rules=claude_cfg.get("agent_deny_rules", []),
            disabled_mcp_servers=claude_cfg.get("disabled_mcp_servers", []),
            agent_hot_reload=daemon_cfg.get("agent_hot_reload", True),
            agent_reload_interval=int(daemon_cfg.get("agent_reload_interval", 10)),
            daily_log_enabled=memory_cfg.get("daily_log", True),
            compaction_threshold=int(memory_cfg.get("compaction_threshold", 50_000)),
            max_session_age_hours=int(memory_cfg.get("max_session_age_hours", 72)),
            dream_enabled=memory_cfg.get("dream_enabled", True),
            max_context_chars=int(memory_cfg.get("max_context_chars", 5000)),
            max_memory_chars=int(memory_cfg.get("max_memory_chars", 3000)),
            log_retention_days=int(memory_cfg.get("log_retention_days", 30)),
            self_improve=memory_cfg.get("self_improve", True),
            update_cron=sched_cfg.get("update_cron", "0 3 * * *"),
            compaction_cron=sched_cfg.get("compaction_cron", "0 4 * * *"),
            dream_cron=sched_cfg.get("dream_cron", "0 5 * * 0"),
            heartbeat_interval=int(sched_cfg.get("heartbeat_interval", 1800)),
            custom_jobs=sched_cfg.get("custom_jobs", []),
            api_enabled=daemon_cfg.get("api_enabled", False),
            api_port=int(daemon_cfg.get("api_port", 8080)),
            api_bind=daemon_cfg.get("api_bind", "0.0.0.0"),
            api_key=os.environ.get("CLAUDE_DAEMON_API_KEY") or daemon_cfg.get("api_key", ""),
            dashboard_enabled=daemon_cfg.get("dashboard_enabled", False),
            github_webhook_secret=(
                os.environ.get("GITHUB_WEBHOOK_SECRET")
                or daemon_cfg.get("github_webhook_secret", "")
            ),
            stripe_webhook_secret=(
                os.environ.get("STRIPE_WEBHOOK_SECRET")
                or daemon_cfg.get("stripe_webhook_secret", "")
            ),
            alert_webhook_urls=daemon_cfg.get("alert_webhook_urls", []),
            alert_webhook_timeout=int(daemon_cfg.get("alert_webhook_timeout", 10)),
            rate_limit_per_user=int(daemon_cfg.get("rate_limit_per_user", 20)),
            rate_limit_window=int(daemon_cfg.get("rate_limit_window", 60)),
            telegram_token=os.environ.get("TELEGRAM_BOT_TOKEN") or tg_cfg.get("token"),
            telegram_allowed_users=tg_cfg.get("allowed_user_ids", []),
            telegram_polling=tg_cfg.get("polling", True),
            telegram_agent_channels={
                str(k): v for k, v in tg_cfg.get("agent_channels", {}).items()
            },
            discord_token=os.environ.get("DISCORD_BOT_TOKEN") or dc_cfg.get("token"),
            discord_allowed_guilds=dc_cfg.get("allowed_guild_ids", []),
            discord_agent_channels={
                str(k): v for k, v in dc_cfg.get("agent_channels", {}).items()
            },
            discord_alert_channel_ids=[
                str(cid) for cid in dc_cfg.get("alert_channel_ids", [])
            ],
            paperclip_url=os.environ.get("PAPERCLIP_URL") or pc_cfg.get("url"),
            paperclip_api_key=os.environ.get("PAPERCLIP_API_KEY") or pc_cfg.get("api_key"),
            paperclip_poll_interval=int(pc_cfg.get("poll_interval", 5)),
        )
