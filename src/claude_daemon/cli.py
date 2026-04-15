"""CLI entry point for claude-daemon management commands."""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys
from pathlib import Path


def _cmd_start(args: argparse.Namespace) -> None:
    """Start the daemon (foreground or background)."""
    from claude_daemon.core.config import DaemonConfig
    from claude_daemon.core.daemon import ClaudeDaemon
    from claude_daemon.utils.paths import ensure_dirs

    config_path = Path(args.config) if args.config else None
    config = DaemonConfig.load(config_path)
    ensure_dirs()

    # Check if already running
    if config.pid_path.exists():
        try:
            pid = int(config.pid_path.read_text().strip())
            os.kill(pid, 0)  # Check if process exists
            print(f"Claude Daemon is already running (PID {pid})")
            sys.exit(1)
        except (ProcessLookupError, ValueError):
            config.pid_path.unlink(missing_ok=True)

    if args.foreground:
        # Run in foreground (for systemd/launchd/development)
        daemon = ClaudeDaemon(config)
        asyncio.run(daemon.start())
    else:
        # Fork to background
        _daemonize(config)


def _daemonize(config) -> None:
    """Fork to background using double-fork pattern."""
    from claude_daemon.core.daemon import ClaudeDaemon

    # First fork
    pid = os.fork()
    if pid > 0:
        print(f"Claude Daemon started in background (PID {pid})")
        sys.exit(0)

    os.setsid()

    # Second fork
    pid = os.fork()
    if pid > 0:
        sys.exit(0)

    # Redirect stdio
    sys.stdin.close()
    log_path = config.log_dir / "daemon.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    sys.stdout = open(log_path, "a")
    sys.stderr = sys.stdout

    daemon = ClaudeDaemon(config)
    asyncio.run(daemon.start())


def _cmd_stop(args: argparse.Namespace) -> None:
    """Stop a running daemon."""
    from claude_daemon.utils.paths import pid_path

    pf = pid_path()
    if not pf.exists():
        print("Claude Daemon is not running (no PID file)")
        sys.exit(1)

    try:
        pid = int(pf.read_text().strip())
        os.kill(pid, signal.SIGTERM)
        print(f"Sent SIGTERM to Claude Daemon (PID {pid})")
    except ProcessLookupError:
        print("Claude Daemon is not running (stale PID file)")
        pf.unlink(missing_ok=True)
    except ValueError:
        print("Invalid PID file")
        pf.unlink(missing_ok=True)


def _cmd_restart(args: argparse.Namespace) -> None:
    """Restart the daemon."""
    _cmd_stop(args)
    import time
    time.sleep(1)
    _cmd_start(args)


def _cmd_status(args: argparse.Namespace) -> None:
    """Show daemon status."""
    from claude_daemon.utils.paths import pid_path, config_dir

    pf = pid_path()
    if not pf.exists():
        print("Claude Daemon: not running")
        return

    try:
        pid = int(pf.read_text().strip())
        os.kill(pid, 0)
        print(f"Claude Daemon: running (PID {pid})")
        print(f"Config dir: {config_dir()}")
    except ProcessLookupError:
        print("Claude Daemon: not running (stale PID file)")


def _cmd_logs(args: argparse.Namespace) -> None:
    """View daemon logs."""
    from claude_daemon.utils.paths import log_dir

    log_file = log_dir() / "daemon.log"
    if not log_file.exists():
        print("No log file found")
        return

    lines = args.lines or 50
    if args.follow:
        os.execlp("tail", "tail", "-f", "-n", str(lines), str(log_file))
    else:
        with open(log_file) as f:
            all_lines = f.readlines()
            for line in all_lines[-lines:]:
                print(line, end="")


def _cmd_config(args: argparse.Namespace) -> None:
    """Show or edit configuration."""
    from claude_daemon.utils.paths import config_dir

    cfg_path = config_dir() / "config.yaml"
    if args.edit:
        editor = os.environ.get("EDITOR", "vi")
        if not cfg_path.exists():
            cfg_path.parent.mkdir(parents=True, exist_ok=True)
            cfg_path.write_text("# Claude Daemon configuration\n# See config.example.yaml\n")
        os.execlp(editor, editor, str(cfg_path))
    else:
        if cfg_path.exists():
            print(cfg_path.read_text())
        else:
            print(f"No config file at {cfg_path}")
            print("Run 'claude-daemon config --edit' to create one")


def _cmd_memory(args: argparse.Namespace) -> None:
    """Memory management commands."""
    from claude_daemon.utils.paths import memory_dir

    if args.action == "show":
        mem_file = memory_dir() / "MEMORY.md"
        if mem_file.exists():
            print(mem_file.read_text())
        else:
            print("No persistent memory yet.")
    elif args.action == "compact":
        print("Running memory compaction...")
        from claude_daemon.core.config import DaemonConfig
        from claude_daemon.core.daemon import ClaudeDaemon
        config = DaemonConfig.load()
        daemon = ClaudeDaemon(config)
        asyncio.run(_run_compaction(daemon))
    elif args.action == "dream":
        print("Running auto-dream memory consolidation...")
        from claude_daemon.core.config import DaemonConfig
        from claude_daemon.core.daemon import ClaudeDaemon
        config = DaemonConfig.load()
        daemon = ClaudeDaemon(config)
        asyncio.run(_run_dream(daemon))


async def _run_compaction(daemon) -> None:
    from claude_daemon.memory.compactor import ContextCompactor
    from claude_daemon.memory.durable import DurableMemory
    from claude_daemon.memory.store import ConversationStore
    from claude_daemon.core.process import ProcessManager

    store = ConversationStore(daemon.config.db_path)
    durable = DurableMemory(daemon.config.memory_dir)
    pm = ProcessManager(daemon.config)
    compactor = ContextCompactor(store, durable, pm)
    await compactor.daily_compaction()
    store.close()
    print("Compaction complete.")


async def _run_dream(daemon) -> None:
    from claude_daemon.memory.compactor import ContextCompactor
    from claude_daemon.memory.durable import DurableMemory
    from claude_daemon.memory.store import ConversationStore
    from claude_daemon.core.process import ProcessManager

    store = ConversationStore(daemon.config.db_path)
    durable = DurableMemory(daemon.config.memory_dir)
    pm = ProcessManager(daemon.config)
    compactor = ContextCompactor(store, durable, pm)
    await compactor.auto_dream()
    store.close()
    print("Auto-dream complete.")


def _cmd_update(args: argparse.Namespace) -> None:
    """Check for and apply updates."""
    from claude_daemon.core.config import DaemonConfig
    from claude_daemon.core.process import ProcessManager
    from claude_daemon.updater.updater import Updater

    config = DaemonConfig.load()
    pm = ProcessManager(config)
    updater = Updater(config, pm)
    result = asyncio.run(updater.check_and_update(check_only=args.check_only))
    print(result)


def _cmd_install_service(args: argparse.Namespace) -> None:
    """Install OS service files."""
    from claude_daemon.utils.paths import log_dir
    from claude_daemon.utils.platform import install_systemd_service, install_launchd_service

    ld = log_dir()
    ld.mkdir(parents=True, exist_ok=True)

    if args.launchd:
        path = install_launchd_service(ld)
        print(f"Installed launchd plist: {path}")
        print("Load with: launchctl load " + str(path))
    else:
        path = install_systemd_service(ld)
        print(f"Installed systemd unit: {path}")
        print("Enable with: systemctl --user enable --now claude-daemon")


def _cmd_env(args: argparse.Namespace) -> None:
    """Manage environment variables."""
    from claude_daemon.core.config import DaemonConfig
    from claude_daemon.core.env_manager import list_env_vars, set_env_var, reload_env

    # Ensure .env is loaded
    DaemonConfig.load()

    action = getattr(args, "env_action", None) or "list"

    if action == "list":
        env_vars = list_env_vars()
        print("Environment variables:\n")
        for var in env_vars:
            status = var["status"]
            if status == "set":
                print(f"  {var['key']:30s} set  ({var['masked']})")
            else:
                print(f"  {var['key']:30s} unset")
        print(f"\nFile: {env_vars and 'see' or ''} ~/.config/claude-daemon/.env")
        print("Set with: claude-daemon env set KEY=VALUE")

    elif action == "set":
        pair = args.pair
        if "=" not in pair:
            print("Usage: claude-daemon env set KEY=VALUE")
            sys.exit(1)
        key, value = pair.split("=", 1)
        key = key.strip()
        value = value.strip()
        try:
            set_env_var(key, value)
            masked = "****" + value[-4:] if len(value) >= 4 else "****"
            print(f"Set {key} = {masked}")
            print("Restart the daemon for integration tokens to take effect.")
        except ValueError as e:
            print(f"Error: {e}")
            sys.exit(1)


def _cmd_mcp(args: argparse.Namespace) -> None:
    """Manage MCP server pool."""
    from claude_daemon.agents.bootstrap import (
        get_mcp_catalog_status, refresh_agent_tools_json,
    )
    from claude_daemon.core.config import DaemonConfig

    config = DaemonConfig.load()
    action = getattr(args, "mcp_action", None) or "list"

    if action == "list":
        statuses = get_mcp_catalog_status(config.disabled_mcp_servers)
        by_cat: dict[str, list] = {}
        for s in statuses:
            by_cat.setdefault(s["category"], []).append(s)

        print(f"MCP Server Pool ({len(statuses)} servers):\n")
        for cat, servers in sorted(by_cat.items()):
            print(f"  [{cat}]")
            for s in servers:
                icon = {"active": "+", "inactive": "-", "disabled": "x"}
                mark = icon.get(s["status"], "?")
                tier_label = {"zero-config": "T1", "configured": "T2",
                              "needs-token": "T2", "disabled": "T3"}
                tier = tier_label.get(s["tier"], "?")
                extra = ""
                if s["status"] == "inactive":
                    missing = [k for k, v in s["env_status"].items() if v == "unset"]
                    extra = f" (needs: {', '.join(missing)})"
                print(f"    {mark} {s['name']:20s} [{tier}]  {s['description']}{extra}")
        print()
        print("+ active  - needs token  x disabled")
        print("T1=zero-config  T2=token-required  T3=disabled")
        print()
        print("claude-daemon mcp enable <name>  — enable a disabled server")
        print("claude-daemon mcp disable <name> — disable a server")
        print("claude-daemon mcp refresh        — regenerate tools.json from env")

    elif action == "enable":
        name = args.server
        import yaml
        cfg_path = config.data_dir / "config.yaml"
        for p in [cfg_path, config.data_dir.parent / "config.yaml"]:
            if p.exists():
                cfg_path = p
                break
        data: dict = {}
        if cfg_path.exists():
            with open(cfg_path) as f:
                data = yaml.safe_load(f) or {}
        claude_sec = data.setdefault("claude", {})
        disabled = claude_sec.get("disabled_mcp_servers", [])
        if name in disabled:
            disabled.remove(name)
            claude_sec["disabled_mcp_servers"] = disabled
            with open(cfg_path, "w") as f:
                yaml.safe_dump(data, f, default_flow_style=False, sort_keys=False)
            print(f"Enabled '{name}'. Refreshing configs...")
        else:
            print(f"'{name}' is not disabled.")
        agents_dir = config.data_dir / "agents"
        counts = refresh_agent_tools_json(agents_dir, disabled_servers=disabled)
        sample = next(iter(counts.values()), 0) if counts else 0
        print(f"Done. {sample} servers active across {len(counts)} agents.")

    elif action == "disable":
        name = args.server
        import yaml
        cfg_path = config.data_dir / "config.yaml"
        for p in [cfg_path, config.data_dir.parent / "config.yaml"]:
            if p.exists():
                cfg_path = p
                break
        data: dict = {}
        if cfg_path.exists():
            with open(cfg_path) as f:
                data = yaml.safe_load(f) or {}
        claude_sec = data.setdefault("claude", {})
        disabled = claude_sec.get("disabled_mcp_servers", [])
        if name not in disabled:
            disabled.append(name)
            claude_sec["disabled_mcp_servers"] = disabled
            with open(cfg_path, "w") as f:
                yaml.safe_dump(data, f, default_flow_style=False, sort_keys=False)
            print(f"Disabled '{name}'. Refreshing configs...")
        else:
            print(f"'{name}' is already disabled.")
        agents_dir = config.data_dir / "agents"
        counts = refresh_agent_tools_json(agents_dir, disabled_servers=disabled)
        sample = next(iter(counts.values()), 0) if counts else 0
        print(f"Done. {sample} servers active across {len(counts)} agents.")

    elif action == "refresh":
        agents_dir = config.data_dir / "agents"
        counts = refresh_agent_tools_json(
            agents_dir, disabled_servers=config.disabled_mcp_servers,
        )
        sample = next(iter(counts.values()), 0) if counts else 0
        print(f"MCP configs refreshed: {sample} servers active across {len(counts)} agents.")


def _cmd_thinking(args: argparse.Namespace) -> None:
    """Toggle extended thinking for all agents."""
    from claude_daemon.core.config import DaemonConfig
    from claude_daemon.agents.bootstrap import refresh_agent_configs

    config = DaemonConfig.load()
    enabled = args.toggle == "on"
    config.thinking_enabled = enabled

    agents_dir = config.data_dir / "agents"
    counts = refresh_agent_configs(
        agents_dir,
        disabled_servers=config.disabled_mcp_servers,
        deny_rules=config.agent_deny_rules,
        thinking_enabled=enabled,
    )
    state = "on" if enabled else "off"
    print(f"Extended thinking: {state} (updated {len(counts)} agents)")


def _cmd_effort(args: argparse.Namespace) -> None:
    """Set reasoning effort level for all tasks."""
    from claude_daemon.core.config import DaemonConfig

    config = DaemonConfig.load()
    level = args.level
    config.default_effort = level
    print(f"Default effort set to: {level}")
    print("Note: this takes effect on next daemon restart or config reload.")


def _cmd_backend(args: argparse.Namespace) -> None:
    """Control Managed Agents backend."""
    import os
    from claude_daemon.core.config import DaemonConfig

    config = DaemonConfig.load()
    action = getattr(args, "action", "status")

    if action == "on":
        if not os.environ.get("ANTHROPIC_API_KEY"):
            print("Error: ANTHROPIC_API_KEY env var not set.")
            print("Set it with: claude-daemon env set ANTHROPIC_API_KEY=sk-ant-...")
            return
        config.managed_agents_enabled = True
        print("Managed Agents enabled.")
        print(f"Task types routed to API: {', '.join(config.managed_agents_task_types)}")
        print("Note: takes effect on next daemon restart or config reload.")

    elif action == "off":
        config.managed_agents_enabled = False
        print("Managed Agents disabled. All tasks route to CLI.")
        print("Note: takes effect on next daemon restart or config reload.")

    else:  # status
        api_key = bool(os.environ.get("ANTHROPIC_API_KEY"))
        print("Managed Agents Backend")
        print(f"  Enabled:    {config.managed_agents_enabled}")
        print(f"  API key:    {'set' if api_key else 'not set'}")
        print(f"  Task types: {', '.join(config.managed_agents_task_types)}")
        if not api_key:
            print("\n  To enable: set ANTHROPIC_API_KEY then run 'claude-daemon backend on'")


def _cmd_agents(args: argparse.Namespace) -> None:
    """Manage agents."""
    from claude_daemon.agents.bootstrap import create_csuite_workspaces, create_shared_workspace
    from claude_daemon.agents.template_merge import merge_agent_templates
    from claude_daemon.agents.registry import AgentRegistry
    from claude_daemon.core.config import DaemonConfig

    config = DaemonConfig.load()
    agents_dir = config.data_dir / "agents"
    shared_dir = config.data_dir / "shared"
    create_shared_workspace(config.data_dir)
    create_csuite_workspaces(agents_dir)
    merge_agent_templates(agents_dir)
    registry = AgentRegistry(agents_dir, shared_dir=shared_dir)
    registry.load_all()

    action = getattr(args, "agents_action", None) or "list"

    if action == "list":
        if not len(registry):
            print("No agents configured.")
            return
        print(f"Agents ({len(registry)}):\n")
        for agent in registry:
            orch = " [orchestrator]" if agent.is_orchestrator else ""
            role = f" ({agent.identity.role})" if agent.identity.role else ""
            emoji = f"{agent.identity.emoji} " if agent.identity.emoji else ""
            print(f"  {emoji}{agent.name}{role}{orch}")
            print(f"    workspace: {agent.workspace}")
        print(f"\nAgent workspaces: {agents_dir}")

    elif action == "create":
        name = args.name.lower().replace(" ", "-")
        agent = registry.create_agent(
            name=name,
            role=args.role,
            emoji=args.emoji,
            is_orchestrator=args.orchestrator,
        )
        print(f"Created agent: {name}")
        print(f"  Workspace: {agent.workspace}")
        print(f"  Files: SOUL.md, IDENTITY.md, MEMORY.md")
        print(f"\nEdit the .md files in the workspace to customize this agent.")


def _cmd_jobs(args: argparse.Namespace) -> None:
    """List scheduled jobs."""
    from claude_daemon.core.config import DaemonConfig
    config = DaemonConfig.load()

    print("Built-in jobs:")
    print(f"  auto_update:       {config.update_cron}")
    print(f"  memory_compaction: {config.compaction_cron}")
    print(f"  auto_dream:        {config.dream_cron}")
    print(f"  heartbeat:         every {config.heartbeat_interval}s")
    print(f"  session_cleanup:   every 6h")

    if config.custom_jobs:
        print("\nCustom jobs:")
        for job in config.custom_jobs:
            print(f"  {job.get('id', '?'):20s} {job.get('cron', '?')}")
    else:
        print("\nNo custom jobs configured.")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="claude-daemon",
        description="Persistent daemon wrapper for Claude Code",
    )
    sub = parser.add_subparsers(dest="command")

    # start
    p_start = sub.add_parser("start", help="Start the daemon")
    p_start.add_argument("--config", "-c", help="Path to config YAML")
    p_start.add_argument("--foreground", "-f", action="store_true",
                         help="Run in foreground (for systemd/development)")

    # stop
    sub.add_parser("stop", help="Stop the daemon")

    # restart
    p_restart = sub.add_parser("restart", help="Restart the daemon")
    p_restart.add_argument("--config", "-c", help="Path to config YAML")
    p_restart.add_argument("--foreground", "-f", action="store_true")

    # status
    sub.add_parser("status", help="Show daemon status")

    # logs
    p_logs = sub.add_parser("logs", help="View daemon logs")
    p_logs.add_argument("--follow", "-f", action="store_true")
    p_logs.add_argument("--lines", "-n", type=int, default=50)

    # config
    p_config = sub.add_parser("config", help="Show or edit configuration")
    p_config.add_argument("--edit", "-e", action="store_true")

    # memory
    p_mem = sub.add_parser("memory", help="Memory management")
    p_mem.add_argument("action", choices=["show", "compact", "dream"], default="show", nargs="?")

    # update
    p_update = sub.add_parser("update", help="Check for updates")
    p_update.add_argument("--check-only", action="store_true")

    # install-service
    p_svc = sub.add_parser("install-service", help="Install OS service files")
    p_svc.add_argument("--systemd", action="store_true", default=True)
    p_svc.add_argument("--launchd", action="store_true")

    # jobs
    sub.add_parser("jobs", help="List scheduled jobs")

    # env
    p_env = sub.add_parser("env", help="Manage environment variables")
    p_env_sub = p_env.add_subparsers(dest="env_action")
    p_env_sub.add_parser("list", help="List all env vars with set/unset status")
    p_env_set = p_env_sub.add_parser("set", help="Set an env var (KEY=VALUE)")
    p_env_set.add_argument("pair", help="KEY=VALUE")

    # mcp
    p_mcp = sub.add_parser("mcp", help="Manage MCP server pool")
    p_mcp_sub = p_mcp.add_subparsers(dest="mcp_action")
    p_mcp_sub.add_parser("list", help="List all MCP servers with tier and status")
    p_mcp_en = p_mcp_sub.add_parser("enable", help="Enable a disabled server")
    p_mcp_en.add_argument("server", help="Server name")
    p_mcp_dis = p_mcp_sub.add_parser("disable", help="Disable a server")
    p_mcp_dis.add_argument("server", help="Server name")
    p_mcp_sub.add_parser("refresh", help="Regenerate tools.json from current env")

    # thinking
    p_thinking = sub.add_parser("thinking", help="Toggle extended thinking for all agents")
    p_thinking.add_argument("toggle", choices=["on", "off"], help="on or off")

    # effort
    p_effort = sub.add_parser("effort", help="Set reasoning effort level for all tasks")
    p_effort.add_argument("level", choices=["low", "medium", "high", "max"], help="Effort level")

    # backend
    p_backend = sub.add_parser("backend", help="Control Managed Agents backend")
    p_backend.add_argument("action", nargs="?", default="status",
                           choices=["status", "on", "off"], help="Action")

    # agents
    p_agents = sub.add_parser("agents", help="Manage agents")
    p_agents_sub = p_agents.add_subparsers(dest="agents_action")
    p_agents_sub.add_parser("list", help="List all agents")
    p_ag_create = p_agents_sub.add_parser("create", help="Create a new agent")
    p_ag_create.add_argument("name", help="Agent name")
    p_ag_create.add_argument("--role", default="", help="Agent role")
    p_ag_create.add_argument("--emoji", default="", help="Agent emoji")
    p_ag_create.add_argument("--orchestrator", action="store_true")

    args = parser.parse_args()

    commands = {
        "start": _cmd_start,
        "stop": _cmd_stop,
        "restart": _cmd_restart,
        "status": _cmd_status,
        "logs": _cmd_logs,
        "config": _cmd_config,
        "memory": _cmd_memory,
        "update": _cmd_update,
        "install-service": _cmd_install_service,
        "jobs": _cmd_jobs,
        "env": _cmd_env,
        "mcp": _cmd_mcp,
        "thinking": _cmd_thinking,
        "effort": _cmd_effort,
        "backend": _cmd_backend,
        "agents": _cmd_agents,
    }

    handler = commands.get(args.command)
    if handler:
        handler(args)
    else:
        parser.print_help()
