"""ProcessManager - manages Claude Code CLI subprocesses.

Supports both buffered (--output-format json) and streaming
(--output-format stream-json) modes. Session continuity via --resume.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, AsyncIterator

if TYPE_CHECKING:
    from claude_daemon.core.config import DaemonConfig
    from claude_daemon.core.managed_agents import ManagedAgentBackend

log = logging.getLogger(__name__)

# Patterns that indicate a rate limit or model unavailability error
_RATE_LIMIT_PATTERNS = re.compile(
    r"rate.?limit|429|overloaded|capacity|unavailable|too many requests|quota.?exceeded",
    re.IGNORECASE,
)


def _is_rate_limit_error(stderr_text: str, response: "ClaudeResponse | None" = None) -> bool:
    """Detect rate limit / model unavailability in stderr or error response."""
    if _RATE_LIMIT_PATTERNS.search(stderr_text):
        return True
    if response and response.is_error and _RATE_LIMIT_PATTERNS.search(response.result):
        return True
    return False


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
    model_used: str = ""
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
    """Manages concurrent Claude Code CLI invocations with streaming support.

    Supports triple-backend execution:
    1. SDK bridge (preferred) — persistent sessions via Agent SDK, sub-second response
    2. Managed Agents API — Anthropic-hosted, for long-running tasks
    3. CLI subprocess (fallback) — spawns claude --print per message
    """

    # Max cached session locks before eviction (prevents unbounded growth)
    _MAX_SESSION_LOCKS = 500

    def __init__(self, config: DaemonConfig) -> None:
        self.config = config
        self._semaphore = asyncio.Semaphore(config.max_concurrent_sessions)
        self._agent_semaphores: dict[str, asyncio.Semaphore] = {}
        self._active: dict[str, ActiveSession] = {}
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._confirmed_sessions: set[str] = set()  # sessions Claude Code has seen
        self._managed: "ManagedAgentBackend | None" = None  # lazy init
        self._sdk_bridge: "SDKBridgeManager | None" = None  # lazy init
        self._sdk_bridge_disabled: bool = False  # set True if bridge can't start

    def get_agent_semaphore(self, agent_name: str, max_per_agent: int = 3) -> asyncio.Semaphore:
        """Get or create a per-agent semaphore to prevent one agent hogging all slots."""
        if agent_name not in self._agent_semaphores:
            self._agent_semaphores[agent_name] = asyncio.Semaphore(max_per_agent)
        return self._agent_semaphores[agent_name]

    @property
    def active_count(self) -> int:
        return len(self._active)

    def is_session_busy(self, session_id: str) -> bool:
        """Check if a session currently has an active Claude subprocess."""
        return session_id in self._active

    def _get_session_lock(self, session_id: str) -> asyncio.Lock:
        """Get or create a per-session lock to prevent concurrent resume collisions.

        Evicts idle locks when cache exceeds _MAX_SESSION_LOCKS to prevent unbounded growth.
        """
        if session_id not in self._session_locks:
            # Evict unlocked sessions if cache is too large
            if len(self._session_locks) >= self._MAX_SESSION_LOCKS:
                idle = [k for k, v in self._session_locks.items() if not v.locked()]
                for k in idle[:len(idle) // 2]:  # Remove half of idle locks
                    del self._session_locks[k]
            self._session_locks[session_id] = asyncio.Lock()
        return self._session_locks[session_id]

    @property
    def managed(self) -> "ManagedAgentBackend | None":
        """Lazy-init Managed Agents backend if API key is available."""
        if self._managed is None and os.getenv("ANTHROPIC_API_KEY"):
            try:
                from claude_daemon.core.managed_agents import ManagedAgentBackend
                self._managed = ManagedAgentBackend(self.config)
            except ImportError:
                log.debug("anthropic SDK not installed, Managed Agents unavailable")
        return self._managed

    async def ensure_sdk_bridge(self) -> "SDKBridgeManager | None":
        """Lazy-init the SDK bridge. Returns None if unavailable."""
        if self._sdk_bridge_disabled:
            return None
        if self._sdk_bridge and self._sdk_bridge.is_alive:
            return self._sdk_bridge

        if not getattr(self.config, "sdk_bridge_enabled", True):
            self._sdk_bridge_disabled = True
            return None

        try:
            from claude_daemon.core.sdk_bridge import SDKBridgeManager
            bridge = SDKBridgeManager(self.config)
            await bridge.start()
            self._sdk_bridge = bridge
            return bridge
        except FileNotFoundError as e:
            log.warning("SDK bridge unavailable: %s", e)
            self._sdk_bridge_disabled = True
            return None
        except Exception as e:
            log.warning("SDK bridge failed to start: %s", e)
            self._sdk_bridge_disabled = True
            return None

    async def ensure_agent_session(
        self,
        agent_name: str,
        model: str,
        system_prompt: str = "",
        mcp_config_path: str | None = None,
        settings_path: str | None = None,
        agent_workspace: str | None = None,
    ) -> bool:
        """Ensure an (agent, model) SDK session exists. Creates if needed."""
        bridge = await self.ensure_sdk_bridge()
        if not bridge:
            return False
        if bridge.has_session(agent_name, model):
            return True
        session_id = await bridge.create_session(
            agent_name=agent_name,
            model=model,
            system_prompt=system_prompt,
            mcp_config_path=mcp_config_path,
            settings_path=settings_path,
            agent_workspace=agent_workspace,
        )
        return session_id is not None

    def _should_use_managed(self, task_type: str, agent_name: str | None) -> bool:
        """Route to Managed Agents or CLI based on task characteristics."""
        if not self.managed:
            return False
        if not self.config.managed_agents_enabled:
            return False
        return task_type in self.config.managed_agents_task_types

    def _subprocess_env(self) -> dict[str, str]:
        """Build subprocess environment with Claude CLI overrides."""
        env = os.environ.copy()
        # Override the 90s default idle timeout — Opus thinking phases can take several minutes
        env["CLAUDE_STREAM_IDLE_TIMEOUT_MS"] = str(self.config.stream_idle_timeout_ms)
        # Auto-compact at configured % to prevent context degradation in long sessions
        env["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"] = str(self.config.auto_compact_pct)
        return env

    def _build_args(
        self,
        prompt: str,
        session_id: str | None,
        system_context: str | None,
        max_budget: float,
        output_format: str = "json",
        model_override: str | None = None,
        mcp_config_path: str | None = None,
        settings_path: str | None = None,
        effort: str | None = None,
    ) -> tuple[list[str], str]:
        """Build the claude CLI arguments. Returns (args, tracking_id)."""
        args = [
            self.config.claude_binary,
            "--print",
            "--output-format", output_format,
            "--permission-mode", self.config.permission_mode,
        ]

        # Recent Claude CLI versions require --verbose when --print is combined
        # with --output-format=stream-json; buffered json does not.
        if output_format == "stream-json":
            args.append("--verbose")

        # Use --bare when API key auth is available (faster, no hooks/LSP overhead).
        # Skip --bare for OAuth/subscription users — bare blocks keychain/OAuth reads.
        if os.environ.get("ANTHROPIC_API_KEY"):
            args.append("--bare")

        tracking_id = session_id or str(uuid.uuid4())
        if session_id and session_id in self._confirmed_sessions:
            # Only --resume sessions that Claude Code has actually seen
            args.extend(["--resume", session_id])
        # else: don't pass --session-id or --resume — let Claude Code
        # generate its own session ID. We capture it from the response
        # and mark it as confirmed for future --resume.

        # Model: override > config default
        model = model_override or self.config.default_model
        if model:
            args.extend(["--model", model])

        if max_budget > 0:
            args.extend(["--max-budget-usd", str(max_budget)])

        # MCP config: per-agent override > global config
        mcp_path = mcp_config_path or self.config.mcp_config
        if mcp_path:
            args.extend(["--mcp-config", mcp_path])

        # Per-agent settings.json (permissions, thinking, etc.)
        if settings_path:
            args.extend(["--settings", settings_path])

        # Effort level: controls reasoning depth within a model
        if effort:
            args.extend(["--effort", effort])

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
        model_override: str | None = None,
        mcp_config_path: str | None = None,
        settings_path: str | None = None,
        effort: str | None = None,
        task_type: str = "default",
        agent_name: str | None = None,
    ) -> ClaudeResponse:
        """Send a prompt and return the complete response (buffered mode).

        Auto-parallel: if the target session is already processing, a fresh
        session is started automatically instead of blocking.

        If managed_agents_enabled and task_type matches, routes through the
        Managed Agents API instead of CLI subprocess. Falls back to CLI on error.
        """
        budget = max_budget if max_budget is not None else self.config.max_budget_per_message

        # Managed Agents routing (with automatic fallback to CLI)
        if agent_name and self._should_use_managed(task_type, agent_name):
            try:
                return await self._managed.send_message(
                    prompt=prompt,
                    agent_name=agent_name,
                    session_id=session_id,
                    system_context=system_context,
                    max_budget=budget,
                    platform=platform,
                    user_id=user_id,
                    model_override=model_override,
                    settings_path=settings_path,
                    effort=effort,
                )
            except Exception as e:
                log.warning(
                    "Managed Agents failed for %s (task_type=%s), falling back to CLI: %s",
                    agent_name, task_type, e,
                )
                # Fall through to CLI execution

        # SDK bridge routing — persistent sessions (fast, no subprocess spawn).
        # Matches by (agent, model): each agent can have multiple warm sessions
        # for different models (e.g. Johnny has sonnet for chat + opus for planning).
        # If no warm session exists for this (agent, model), falls through to subprocess.
        sdk_model = model_override or self.config.default_model
        if (agent_name and self._sdk_bridge
                and self._sdk_bridge.has_session(agent_name, sdk_model)):
            try:
                import time as _time
                t0 = _time.monotonic()
                response = await self._sdk_bridge.send_message(
                    agent_name=agent_name,
                    prompt=prompt,
                    context=system_context,
                    model=sdk_model,
                )
                elapsed = _time.monotonic() - t0
                if not response.is_error:
                    if response.session_id:
                        self._confirmed_sessions.add(response.session_id)
                    log.info("SDK response for %s in %.1fs", agent_name, elapsed)
                    return response
                log.warning("SDK send failed for %s:%s (%.1fs), falling back: %s",
                            agent_name, sdk_model, elapsed, response.result[:200])
                # Remove dead session so next call recreates
                self._sdk_bridge._sessions.pop(
                    self._sdk_bridge._key(agent_name, sdk_model), None,
                )
            except Exception as e:
                log.warning("SDK bridge error for %s:%s, falling back to CLI: %s",
                            agent_name, sdk_model, e)

        if session_id:
            lock = self._get_session_lock(session_id)
            if lock.locked():
                # Session is busy — auto-spawn on a fresh session instead of blocking
                log.info("Auto-parallel: session %s busy, starting fresh session", session_id[:8])
                async with self._semaphore:
                    return await self._execute_buffered(
                        prompt, None, system_context, budget, platform, user_id,
                        model_override, mcp_config_path, settings_path, effort,
                    )
            async with lock:
                async with self._semaphore:
                    return await self._execute_buffered(
                        prompt, session_id, system_context, budget, platform, user_id,
                        model_override, mcp_config_path, settings_path, effort,
                    )
        else:
            async with self._semaphore:
                return await self._execute_buffered(
                    prompt, session_id, system_context, budget, platform, user_id,
                    model_override, mcp_config_path, settings_path, effort,
                )

    async def stream_message(
        self,
        prompt: str,
        session_id: str | None = None,
        system_context: str | None = None,
        max_budget: float | None = None,
        platform: str = "cli",
        user_id: str = "local",
        model_override: str | None = None,
        mcp_config_path: str | None = None,
        settings_path: str | None = None,
        effort: str | None = None,
        task_type: str = "default",
        agent_name: str | None = None,
    ) -> AsyncIterator[str | ClaudeResponse]:
        """Stream a response token-by-token. Yields text chunks, then final ClaudeResponse.

        Auto-parallel: if the target session has an active subprocess, a fresh
        session is started automatically instead of colliding on --resume.

        If managed_agents_enabled and task_type matches, routes through the
        Managed Agents API. Falls back to CLI on error.

        Usage:
            async for chunk in pm.stream_message("hello"):
                if isinstance(chunk, str):
                    # partial text
                elif isinstance(chunk, ClaudeResponse):
                    # final result with metadata
        """
        budget = max_budget if max_budget is not None else self.config.max_budget_per_message

        # Managed Agents routing (with automatic fallback to CLI)
        if agent_name and self._should_use_managed(task_type, agent_name):
            try:
                async for chunk in self._managed.stream_message(
                    prompt=prompt,
                    agent_name=agent_name,
                    session_id=session_id,
                    system_context=system_context,
                    max_budget=budget,
                    platform=platform,
                    user_id=user_id,
                    model_override=model_override,
                    settings_path=settings_path,
                    effort=effort,
                ):
                    yield chunk
                return
            except Exception as e:
                log.warning(
                    "Managed Agents streaming failed for %s, falling back to CLI: %s",
                    agent_name, e,
                )
                # Fall through to CLI execution

        # SDK bridge streaming — persistent sessions keyed by (agent, model)
        sdk_model = model_override or self.config.default_model
        if (agent_name and self._sdk_bridge
                and self._sdk_bridge.has_session(agent_name, sdk_model)):
            sdk_ok = False
            try:
                async for chunk in self._sdk_bridge.stream_message(
                    agent_name=agent_name,
                    prompt=prompt,
                    context=system_context,
                    model=sdk_model,
                ):
                    if isinstance(chunk, ClaudeResponse):
                        if chunk.session_id:
                            self._confirmed_sessions.add(chunk.session_id)
                        if chunk.is_error:
                            log.warning(
                                "SDK bridge error for %s:%s: %s — falling back to CLI",
                                agent_name, sdk_model, chunk.result[:200],
                            )
                            break  # Don't yield the error; fall through to CLI
                        sdk_ok = True
                    yield chunk
                if sdk_ok:
                    return
            except Exception as e:
                log.warning("SDK streaming error for %s:%s, falling back: %s",
                            agent_name, sdk_model, e)
            # SDK path failed or errored — invalidate session and fall through to CLI
            self._sdk_bridge._sessions.pop(
                self._sdk_bridge._key(agent_name, sdk_model), None,
            )
            log.info("Falling back to CLI subprocess for %s", agent_name)

        # Auto-parallel: if this session already has a running subprocess, start fresh
        if session_id and session_id in self._active:
            log.info("Auto-parallel: session %s active, starting fresh session", session_id[:8])
            session_id = None

        args, tracking_id = self._build_args(
            prompt, session_id, system_context, budget,
            output_format="stream-json", model_override=model_override,
            mcp_config_path=mcp_config_path,
            settings_path=settings_path, effort=effort,
        )

        log.debug("Streaming Claude: session=%s, model=%s, prompt_len=%d",
                   tracking_id, model_override or "default", len(prompt))

        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._subprocess_env(),
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
                # Mark session as confirmed — safe to --resume on next message
                if final_response.session_id:
                    self._confirmed_sessions.add(final_response.session_id)
                log.info(
                    "Stream complete: session=%s, cost=$%.4f, turns=%d",
                    final_response.session_id, final_response.cost, final_response.num_turns,
                )
                yield final_response
            elif accumulated_text:
                # Partial stream without a final `result` event — still usable.
                yield ClaudeResponse(
                    result=accumulated_text, session_id=tracking_id, cost=0,
                    input_tokens=0, output_tokens=0, num_turns=0, duration_ms=0,
                    is_error=False,
                )
            else:
                # No output at all. Capture stderr so the user can see what broke.
                stderr_text = ""
                try:
                    stderr_bytes = await asyncio.wait_for(proc.stderr.read(), timeout=2.0)
                    stderr_text = stderr_bytes.decode(errors="replace").strip()
                except (asyncio.TimeoutError, Exception):
                    pass
                exit_code = proc.returncode
                msg = stderr_text or (
                    f"Claude CLI exited with code {exit_code} and produced no output. "
                    "Check that ANTHROPIC_API_KEY is set and that the `claude` binary is installed."
                )
                log.error(
                    "Claude CLI produced no output. exit_code=%s stderr=%r",
                    exit_code, stderr_text,
                )
                yield ClaudeResponse.error(msg)

        except FileNotFoundError:
            yield ClaudeResponse.error(
                f"Claude Code not found at '{self.config.claude_binary}'. "
                "Install with: npm install -g @anthropic-ai/claude-code"
            )
        except Exception as e:
            log.exception("Streaming error")
            yield ClaudeResponse.error(f"Streaming error: {e}")

    def _build_fallback_chain(self, model_override: str | None) -> list[str]:
        """Build a deduplicated model fallback chain: [requested] + config chain."""
        chain: list[str] = []
        if model_override:
            chain.append(model_override)
        for m in self.config.model_fallback_chain:
            if m not in chain:
                chain.append(m)
        return chain if chain else ["sonnet"]

    async def _execute_buffered(
        self, prompt: str, session_id: str | None, system_context: str | None,
        max_budget: float, platform: str, user_id: str,
        model_override: str | None = None,
        mcp_config_path: str | None = None,
        settings_path: str | None = None,
        effort: str | None = None,
    ) -> ClaudeResponse:
        """Execute claude CLI with automatic model fallback on rate limit errors."""
        max_retries = self.config.model_max_retries
        if max_retries <= 0:
            # Fallback disabled — single attempt
            response, _ = await self._execute_buffered_once(
                prompt, session_id, system_context, max_budget, platform, user_id,
                model_override, mcp_config_path, settings_path, effort,
            )
            response.model_used = model_override or ""
            return response

        chain = self._build_fallback_chain(model_override)
        last_response: ClaudeResponse | None = None

        for i, model in enumerate(chain[:max_retries + 1]):
            response, stderr_text = await self._execute_buffered_once(
                prompt, session_id, system_context, max_budget, platform, user_id,
                model, mcp_config_path, settings_path, effort,
            )
            response.model_used = model
            last_response = response

            if not _is_rate_limit_error(stderr_text, response):
                return response

            log.warning(
                "Model '%s' hit rate limit (attempt %d/%d), trying next fallback",
                model, i + 1, len(chain),
            )
            if i < len(chain) - 1:
                await asyncio.sleep(self.config.model_retry_delay)

        return last_response or ClaudeResponse.error("All models exhausted")

    async def _execute_buffered_once(
        self, prompt: str, session_id: str | None, system_context: str | None,
        max_budget: float, platform: str, user_id: str,
        model_override: str | None = None,
        mcp_config_path: str | None = None,
        settings_path: str | None = None,
        effort: str | None = None,
    ) -> tuple[ClaudeResponse, str]:
        """Execute a single buffered Claude CLI call. Returns (response, stderr_text)."""
        args, tracking_id = self._build_args(
            prompt, session_id, system_context, max_budget,
            model_override=model_override, mcp_config_path=mcp_config_path,
            settings_path=settings_path, effort=effort,
        )

        log.debug("Executing Claude: session=%s, prompt_len=%d", tracking_id, len(prompt))

        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._subprocess_env(),
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

            stderr_text = stderr.decode().strip() if stderr else ""

            if proc.returncode != 0 and not stdout:
                err_msg = stderr_text or f"Exit code {proc.returncode}"
                log.error("Claude CLI error: %s", err_msg)
                return ClaudeResponse.error(f"Claude Code error: {err_msg}"), stderr_text

            raw = stdout.decode().strip()
            if not raw:
                return ClaudeResponse.error("Empty response from Claude Code"), stderr_text

            data = json.loads(raw)
            response = ClaudeResponse.from_json(data)

            # Mark this session as confirmed — safe to --resume on next message
            if response.session_id:
                self._confirmed_sessions.add(response.session_id)

            log.info(
                "Claude response: session=%s, tokens=%d/%d, cost=$%.4f, turns=%d",
                response.session_id, response.input_tokens, response.output_tokens,
                response.cost, response.num_turns,
            )

            return response, stderr_text

        except asyncio.TimeoutError:
            log.error("Claude CLI timed out after %ds", self.config.process_timeout)
            # Kill the hung process and wait for it to exit (prevent zombies)
            if tracking_id in self._active:
                proc = self._active[tracking_id].process
                try:
                    proc.kill()
                    await proc.wait()  # Reap the process to prevent zombie
                except ProcessLookupError:
                    pass
                self._active.pop(tracking_id, None)
            return ClaudeResponse.error(
                f"Claude Code timed out after {self.config.process_timeout}s"
            ), ""
        except json.JSONDecodeError as e:
            log.error("Failed to parse Claude response: %s", e)
            return ClaudeResponse.error(f"Failed to parse response: {e}"), ""
        except FileNotFoundError:
            log.error("Claude binary not found: %s", self.config.claude_binary)
            return ClaudeResponse.error(
                f"Claude Code not found at '{self.config.claude_binary}'. "
                "Install with: npm install -g @anthropic-ai/claude-code"
            ), ""
        except Exception as e:
            log.exception("Unexpected error running Claude")
            return ClaudeResponse.error(f"Unexpected error: {e}"), ""

    async def drain_all(self, timeout: float = 60) -> None:
        """Wait for all active Claude processes to finish and shutdown SDK bridge."""
        # Shutdown SDK bridge first
        if self._sdk_bridge:
            try:
                await self._sdk_bridge.shutdown()
            except Exception:
                log.debug("SDK bridge shutdown error (non-critical)")
            self._sdk_bridge = None

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
