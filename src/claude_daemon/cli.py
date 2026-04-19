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
    """Stop a running daemon gracefully.

    Uses the OS service manager (launchd/systemd) so the daemon is not
    automatically respawned. Falls back to SIGTERM if no service manager.
    """
    import platform
    import subprocess

    system = platform.system()

    # macOS — launchctl unload (prevents KeepAlive respawn)
    if system == "Darwin":
        plist = Path.home() / "Library" / "LaunchAgents" / "com.claude-daemon.plist"
        if plist.exists():
            result = subprocess.run(
                ["launchctl", "unload", str(plist)],
                capture_output=True, text=True,
            )
            if result.returncode == 0:
                print("Claude Daemon stopped (launchd service unloaded)")
                print(f"  To start again: launchctl load {plist}")
                return
            else:
                print(f"launchctl unload failed: {result.stderr.strip()}")
                print("Falling back to SIGTERM...")

    # Linux — systemctl --user stop (prevents Restart= respawn)
    elif system == "Linux":
        result = subprocess.run(
            ["systemctl", "--user", "stop", "claude-daemon"],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            print("Claude Daemon stopped (systemd service stopped)")
            print("  To start again: systemctl --user start claude-daemon")
            return
        # systemctl may not be available — fall through to SIGTERM

    # Fallback — SIGTERM via PID file (for --foreground or non-service setups)
    from claude_daemon.utils.paths import pid_path

    pf = pid_path()
    if not pf.exists():
        print("Claude Daemon is not running (no PID file, no active service)")
        sys.exit(1)

    try:
        pid = int(pf.read_text().strip())
        os.kill(pid, signal.SIGTERM)
        print(f"Sent SIGTERM to Claude Daemon (PID {pid})")
        print("  Note: if managed by launchd/systemd, it may respawn.")
        print("  Use 'launchctl unload' or 'systemctl --user stop' instead.")
    except ProcessLookupError:
        print("Claude Daemon is not running (stale PID file)")
        pf.unlink(missing_ok=True)
    except ValueError:
        print("Invalid PID file")
        pf.unlink(missing_ok=True)


def _cmd_restart(args: argparse.Namespace) -> None:
    """Restart the daemon via the OS service manager."""
    import platform
    import subprocess

    system = platform.system()

    if system == "Darwin":
        plist = Path.home() / "Library" / "LaunchAgents" / "com.claude-daemon.plist"
        if plist.exists():
            subprocess.run(["launchctl", "unload", str(plist)], capture_output=True)
            result = subprocess.run(
                ["launchctl", "load", str(plist)],
                capture_output=True, text=True,
            )
            if result.returncode == 0:
                print("Claude Daemon restarted (launchd)")
                return
            else:
                print(f"launchctl load failed: {result.stderr.strip()}")

    elif system == "Linux":
        result = subprocess.run(
            ["systemctl", "--user", "restart", "claude-daemon"],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            print("Claude Daemon restarted (systemd)")
            return
        print(f"systemctl restart failed: {result.stderr.strip()}")

    # Fallback
    _cmd_stop(args)
    import time
    time.sleep(1)
    _cmd_start(args)


def _cmd_status(args: argparse.Namespace) -> None:
    """Show daemon status and active warnings."""
    from claude_daemon.utils.paths import pid_path, config_dir

    pf = pid_path()
    if not pf.exists():
        print("Claude Daemon: not running")
    else:
        try:
            pid = int(pf.read_text().strip())
            os.kill(pid, 0)
            print(f"Claude Daemon: running (PID {pid})")
            print(f"Config dir: {config_dir()}")
        except ProcessLookupError:
            print("Claude Daemon: not running (stale PID file)")

    # Show active warnings (written by daemon on startup)
    warnings_path = config_dir() / "shared" / "WARNINGS.md"
    if warnings_path.exists():
        content = warnings_path.read_text().strip()
        if content:
            print()
            print("\033[1;33m--- Active Warnings ---\033[0m")
            # Skip the markdown header, show the content
            for line in content.split("\n"):
                if line.startswith("# ") or line.startswith("These warnings"):
                    continue
                if line.strip():
                    print(f"  \033[33m{line}\033[0m")
            print("\033[33m  Fix the issues above, then restart to clear.\033[0m")
            print()


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


INSTALL_SCRIPT_URL = "https://raw.githubusercontent.com/dac-uk/claude-daemon/main/install.sh"


def _stop_daemon_for_update() -> None:
    """Gracefully stop a running daemon before an update.

    Best-effort: tries launchd (macOS), systemd (Linux), then SIGTERM via
    PID file. Waits briefly for the process to exit so files aren't held
    open during `pip install`. Never raises — the update proceeds either
    way, but with clear status messages.
    """
    import platform
    import subprocess as sp
    import time

    from claude_daemon.utils.paths import pid_path

    system = platform.system()
    stopped = False

    if system == "Darwin":
        plist = Path.home() / "Library" / "LaunchAgents" / "com.claude-daemon.plist"
        if plist.exists():
            listing = sp.run(
                ["launchctl", "list"], capture_output=True, text=True,
            )
            if listing.returncode == 0 and "com.claude-daemon" in listing.stdout:
                print("Stopping Claude Daemon (launchctl unload) ...")
                result = sp.run(
                    ["launchctl", "unload", str(plist)],
                    capture_output=True, text=True,
                )
                if result.returncode == 0:
                    print("  Unloaded.")
                    stopped = True
                else:
                    print(f"  launchctl unload failed: {result.stderr.strip()}")
    elif system == "Linux":
        check = sp.run(
            ["systemctl", "--user", "is-active", "claude-daemon"],
            capture_output=True, text=True,
        )
        if check.returncode == 0 and check.stdout.strip() == "active":
            print("Stopping Claude Daemon (systemctl --user stop) ...")
            result = sp.run(
                ["systemctl", "--user", "stop", "claude-daemon"],
                capture_output=True, text=True,
            )
            if result.returncode == 0:
                print("  Stopped.")
                stopped = True
            else:
                print(f"  systemctl stop failed: {result.stderr.strip()}")

    # Fallback: SIGTERM via PID file (covers --foreground or non-service setups)
    pf = pid_path()
    if pf.exists():
        try:
            pid = int(pf.read_text().strip())
            os.kill(pid, 0)  # probe
            if not stopped:
                print(f"Stopping Claude Daemon (SIGTERM to PID {pid}) ...")
                os.kill(pid, signal.SIGTERM)
            # Wait up to 10s for graceful exit
            for _ in range(20):
                try:
                    os.kill(pid, 0)
                    time.sleep(0.5)
                except ProcessLookupError:
                    print("  Daemon process exited.")
                    pf.unlink(missing_ok=True)
                    return
            print(f"  Daemon PID {pid} still running after 10s; continuing anyway.")
        except ProcessLookupError:
            pf.unlink(missing_ok=True)
        except (ValueError, PermissionError):
            pass


def _cmd_update(args: argparse.Namespace) -> None:
    """Shorthand for:

        curl -sSL https://raw.githubusercontent.com/dac-uk/claude-daemon/main/install.sh | bash -s -- --update

    Fetches the latest install.sh from GitHub and pipes it into bash with --update.
    The installer preserves .env, config.yaml, and agent memories.

    With --local, uses the install.sh already on disk (next to the package).
    With --check-only, reports whether the local checkout has upstream commits
    but does not apply anything.
    """
    import subprocess as sp
    from pathlib import Path

    package_dir = Path(__file__).resolve().parent.parent.parent
    local_script = package_dir / "install.sh"
    git_dir = package_dir / ".git"

    if args.check_only:
        if not git_dir.is_dir():
            print("Not a git checkout; cannot check for updates without fetching.")
            print(f"Run `claude-daemon update` to fetch the latest installer from {INSTALL_SCRIPT_URL}")
            return
        try:
            sp.run(["git", "-C", str(package_dir), "fetch", "--quiet"], check=True, timeout=30)
            local = sp.check_output(
                ["git", "-C", str(package_dir), "rev-parse", "HEAD"],
                text=True, timeout=10,
            ).strip()
            remote = sp.check_output(
                ["git", "-C", str(package_dir), "rev-parse", "@{u}"],
                text=True, timeout=10,
            ).strip()
            if local == remote:
                print("Up to date.")
            else:
                behind = sp.check_output(
                    ["git", "-C", str(package_dir), "rev-list", "--count", "HEAD..@{u}"],
                    text=True, timeout=10,
                ).strip()
                print(f"{behind} new commit(s) available. Run `claude-daemon update` to apply.")
        except (sp.CalledProcessError, sp.TimeoutExpired, FileNotFoundError) as e:
            print(f"Could not check upstream: {e}")
        return

    if args.local:
        if not local_script.exists():
            print(f"--local specified but install.sh not found at {local_script}.")
            sys.exit(1)
        _stop_daemon_for_update()
        print(f"\nRunning local {local_script} --update ...\n")
        try:
            sp.run(["bash", str(local_script), "--update"], check=True)
        except sp.CalledProcessError as e:
            print(f"\nUpdate failed with exit code {e.returncode}.")
            sys.exit(e.returncode)
        except FileNotFoundError:
            print("bash is not available on this system.")
            sys.exit(1)
        return

    # Default: curl latest install.sh and pipe to bash --update
    print(f"Fetching latest installer from {INSTALL_SCRIPT_URL} ...")
    print("(Preserves your .env, config.yaml, and agent memories.)\n")
    _stop_daemon_for_update()
    try:
        curl = sp.Popen(
            ["curl", "-fsSL", INSTALL_SCRIPT_URL],
            stdout=sp.PIPE,
        )
        bash = sp.Popen(
            ["bash", "-s", "--", "--update"],
            stdin=curl.stdout,
        )
        if curl.stdout is not None:
            curl.stdout.close()
        bash.wait()
        curl.wait()
        if curl.returncode != 0:
            print(f"\nFailed to download installer (curl exit {curl.returncode}).")
            if local_script.exists():
                print(f"Falling back to local {local_script} ...\n")
                sp.run(["bash", str(local_script), "--update"], check=True)
            else:
                sys.exit(curl.returncode)
        elif bash.returncode != 0:
            print(f"\nUpdate failed with exit code {bash.returncode}.")
            sys.exit(bash.returncode)
    except FileNotFoundError as e:
        missing = "curl" if "curl" in str(e) else "bash"
        print(f"{missing} is not available on this system; cannot run update.")
        sys.exit(1)


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
    from claude_daemon.core.env_manager import list_env_vars, set_env_var

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


def _cmd_shared_brain(args: argparse.Namespace) -> None:
    """Manage the shared brain digest file."""
    from claude_daemon.agents.registry import AgentRegistry
    from claude_daemon.agents.shared_brain import (
        DEFAULT_CLAUDE_MD,
        SharedBrainBuilder,
        brain_status,
        install_into_claude_md,
        uninstall_from_claude_md,
    )
    from claude_daemon.core.config import DaemonConfig

    config = DaemonConfig.load()
    action = getattr(args, "sb_action", None) or "status"

    registry = AgentRegistry(
        agents_dir=config.data_dir / "agents",
        shared_dir=config.data_dir / "shared",
    )
    registry.load_all()
    builder = SharedBrainBuilder(
        registry=registry,
        shared_dir=config.data_dir / "shared",
        output_path=config.shared_brain_path,
        max_chars=config.shared_brain_max_chars,
    )

    if action == "sync":
        try:
            path = builder.write()
        except PermissionError as e:
            print(f"Error: cannot write {config.shared_brain_path}: {e}")
            sys.exit(1)
        size = path.stat().st_size if path.exists() else 0
        print(f"Wrote {path} ({size} bytes, {len(registry)} agents)")

    elif action == "show":
        if not config.shared_brain_path.exists():
            print("Not yet generated. Run: claude-daemon shared-brain sync")
            sys.exit(1)
        print(config.shared_brain_path.read_text(encoding="utf-8"))

    elif action == "install":
        try:
            builder.write()
            changed = install_into_claude_md(config.shared_brain_path)
        except PermissionError as e:
            print(f"Error: cannot write ~/.claude/CLAUDE.md: {e}")
            sys.exit(1)
        if changed:
            print(f"Installed @-import into {DEFAULT_CLAUDE_MD}")
        else:
            print(f"Already installed in {DEFAULT_CLAUDE_MD}")
        print(
            "Restart Claude Code CLI and the macOS app to pick up the brain."
        )

    elif action == "uninstall":
        changed = uninstall_from_claude_md()
        if changed:
            print(f"Removed shared-brain block from {DEFAULT_CLAUDE_MD}")
        else:
            print(f"No shared-brain block found in {DEFAULT_CLAUDE_MD}")

    elif action == "status":
        info = brain_status(config.shared_brain_path)
        for key, val in info.items():
            print(f"{key}: {val}")


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


def _cmd_chat(args: argparse.Namespace) -> None:
    """Interactive CLI chat with the daemon's agents."""
    import http.client
    import json
    import threading
    import urllib.parse

    from claude_daemon.core.config import DaemonConfig

    config = DaemonConfig.load()

    if not config.api_enabled:
        # Auto-enable the API in config.yaml so chat works
        from claude_daemon.utils.paths import config_dir
        yaml_path = config_dir() / "config.yaml"
        if yaml_path.exists():
            content = yaml_path.read_text()
            # Enable api_enabled (uncomment or set)
            import re
            if "api_enabled:" in content:
                content = re.sub(
                    r"^(\s*#?\s*api_enabled:\s*).*$",
                    r"  api_enabled: true",
                    content,
                    flags=re.MULTILINE,
                )
            else:
                # Add under daemon section
                content = content.replace(
                    "daemon:",
                    "daemon:\n  api_enabled: true",
                    1,
                )
            yaml_path.write_text(content)
            print("Enabled HTTP API in config.yaml.")
            print("Restart the daemon for this to take effect:")
            print("  claude-daemon restart")
            sys.exit(0)
        else:
            print("Error: HTTP API is not enabled and config.yaml not found.")
            print("Fix: set 'api_enabled: true' in config.yaml, then restart.")
            sys.exit(1)

    base_url = f"http://127.0.0.1:{config.api_port}"
    headers = {"Content-Type": "application/json"}
    if config.api_key:
        headers["Authorization"] = f"Bearer {config.api_key}"

    agent = getattr(args, "agent", None)
    agent_label = f" (@{agent})" if agent else ""

    print(f"claude-daemon chat{agent_label}")
    print(f"Connected to {base_url}")

    # Show active warnings if any
    warnings_path = config.data_dir / "shared" / "WARNINGS.md"
    if warnings_path.exists():
        content = warnings_path.read_text().strip()
        if content:
            print("\n\033[1;33m--- Active Warnings ---\033[0m")
            for line in content.split("\n"):
                if line.startswith("# ") or line.startswith("These warnings"):
                    continue
                if line.strip():
                    print(f"  \033[33m{line}\033[0m")
            print()

    print("Type your message and press Enter. Ctrl+C to quit.\n")

    while True:
        try:
            prompt = input("you> ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nBye.")
            break

        if not prompt:
            continue
        if prompt.lower() in ("exit", "quit", "/quit", "/exit"):
            print("Bye.")
            break

        body = {"message": prompt, "user_id": "cli-user"}
        if agent:
            body["agent"] = agent

        data = json.dumps(body).encode()

        # Parse the base URL for http.client (unbuffered line-by-line SSE reads)
        parsed = urllib.parse.urlparse(base_url)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 8080

        # Show spinner until first token arrives
        stop_spinner = threading.Event()
        first_token = threading.Event()

        def _spinner():
            frames = [
                "⠋ thinking   ",
                "⠙ thinking.  ",
                "⠹ thinking.. ",
                "⠸ thinking...",
                "⠼ thinking   ",
                "⠴ thinking.  ",
                "⠦ thinking.. ",
                "⠧ thinking...",
                "⠇ thinking   ",
                "⠏ thinking.  ",
            ]
            i = 0
            while not stop_spinner.is_set():
                if first_token.is_set():
                    break
                sys.stdout.write(f"\r\033[36m{frames[i % len(frames)]}\033[0m")
                sys.stdout.flush()
                stop_spinner.wait(0.12)
                i += 1
            sys.stdout.write("\r" + " " * 20 + "\r")
            sys.stdout.flush()

        spinner_thread = threading.Thread(target=_spinner, daemon=True)
        spinner_thread.start()

        try:
            # Use http.client directly for unbuffered SSE streaming
            conn = http.client.HTTPConnection(host, port, timeout=300)
            conn.request("POST", "/api/message/stream", body=data, headers=headers)
            resp = conn.getresponse()

            if resp.status != 200:
                stop_spinner.set()
                spinner_thread.join()
                print(f"\nHTTP {resp.status}: {resp.read().decode()[:200]}\n")
                conn.close()
                continue

            got_text = False
            while True:
                raw_line = resp.readline()
                if not raw_line:
                    break
                line = raw_line.decode().strip()
                if not line.startswith("data: "):
                    continue
                try:
                    event = json.loads(line[6:])
                except json.JSONDecodeError:
                    continue

                if "text" in event:
                    if not got_text:
                        first_token.set()
                        stop_spinner.set()
                        spinner_thread.join()
                        sys.stdout.write("\r" + " " * 20 + "\r\n")
                        got_text = True
                    sys.stdout.write(event["text"])
                    sys.stdout.flush()
                elif event.get("done"):
                    if event.get("error"):
                        if not got_text:
                            first_token.set()
                            stop_spinner.set()
                            spinner_thread.join()
                            sys.stdout.write("\r" + " " * 20 + "\r")
                        print(f"\nError: {event['error']}")
                    break
                elif "error" in event:
                    first_token.set()
                    stop_spinner.set()
                    spinner_thread.join()
                    sys.stdout.write("\r" + " " * 20 + "\r")
                    print(f"\nError: {event['error']}")
                    break

            conn.close()
            if not got_text:
                stop_spinner.set()
                spinner_thread.join()
            print("\n")
        except KeyboardInterrupt:
            stop_spinner.set()
            first_token.set()
            spinner_thread.join()
            print("\n\nInterrupted. Type another message or Ctrl+C again to quit.\n")
            try:
                continue
            except KeyboardInterrupt:
                print("Bye.")
                break
        except ConnectionRefusedError:
            stop_spinner.set()
            spinner_thread.join()
            print(f"\nError: Could not connect to daemon at {base_url}")
            print("  Is the daemon running? Try: claude-daemon status\n")
        except Exception as e:
            stop_spinner.set()
            spinner_thread.join()
            print(f"\nError: {e}\n")


def _cmd_dashboard_url(args: argparse.Namespace) -> None:
    """Print a one-click URL that authenticates the browser into the dashboard."""
    from claude_daemon.core.config import DaemonConfig

    DaemonConfig.load()
    config = DaemonConfig.load()

    if not config.api_enabled:
        print("HTTP API is disabled — set api_enabled: true in config.yaml.")
        sys.exit(1)
    if not config.dashboard_enabled:
        print("Dashboard is disabled — set dashboard_enabled: true in config.yaml.")
        sys.exit(1)

    host = getattr(args, "host", None) or "localhost"
    port = config.api_port
    if not config.api_key:
        print(f"http://{host}:{port}/")
        print("(No api_key set — dashboard requires no login.)")
        return

    from urllib.parse import quote
    print(f"http://{host}:{port}/?key={quote(config.api_key)}")


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
        print("  Files: SOUL.md, IDENTITY.md, MEMORY.md")
        print("\nEdit the .md files in the workspace to customize this agent.")


def _factory_post(path: str, body: dict) -> dict | None:
    """POST to the daemon's HTTP API and return the decoded JSON body."""
    import http.client
    import json
    import urllib.parse

    from claude_daemon.core.config import DaemonConfig

    config = DaemonConfig.load()
    if not config.api_enabled:
        print("Error: HTTP API is not enabled.")
        print("Fix: set 'api_enabled: true' in config.yaml, then restart.")
        sys.exit(1)

    base_url = f"http://127.0.0.1:{config.api_port}"
    headers = {"Content-Type": "application/json"}
    if config.api_key:
        headers["Authorization"] = f"Bearer {config.api_key}"

    data = json.dumps(body).encode()
    parsed = urllib.parse.urlparse(base_url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 8080

    conn = http.client.HTTPConnection(host, port, timeout=1800)
    try:
        conn.request("POST", path, body=data, headers=headers)
        resp = conn.getresponse()
        raw = resp.read().decode("utf-8", errors="replace")
        if resp.status >= 400:
            print(f"HTTP {resp.status}: {raw}")
            sys.exit(1)
        try:
            return json.loads(raw) if raw else None
        except json.JSONDecodeError:
            print(raw)
            return None
    finally:
        conn.close()


def _cmd_plan(args: argparse.Namespace) -> None:
    """Submit a plan request to the Software Factory."""
    body: dict = {"request": args.request}
    if args.goal_id is not None:
        body["goal_id"] = args.goal_id
    result = _factory_post("/api/v1/factory/plan", body)
    if not result:
        return
    print(f"Plan: {result.get('slug', '?')}")
    if result.get("plan_path"):
        print(f"Spec: {result['plan_path']}")
    if result.get("task_id"):
        print(f"Task: {result['task_id']}")
    if result.get("approval_id"):
        print(f"Approval: {result['approval_id']}")
    if result.get("summary"):
        print()
        print(result["summary"])


def _cmd_build(args: argparse.Namespace) -> None:
    """Submit a build request to the Software Factory."""
    body: dict = {"request": args.request}
    if args.plan_path:
        body["plan_path"] = args.plan_path
    if args.max_cost:
        body["max_cost"] = args.max_cost
    if args.executors:
        body["executor_agents"] = [
            a.strip() for a in args.executors.split(",") if a.strip()
        ]
    if args.skip_plan:
        body["skip_plan"] = True
    if args.goal_id is not None:
        body["goal_id"] = args.goal_id
    result = _factory_post("/api/v1/factory/build", body)
    if not result:
        return
    print(f"Build: {result.get('slug', '?')}")
    if result.get("result_path"):
        print(f"Result: {result['result_path']}")
    if result.get("accepted") is not None:
        print(f"Accepted: {result['accepted']}")
    if result.get("iterations"):
        print(f"Iterations: {result['iterations']}")
    if result.get("summary"):
        print()
        print(result["summary"])


def _cmd_review(args: argparse.Namespace) -> None:
    """Submit a review request to the Software Factory."""
    body: dict = {}
    if args.target:
        body["target"] = args.target
    if args.max_cost:
        body["max_cost"] = args.max_cost
    result = _factory_post("/api/v1/factory/review", body)
    if not result:
        return
    print(f"Review: {result.get('slug', '?')}")
    if result.get("report_path"):
        print(f"Report: {result['report_path']}")
    findings = result.get("findings") or []
    if findings:
        print(f"Findings ({len(findings)}):")
        for f in findings:
            focus = f.get("focus", "?")
            agent = f.get("agent", "?")
            print(f"  [{focus}] @{agent}")
    if result.get("summary"):
        print()
        print(result["summary"])


def _cmd_jobs(args: argparse.Namespace) -> None:
    """List scheduled jobs."""
    from claude_daemon.core.config import DaemonConfig
    config = DaemonConfig.load()

    print("Built-in jobs:")
    print(f"  auto_update:       {config.update_cron}")
    print(f"  memory_compaction: {config.compaction_cron}")
    print(f"  auto_dream:        {config.dream_cron}")
    print(f"  heartbeat:         every {config.heartbeat_interval}s")
    print("  session_cleanup:   every 6h")

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

    # chat
    p_chat = sub.add_parser("chat", help="Interactive chat with daemon agents")
    p_chat.add_argument("--agent", "-a", help="Target agent (e.g. albert, luna)")

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
    p_update = sub.add_parser(
        "update",
        help="Fetch latest install.sh from GitHub and run it with --update "
             "(preserves .env, config, memories)",
    )
    p_update.add_argument("--check-only", action="store_true",
                          help="Only report whether the local checkout is behind; do not apply")
    p_update.add_argument("--local", action="store_true",
                          help="Use the install.sh already on disk instead of fetching from GitHub")

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

    # shared-brain
    p_sb = sub.add_parser(
        "shared-brain",
        help="Manage the shared brain digest (exposes daemon agents to Claude Code CLI/macOS)",
    )
    p_sb_sub = p_sb.add_subparsers(dest="sb_action")
    p_sb_sub.add_parser("sync", help="Regenerate shared-brain.md now")
    p_sb_sub.add_parser("show", help="Print current shared-brain.md")
    p_sb_sub.add_parser(
        "install",
        help="Add @import to ~/.claude/CLAUDE.md so Claude Code CLI + macOS pick it up",
    )
    p_sb_sub.add_parser(
        "uninstall", help="Remove the @import block from ~/.claude/CLAUDE.md"
    )
    p_sb_sub.add_parser(
        "status", help="Show install state, last sync, file size",
    )

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

    # dashboard-url
    p_durl = sub.add_parser(
        "dashboard-url",
        help="Print a ?key=<api_key> URL for one-click dashboard login",
    )
    p_durl.add_argument(
        "--host", default=None,
        help="Override hostname (default: localhost)",
    )

    # software factory: plan / build / review
    p_plan = sub.add_parser(
        "plan",
        help="Create an implementation plan via the Software Factory "
             "(writes a spec artifact + optional approval row)",
    )
    p_plan.add_argument("request", help="Feature request to plan")
    p_plan.add_argument(
        "--goal-id", type=int, default=None, help="Optional Goal ID to link",
    )

    p_build = sub.add_parser(
        "build",
        help="Run the full plan -> execute -> review loop",
    )
    p_build.add_argument("request", help="Feature request to build")
    p_build.add_argument(
        "--plan-path", default=None,
        help="Existing plan markdown file to use (skip auto-planning)",
    )
    p_build.add_argument(
        "--max-cost", type=float, default=0.0,
        help="Cost cap in USD (0 = unlimited)",
    )
    p_build.add_argument(
        "--executors", default=None,
        help="Comma-separated executor agent names (overrides factory config)",
    )
    p_build.add_argument(
        "--skip-plan", action="store_true",
        help="Skip auto-planning (use only if the request is trivial)",
    )
    p_build.add_argument(
        "--goal-id", type=int, default=None,
        help="Optional Goal ID to link this build to",
    )

    p_review = sub.add_parser(
        "review",
        help="Run a parallel multi-focus code review on a diff target",
    )
    p_review.add_argument(
        "target", nargs="?", default="",
        help="Diff target (default: current branch vs main)",
    )
    p_review.add_argument(
        "--max-cost", type=float, default=0.0,
        help="Cost cap in USD (0 = unlimited)",
    )

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
        "chat": _cmd_chat,
        "logs": _cmd_logs,
        "config": _cmd_config,
        "memory": _cmd_memory,
        "update": _cmd_update,
        "install-service": _cmd_install_service,
        "jobs": _cmd_jobs,
        "env": _cmd_env,
        "mcp": _cmd_mcp,
        "shared-brain": _cmd_shared_brain,
        "thinking": _cmd_thinking,
        "effort": _cmd_effort,
        "backend": _cmd_backend,
        "dashboard-url": _cmd_dashboard_url,
        "agents": _cmd_agents,
        "plan": _cmd_plan,
        "build": _cmd_build,
        "review": _cmd_review,
    }

    handler = commands.get(args.command)
    if handler:
        handler(args)
    else:
        parser.print_help()
