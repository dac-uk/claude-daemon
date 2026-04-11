"""ProcessManager - manages Claude Code CLI subprocesses.

Each message spawns a `claude --print` one-shot subprocess.
Session continuity is achieved via `--resume <session_id>`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from claude_daemon.core.config import DaemonConfig

log = logging.getLogger(__name__)


@dataclass
class ClaudeResponse:
    """Parsed response from Claude Code CLI JSON output."""

    result: str
    session_id: str
    cost: float
    input_tokens: int
    output_tokens: int
    num_turns: int
    duration_ms: int
    is_error: bool
    raw: dict = field(repr=False, default_factory=dict)

    @classmethod
    def from_json(cls, data: dict) -> ClaudeResponse:
        usage = data.get("usage", {})
        return cls(
            result=data.get("result", ""),
            session_id=data.get("session_id", ""),
            cost=float(data.get("total_cost_usd", 0)),
            input_tokens=int(usage.get("input_tokens", 0)),
            output_tokens=int(usage.get("output_tokens", 0)),
            num_turns=int(data.get("num_turns", 0)),
            duration_ms=int(data.get("duration_ms", 0)),
            is_error=bool(data.get("is_error", False)),
            raw=data,
        )

    @classmethod
    def error(cls, message: str) -> ClaudeResponse:
        return cls(
            result=message,
            session_id="",
            cost=0,
            input_tokens=0,
            output_tokens=0,
            num_turns=0,
            duration_ms=0,
            is_error=True,
        )


@dataclass
class ActiveSession:
    """Tracks an in-flight Claude subprocess."""

    session_id: str
    platform: str
    user_id: str
    started_at: datetime
    process: asyncio.subprocess.Process


class ProcessManager:
    """Manages concurrent Claude Code CLI invocations."""

    def __init__(self, config: DaemonConfig) -> None:
        self.config = config
        self._semaphore = asyncio.Semaphore(config.max_concurrent_sessions)
        self._active: dict[str, ActiveSession] = {}

    @property
    def active_count(self) -> int:
        return len(self._active)

    async def send_message(
        self,
        prompt: str,
        session_id: str | None = None,
        system_context: str | None = None,
        max_budget: float | None = None,
        platform: str = "cli",
        user_id: str = "local",
    ) -> ClaudeResponse:
        """Send a prompt to Claude Code CLI and return the parsed response.

        Uses --resume for session continuity when session_id is provided.
        Enforces concurrency limits via semaphore.
        """
        async with self._semaphore:
            return await self._execute(
                prompt=prompt,
                session_id=session_id,
                system_context=system_context,
                max_budget=max_budget or self.config.max_budget_per_message,
                platform=platform,
                user_id=user_id,
            )

    async def _execute(
        self,
        prompt: str,
        session_id: str | None,
        system_context: str | None,
        max_budget: float,
        platform: str,
        user_id: str,
    ) -> ClaudeResponse:
        """Execute claude CLI as a subprocess."""
        args = [
            self.config.claude_binary,
            "--print",
            "--output-format", "json",
            "--bare",
            "--permission-mode", self.config.permission_mode,
        ]

        if session_id:
            args.extend(["--resume", session_id])
        else:
            new_id = str(uuid.uuid4())
            args.extend(["--session-id", new_id])

        if self.config.default_model:
            args.extend(["--model", self.config.default_model])

        if max_budget > 0:
            args.extend(["--max-budget-usd", str(max_budget)])

        if system_context:
            args.extend(["--append-system-prompt", system_context])

        args.append(prompt)

        tracking_id = session_id or "new"
        log.debug("Executing Claude: session=%s, prompt_len=%d", tracking_id, len(prompt))

        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            # Track active session
            active = ActiveSession(
                session_id=tracking_id,
                platform=platform,
                user_id=user_id,
                started_at=datetime.now(timezone.utc),
                process=proc,
            )
            self._active[tracking_id] = active

            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=300,  # 5 minute timeout
                )
            finally:
                self._active.pop(tracking_id, None)

            if proc.returncode != 0 and not stdout:
                err_msg = stderr.decode().strip() if stderr else f"Exit code {proc.returncode}"
                log.error("Claude CLI error: %s", err_msg)
                return ClaudeResponse.error(f"Claude Code error: {err_msg}")

            # Parse JSON response
            raw = stdout.decode().strip()
            if not raw:
                return ClaudeResponse.error("Empty response from Claude Code")

            data = json.loads(raw)
            response = ClaudeResponse.from_json(data)

            log.info(
                "Claude response: session=%s, tokens=%d/%d, cost=$%.4f, turns=%d, duration=%dms",
                response.session_id,
                response.input_tokens,
                response.output_tokens,
                response.cost,
                response.num_turns,
                response.duration_ms,
            )

            if response.is_error:
                log.warning("Claude returned error: %s", response.result[:200])

            return response

        except asyncio.TimeoutError:
            log.error("Claude CLI timed out after 300s")
            return ClaudeResponse.error("Claude Code timed out after 5 minutes")
        except json.JSONDecodeError as e:
            log.error("Failed to parse Claude response: %s", e)
            return ClaudeResponse.error(f"Failed to parse response: {e}")
        except FileNotFoundError:
            log.error("Claude binary not found: %s", self.config.claude_binary)
            return ClaudeResponse.error(
                f"Claude Code not found at '{self.config.claude_binary}'. "
                "Install with: npm install -g @anthropic-ai/claude-code"
            )
        except Exception as e:
            log.exception("Unexpected error running Claude")
            return ClaudeResponse.error(f"Unexpected error: {e}")

    async def drain_all(self, timeout: float = 60) -> None:
        """Wait for all active Claude processes to finish."""
        if not self._active:
            return

        log.info("Draining %d active Claude processes...", len(self._active))
        procs = [s.process for s in self._active.values()]

        try:
            await asyncio.wait_for(
                asyncio.gather(*(p.wait() for p in procs), return_exceptions=True),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            log.warning("Drain timeout, killing remaining processes")
            for p in procs:
                try:
                    p.kill()
                except ProcessLookupError:
                    pass
