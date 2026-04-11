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
    }

    handler = commands.get(args.command)
    if handler:
        handler(args)
    else:
        parser.print_help()
