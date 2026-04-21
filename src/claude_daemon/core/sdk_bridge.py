"""SDKBridgeManager — manages persistent Claude sessions via the Agent SDK.

Wraps a Node.js bridge process (sdk/bridge.js) that keeps one SDKSession
per agent alive. Communicates via NDJSON over stdin/stdout.

Falls back gracefully — if the bridge fails for any reason, ProcessManager
uses the existing one-shot subprocess mode.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, AsyncIterator

if TYPE_CHECKING:
    from claude_daemon.core.config import DaemonConfig

from claude_daemon.core.process import ClaudeResponse

log = logging.getLogger(__name__)

# Path to bridge.js relative to this file
_BRIDGE_SCRIPT = Path(__file__).resolve().parent.parent / "sdk" / "bridge.js"


class SDKBridgeManager:
    """Manages persistent Claude sessions via the Agent SDK bridge process."""

    def __init__(self, config: DaemonConfig) -> None:
        self.config = config
        self._process: asyncio.subprocess.Process | None = None
        self._sessions: dict[str, str] = {}  # agent_name -> session_id
        self._pending: dict[str, asyncio.Future] = {}  # request_id -> Future[dict]
        self._streams: dict[str, asyncio.Queue] = {}  # request_id -> Queue for streaming
        self._system_prompts: dict[str, str] = {}  # agent_name -> static system prompt
        self._first_message: dict[str, bool] = {}  # agent_name -> True if first message sent
        self._reader_task: asyncio.Task | None = None
        self._started = False

    @property
    def is_alive(self) -> bool:
        return self._process is not None and self._process.returncode is None

    @staticmethod
    def _key(agent_name: str, model: str | None = None) -> str:
        """Compose internal session key. Format: agent:model (e.g. 'johnny:sonnet')."""
        return f"{agent_name}:{model or 'default'}"

    def has_session(self, agent_name: str, model: str | None = None) -> bool:
        """Check if a warm session exists for this (agent, model) pair."""
        return self._key(agent_name, model) in self._sessions and self.is_alive

    def warm_session_models(self, agent_name: str) -> list[str]:
        """Return the list of models with warm sessions for this agent."""
        if not self.is_alive:
            return []
        prefix = f"{agent_name}:"
        return sorted(
            key[len(prefix):] for key in self._sessions if key.startswith(prefix)
        )

    def warm_session_count(self) -> int:
        """Total number of warm sessions across all agents."""
        return len(self._sessions) if self.is_alive else 0

    async def start(self) -> None:
        """Spawn the Node.js bridge process."""
        if self._started and self.is_alive:
            return

        bridge_script = str(_BRIDGE_SCRIPT)
        if not os.path.exists(bridge_script):
            raise FileNotFoundError(f"SDK bridge script not found: {bridge_script}")

        # Resolve node executable
        node_path = getattr(self.config, "sdk_bridge_node_path", "node")

        # Check if the SDK package is installed alongside bridge.js
        sdk_dir = _BRIDGE_SCRIPT.parent
        node_modules = sdk_dir / "node_modules" / "@anthropic-ai" / "claude-agent-sdk"
        if not node_modules.exists():
            raise FileNotFoundError(
                f"@anthropic-ai/claude-agent-sdk not installed at {sdk_dir}. "
                "Run: cd {sdk_dir} && npm install @anthropic-ai/claude-agent-sdk"
            )

        env = os.environ.copy()
        # Pass OAuth token if available
        oauth_token = (
            getattr(self.config, "claude_oauth_token", "")
            or os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "")
        )
        if oauth_token:
            env["CLAUDE_CODE_OAUTH_TOKEN"] = oauth_token

        try:
            self._process = await asyncio.create_subprocess_exec(
                node_path, bridge_script,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                cwd=str(sdk_dir),
            )
        except FileNotFoundError:
            raise FileNotFoundError(
                f"Node.js not found at '{node_path}'. "
                "Install Node.js or set sdk_bridge_node_path in config."
            )

        # Start background reader
        self._reader_task = asyncio.create_task(self._read_stdout())
        self._started = True

        # Start stderr logger
        asyncio.create_task(self._read_stderr())

        # Wait for "ready" event
        ready_future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending["__ready__"] = ready_future
        try:
            await asyncio.wait_for(ready_future, timeout=10.0)
            log.info("SDK bridge started (pid=%d)", self._process.pid)
        except asyncio.TimeoutError:
            log.error("SDK bridge failed to start (no ready event in 10s)")
            await self.shutdown()
            raise RuntimeError("SDK bridge startup timeout")

    async def _read_stdout(self) -> None:
        """Background task: read NDJSON events from bridge stdout."""
        assert self._process and self._process.stdout
        try:
            async for line in self._process.stdout:
                line = line.decode().strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    log.warning("Bridge: invalid JSON: %s", line[:200])
                    continue

                self._dispatch_event(event)
        except Exception:
            log.exception("Bridge stdout reader error")
        finally:
            log.info("Bridge stdout reader exited")
            # Resolve all pending futures with errors
            for req_id, future in list(self._pending.items()):
                if not future.done():
                    future.set_result({
                        "event": "error", "message": "Bridge process died", "recoverable": True,
                    })
            # Signal end to all active streams
            for req_id, queue in list(self._streams.items()):
                await queue.put(None)

    async def _read_stderr(self) -> None:
        """Background task: log bridge stderr."""
        assert self._process and self._process.stderr
        try:
            async for line in self._process.stderr:
                msg = line.decode().strip()
                if msg:
                    log.debug("Bridge: %s", msg)
        except Exception:
            pass

    def _dispatch_event(self, event: dict) -> None:
        """Route an event to the correct pending future or stream queue."""
        req_id = event.get("id")
        event_type = event.get("event", "")

        # Handle the startup ready event
        if event_type == "ready":
            future = self._pending.pop("__ready__", None)
            if future and not future.done():
                future.set_result(event)
            return

        if not req_id:
            return

        # Streaming events go to the queue
        if req_id in self._streams:
            queue = self._streams[req_id]
            if event_type in ("text", "result", "error"):
                asyncio.get_event_loop().call_soon_threadsafe(queue.put_nowait, event)
            if event_type in ("result", "error"):
                # Signal end of stream
                asyncio.get_event_loop().call_soon_threadsafe(queue.put_nowait, None)
            return

        # Buffered events resolve the future
        if req_id in self._pending:
            future = self._pending.get(req_id)
            if event_type in ("result", "error", "created", "closed", "shutdown"):
                self._pending.pop(req_id, None)
                if future and not future.done():
                    future.set_result(event)
            elif event_type == "text":
                # For buffered mode, accumulate text in a list on the future
                if not hasattr(future, "_accumulated"):
                    future._accumulated = []
                future._accumulated.append(event.get("text", ""))

    async def _send_command(self, cmd: dict) -> None:
        """Write a command to the bridge's stdin."""
        if not self.is_alive:
            raise RuntimeError("SDK bridge is not running")
        assert self._process and self._process.stdin
        data = json.dumps(cmd) + "\n"
        self._process.stdin.write(data.encode())
        await self._process.stdin.drain()

    def _new_id(self) -> str:
        return str(uuid.uuid4())[:12]

    # ── Public API ──────────────────────────────────────────────────────

    async def create_session(
        self,
        agent_name: str,
        model: str,
        system_prompt: str = "",
        mcp_config_path: str | None = None,
        settings_path: str | None = None,
        resume_session_id: str | None = None,
        agent_workspace: str | None = None,
    ) -> str | None:
        """Create a persistent session for an (agent, model) pair. Returns session_id or None."""
        req_id = self._new_id()
        session_key = self._key(agent_name, model)
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[req_id] = future

        # Store system prompt to inject in first message
        if system_prompt:
            self._system_prompts[session_key] = system_prompt

        await self._send_command({
            "cmd": "create",
            "id": req_id,
            "agent": session_key,  # Bridge uses this as opaque session identifier
            "model": model,
            "permissionMode": self.config.permission_mode,
            "cwd": agent_workspace,
            "resumeSessionId": resume_session_id,
        })

        timeout_s = getattr(self.config, "sdk_create_session_timeout_ms", 20_000) / 1000
        try:
            result = await asyncio.wait_for(future, timeout=timeout_s)
        except asyncio.TimeoutError:
            self._pending.pop(req_id, None)
            log.warning("SDK create_session timeout for %s (%.0fs)", session_key, timeout_s)
            return None

        # Graceful fallback: if a resume attempt errored, retry once without the session_id.
        if result.get("event") == "error" and resume_session_id:
            log.warning(
                "SDK resume failed for %s (%s) — creating fresh session",
                session_key, result.get("message"),
            )
            retry_id = self._new_id()
            retry_future: asyncio.Future = asyncio.get_event_loop().create_future()
            self._pending[retry_id] = retry_future
            await self._send_command({
                "cmd": "create",
                "id": retry_id,
                "agent": session_key,
                "model": model,
                "permissionMode": self.config.permission_mode,
                "cwd": agent_workspace,
                "resumeSessionId": None,
            })
            try:
                result = await asyncio.wait_for(retry_future, timeout=timeout_s)
            except asyncio.TimeoutError:
                self._pending.pop(retry_id, None)
                log.warning("SDK create_session retry timeout for %s", session_key)
                return None

        if result.get("event") == "error":
            log.error("SDK create_session error for %s: %s", session_key, result.get("message"))
            return None

        session_id = result.get("sessionId")
        self._sessions[session_key] = session_id or ""
        if resume_session_id:
            log.info(
                "SDK session created for %s (resume=%s, validated on first message)",
                session_key, resume_session_id[:12],
            )
        else:
            log.info("SDK session created for %s (fresh)", session_key)
        return session_id

    async def send_message(
        self,
        agent_name: str,
        prompt: str,
        context: str | None = None,
        model: str | None = None,
    ) -> ClaudeResponse:
        """Send a prompt and return the full response (buffered)."""
        req_id = self._new_id()
        session_key = self._key(agent_name, model)
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[req_id] = future

        # Inject static system prompt on first message to this session
        effective_context = context
        if session_key not in self._first_message:
            self._first_message[session_key] = True
            sys_prompt = self._system_prompts.get(session_key, "")
            if sys_prompt:
                effective_context = sys_prompt + ("\n\n" + context if context else "")

        await self._send_command({
            "cmd": "send",
            "id": req_id,
            "agent": session_key,
            "prompt": prompt,
            "context": effective_context or None,
        })

        try:
            result = await asyncio.wait_for(future, timeout=self.config.process_timeout)
        except asyncio.TimeoutError:
            self._pending.pop(req_id, None)
            return ClaudeResponse.error(f"SDK bridge timeout ({self.config.process_timeout}s)")

        if result.get("event") == "error":
            msg = result.get("message", "Unknown error")
            if result.get("sessionDead"):
                self._sessions.pop(session_key, None)
                self._first_message.pop(session_key, None)
            return ClaudeResponse.error(f"SDK bridge error: {msg}")

        # Update session_id if we got one
        session_id = result.get("sessionId", "")
        if session_id:
            self._sessions[session_key] = session_id

        return ClaudeResponse(
            result=result.get("result", ""),
            session_id=session_id,
            cost=result.get("cost", 0),
            input_tokens=result.get("inputTokens", 0),
            output_tokens=result.get("outputTokens", 0),
            num_turns=1,
            duration_ms=result.get("durationMs", 0),
            is_error=False,
            stop_reason=result.get("stopReason") or "",
        )

    async def stream_message(
        self,
        agent_name: str,
        prompt: str,
        context: str | None = None,
        model: str | None = None,
    ) -> AsyncIterator[str | ClaudeResponse]:
        """Send a prompt and stream text deltas, then final ClaudeResponse."""
        req_id = self._new_id()
        session_key = self._key(agent_name, model)
        queue: asyncio.Queue = asyncio.Queue()
        self._streams[req_id] = queue

        # Inject static system prompt on first message to this session
        effective_context = context
        if session_key not in self._first_message:
            self._first_message[session_key] = True
            sys_prompt = self._system_prompts.get(session_key, "")
            if sys_prompt:
                effective_context = sys_prompt + ("\n\n" + context if context else "")

        await self._send_command({
            "cmd": "send",
            "id": req_id,
            "agent": session_key,
            "prompt": prompt,
            "context": effective_context or None,
        })

        idle_timeout = self.config.sdk_bridge_idle_timeout_ms / 1000.0
        try:
            while True:
                try:
                    # Per-event idle timeout: any event (text delta, tool, result) resets the
                    # clock, so long Opus replies complete as long as *something* is streaming.
                    # A fully silent bridge fails over to CLI after idle_timeout seconds.
                    event = await asyncio.wait_for(queue.get(), timeout=idle_timeout)
                except asyncio.TimeoutError:
                    yield ClaudeResponse.error(
                        f"SDK bridge idle timeout ({idle_timeout:.0f}s without any event)"
                    )
                    return

                if event is None:
                    # Bridge process died or stream ended without a result — surface it.
                    self._sessions.pop(session_key, None)
                    self._first_message.pop(session_key, None)
                    yield ClaudeResponse.error(
                        "SDK bridge: stream ended without result (bridge process may have crashed)"
                    )
                    return

                event_type = event.get("event", "")

                if event_type == "text":
                    yield event.get("text", "")

                elif event_type == "result":
                    session_id = event.get("sessionId", "")
                    if session_id:
                        self._sessions[session_key] = session_id
                    yield ClaudeResponse(
                        result=event.get("result", ""),
                        session_id=session_id,
                        cost=event.get("cost", 0),
                        input_tokens=event.get("inputTokens", 0),
                        output_tokens=event.get("outputTokens", 0),
                        num_turns=1,
                        duration_ms=event.get("durationMs", 0),
                        is_error=False,
                        stop_reason=event.get("stopReason") or "",
                    )
                    return

                elif event_type == "error":
                    msg = event.get("message", "Unknown error")
                    if event.get("sessionDead"):
                        self._sessions.pop(session_key, None)
                        self._first_message.pop(session_key, None)
                    yield ClaudeResponse.error(f"SDK bridge error: {msg}")
                    return
        finally:
            self._streams.pop(req_id, None)

    async def close_session(self, agent_name: str, model: str | None = None) -> None:
        """Close a specific (agent, model) session."""
        req_id = self._new_id()
        session_key = self._key(agent_name, model)
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[req_id] = future

        await self._send_command({
            "cmd": "close",
            "id": req_id,
            "agent": session_key,
        })

        try:
            await asyncio.wait_for(future, timeout=5.0)
        except asyncio.TimeoutError:
            pass

        self._sessions.pop(session_key, None)
        self._first_message.pop(session_key, None)

    async def shutdown(self) -> None:
        """Shutdown all sessions and the bridge process."""
        if not self.is_alive:
            return

        try:
            req_id = self._new_id()
            future: asyncio.Future = asyncio.get_event_loop().create_future()
            self._pending[req_id] = future

            await self._send_command({"cmd": "shutdown", "id": req_id})

            try:
                await asyncio.wait_for(future, timeout=10.0)
            except asyncio.TimeoutError:
                log.warning("SDK bridge shutdown timeout, killing process")
        except Exception:
            pass

        # Force kill if still alive
        if self.is_alive:
            try:
                self._process.kill()
                await self._process.wait()
            except ProcessLookupError:
                pass

        self._sessions.clear()
        self._pending.clear()
        self._streams.clear()
        self._first_message.clear()

        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()

        self._process = None
        self._started = False
        log.info("SDK bridge shut down")
