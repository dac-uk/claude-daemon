"""ProcessManager - manages Claude Code CLI subprocesses.

Supports both buffered (--output-format json) and streaming
(--output-format stream-json) modes. Session continuity via --resume.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, AsyncIterator

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
            result=message, session_id="", cost=0, input_tokens=0,
            output_tokens=0, num_turns=0, duration_ms=0, is_error=True,
        )


@dataclass
class ActiveSession:
    """Tracks an in-flight Claude subprocess."""

    session_id: str
    platform: str
    user_id: str
    started_at: datetime
    process: asyncio.subprocess.Process
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class ProcessManager:
    """Manages concurrent Claude Code CLI invocations with streaming support."""

    def __init__(self, config: DaemonConfig) -> None:
        self.config = config
        self._semaphore = asyncio.Semaphore(config.max_concurrent_sessions)
        self._active: dict[str, ActiveSession] = {}
        self._session_locks: dict[str, asyncio.Lock] = {}

    @property
    def active_count(self) -> int:
        return len(self._active)

    def _get_session_lock(self, session_id: str) -> asyncio.Lock:
        """Get or create a per-session lock to prevent concurrent resume collisions."""
        if session_id not in self._session_locks:
            self._session_locks[session_id] = asyncio.Lock()
        return self._session_locks[session_id]

    def _build_args(
        self,
        prompt: str,
        session_id: str | None,
        system_context: str | None,
        max_budget: float,
        output_format: str = "json",
    ) -> tuple[list[str], str]:
        """Build the claude CLI arguments. Returns (args, tracking_id)."""
        args = [
            self.config.claude_binary,
            "--print",
            "--output-format", output_format,
            "--bare",
            "--permission-mode", self.config.permission_mode,
        ]

        tracking_id = session_id or str(uuid.uuid4())
        if session_id:
            args.extend(["--resume", session_id])
        else:
            args.extend(["--session-id", tracking_id])

        if self.config.default_model:
            args.extend(["--model", self.config.default_model])

        if max_budget > 0:
            args.extend(["--max-budget-usd", str(max_budget)])

        if self.config.mcp_config:
            args.extend(["--mcp-config", self.config.mcp_config])

        if system_context:
            args.extend(["--append-system-prompt", system_context])

        args.append(prompt)
        return args, tracking_id

    async def send_message(
        self,
        prompt: str,
        session_id: str | None = None,
        system_context: str | None = None,
        max_budget: float | None = None,
        platform: str = "cli",
        user_id: str = "local",
    ) -> ClaudeResponse:
        """Send a prompt and return the complete response (buffered mode)."""
        budget = max_budget if max_budget is not None else self.config.max_budget_per_message

        # Lock per-session to prevent concurrent resume of same session
        if session_id:
            lock = self._get_session_lock(session_id)
            async with lock:
                async with self._semaphore:
                    return await self._execute_buffered(
                        prompt, session_id, system_context, budget, platform, user_id,
                    )
        else:
            async with self._semaphore:
                return await self._execute_buffered(
                    prompt, session_id, system_context, budget, platform, user_id,
                )

    async def stream_message(
        self,
        prompt: str,
        session_id: str | None = None,
        system_context: str | None = None,
        max_budget: float | None = None,
        platform: str = "cli",
        user_id: str = "local",
    ) -> AsyncIterator[str | ClaudeResponse]:
        """Stream a response token-by-token. Yields text chunks, then final ClaudeResponse.

        Usage:
            async for chunk in pm.stream_message("hello"):
                if isinstance(chunk, str):
                    # partial text
                elif isinstance(chunk, ClaudeResponse):
                    # final result with metadata
        """
        budget = max_budget if max_budget is not None else self.config.max_budget_per_message
        args, tracking_id = self._build_args(
            prompt, session_id, system_context, budget, output_format="stream-json",
        )

        log.debug("Streaming Claude: session=%s, prompt_len=%d", tracking_id, len(prompt))

        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            active = ActiveSession(
                session_id=tracking_id, platform=platform, user_id=user_id,
                started_at=datetime.now(timezone.utc), process=proc,
            )
            self._active[tracking_id] = active

            accumulated_text = ""
            final_response = None

            try:
                async for line in proc.stdout:
                    line = line.decode().strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    event_type = event.get("type", "")

                    if event_type == "assistant":
                        # Content block with text delta
                        msg = event.get("message", {})
                        content = msg.get("content", "")
                        if isinstance(content, str) and content:
                            delta = content[len(accumulated_text):]
                            if delta:
                                accumulated_text = content
                                yield delta

                    elif event_type == "result":
                        # Final result event
                        final_response = ClaudeResponse.from_json(event)

                await proc.wait()

            finally:
                self._active.pop(tracking_id, None)

            if final_response:
                log.info(
                    "Stream complete: session=%s, cost=$%.4f, turns=%d",
                    final_response.session_id, final_response.cost, final_response.num_turns,
                )
                yield final_response
            else:
                # Build response from accumulated text
                yield ClaudeResponse(
                    result=accumulated_text, session_id=tracking_id, cost=0,
                    input_tokens=0, output_tokens=0, num_turns=0, duration_ms=0,
                    is_error=not accumulated_text,
                )

        except FileNotFoundError:
            yield ClaudeResponse.error(
                f"Claude Code not found at '{self.config.claude_binary}'. "
                "Install with: npm install -g @anthropic-ai/claude-code"
            )
        except Exception as e:
            log.exception("Streaming error")
            yield ClaudeResponse.error(f"Streaming error: {e}")

    async def _execute_buffered(
        self, prompt: str, session_id: str | None, system_context: str | None,
        max_budget: float, platform: str, user_id: str,
    ) -> ClaudeResponse:
        """Execute claude CLI as a buffered subprocess."""
        args, tracking_id = self._build_args(
            prompt, session_id, system_context, max_budget,
        )

        log.debug("Executing Claude: session=%s, prompt_len=%d", tracking_id, len(prompt))

        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            active = ActiveSession(
                session_id=tracking_id, platform=platform, user_id=user_id,
                started_at=datetime.now(timezone.utc), process=proc,
            )
            self._active[tracking_id] = active

            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=self.config.process_timeout,
                )
            finally:
                self._active.pop(tracking_id, None)

            if proc.returncode != 0 and not stdout:
                err_msg = stderr.decode().strip() if stderr else f"Exit code {proc.returncode}"
                log.error("Claude CLI error: %s", err_msg)
                return ClaudeResponse.error(f"Claude Code error: {err_msg}")

            raw = stdout.decode().strip()
            if not raw:
                return ClaudeResponse.error("Empty response from Claude Code")

            data = json.loads(raw)
            response = ClaudeResponse.from_json(data)

            log.info(
                "Claude response: session=%s, tokens=%d/%d, cost=$%.4f, turns=%d",
                response.session_id, response.input_tokens, response.output_tokens,
                response.cost, response.num_turns,
            )

            return response

        except asyncio.TimeoutError:
            log.error("Claude CLI timed out after %ds", self.config.process_timeout)
            # Kill the hung process
            if tracking_id in self._active:
                try:
                    self._active[tracking_id].process.kill()
                except ProcessLookupError:
                    pass
                self._active.pop(tracking_id, None)
            return ClaudeResponse.error(
                f"Claude Code timed out after {self.config.process_timeout}s"
            )
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
            self._active.clear()
