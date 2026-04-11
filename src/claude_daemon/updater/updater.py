"""Updater - auto-update Claude Code and the daemon itself."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from claude_daemon.core.config import DaemonConfig
    from claude_daemon.core.process import ProcessManager

log = logging.getLogger(__name__)


@dataclass
class UpdateResult:
    """Result of an update check/apply."""

    current_version: str
    updated: bool
    new_version: str | None = None
    error: str | None = None

    def __str__(self) -> str:
        if self.error:
            return f"Update check failed: {self.error}"
        if self.updated:
            return f"Updated Claude Code: {self.current_version} -> {self.new_version}"
        return f"Claude Code is up to date ({self.current_version})"


class Updater:
    """Manages Claude Code and self-updates."""

    def __init__(self, config: DaemonConfig, process_manager: ProcessManager) -> None:
        self.config = config
        self.pm = process_manager

    async def get_current_version(self) -> str:
        """Get the currently installed Claude Code version."""
        try:
            proc = await asyncio.create_subprocess_exec(
                self.config.claude_binary, "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
            return stdout.decode().strip()
        except Exception as e:
            log.error("Failed to get Claude version: %s", e)
            return "unknown"

    async def check_and_update(self, check_only: bool = False) -> UpdateResult:
        """Check for Claude Code updates and optionally apply them.

        Uses the built-in `claude update` command.
        """
        current = await self.get_current_version()
        log.info("Current Claude Code version: %s", current)

        if check_only:
            # Just report the version
            return UpdateResult(current_version=current, updated=False)

        try:
            # Drain active sessions before updating
            await self.pm.drain_all(timeout=120)

            # Run claude update
            proc = await asyncio.create_subprocess_exec(
                self.config.claude_binary, "update",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)

            output = stdout.decode().strip()
            err = stderr.decode().strip()

            if proc.returncode != 0:
                log.warning("claude update returned non-zero: %s", err or output)
                return UpdateResult(
                    current_version=current,
                    updated=False,
                    error=err or output or f"Exit code {proc.returncode}",
                )

            # Check new version
            new_version = await self.get_current_version()
            updated = new_version != current

            if updated:
                log.info("Claude Code updated: %s -> %s", current, new_version)
            else:
                log.info("Claude Code already up to date: %s", current)

            return UpdateResult(
                current_version=current,
                updated=updated,
                new_version=new_version if updated else None,
            )

        except asyncio.TimeoutError:
            return UpdateResult(
                current_version=current,
                updated=False,
                error="Update timed out after 120s",
            )
        except Exception as e:
            log.exception("Update failed")
            return UpdateResult(
                current_version=current,
                updated=False,
                error=str(e),
            )

    async def self_update(self) -> UpdateResult:
        """Update claude-daemon itself via pip."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "pip", "install", "--upgrade", "claude-daemon",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)

            if proc.returncode == 0:
                log.info("claude-daemon self-update complete")
                return UpdateResult(current_version="0.1.0", updated=True, new_version="latest")
            else:
                err = stderr.decode().strip()
                return UpdateResult(current_version="0.1.0", updated=False, error=err)

        except Exception as e:
            return UpdateResult(current_version="0.1.0", updated=False, error=str(e))
