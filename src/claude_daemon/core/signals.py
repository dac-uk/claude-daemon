"""Signal handling for graceful shutdown and config reload."""

from __future__ import annotations

import asyncio
import logging
import signal
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from claude_daemon.core.daemon import ClaudeDaemon

log = logging.getLogger(__name__)


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
