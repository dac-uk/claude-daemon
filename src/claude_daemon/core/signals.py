"""Signal handling for graceful shutdown, config reload, and systemd integration."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import socket
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from claude_daemon.core.daemon import ClaudeDaemon

log = logging.getLogger(__name__)


def sd_notify(state: str) -> bool:
    """Send a notification to systemd (if running under systemd).

    Common states:
      READY=1       — daemon is ready
      WATCHDOG=1    — watchdog ping (prevents restart)
      STOPPING=1    — daemon is shutting down

    Returns True if the notification was sent, False otherwise.
    Uses the sd_notify socket protocol directly to avoid requiring systemd-python.
    """
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return False
    try:
        if addr[0] == "@":
            addr = "\0" + addr[1:]
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            sock.sendto(state.encode(), addr)
        finally:
            sock.close()
        return True
    except OSError:
        return False


def install_signal_handlers(daemon: ClaudeDaemon, loop: asyncio.AbstractEventLoop) -> None:
    """Register SIGTERM/SIGINT for shutdown and SIGHUP for config reload."""

    def _request_shutdown(sig: signal.Signals) -> None:
        log.info("Received %s, initiating graceful shutdown...", sig.name)
        daemon.request_shutdown()

    def _request_reload(sig: signal.Signals) -> None:
        log.info("Received SIGHUP, reloading configuration...")
        loop.create_task(daemon.reload_config())

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _request_shutdown, sig)

    # SIGHUP for config reload (Unix only)
    try:
        loop.add_signal_handler(signal.SIGHUP, _request_reload, signal.SIGHUP)
    except (NotImplementedError, OSError):
        pass  # Windows doesn't support SIGHUP
