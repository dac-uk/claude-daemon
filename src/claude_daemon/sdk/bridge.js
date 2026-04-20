#!/usr/bin/env node
/**
 * SDK Bridge — manages persistent Claude Agent SDK sessions.
 *
 * Protocol: NDJSON over stdin/stdout.
 *
 * Commands (stdin):
 *   {"cmd":"create","id":"...","agent":"name","model":"sonnet",...}
 *   {"cmd":"send","id":"...","agent":"name","prompt":"...","context":"..."}
 *   {"cmd":"close","id":"...","agent":"name"}
 *   {"cmd":"shutdown","id":"..."}
 *
 * Events (stdout):
 *   {"event":"created","id":"...","agent":"name","sessionId":"..."}
 *   {"event":"text","id":"...","agent":"name","text":"..."}
 *   {"event":"result","id":"...","agent":"name",...metadata...}
 *   {"event":"error","id":"...","agent":"name","message":"...","recoverable":bool}
 *   {"event":"ready"}  (sent once on startup)
 */

const { unstable_v2_createSession, unstable_v2_resumeSession } = require(
  "@anthropic-ai/claude-agent-sdk"
);
const readline = require("readline");

// ── State ──────────────────────────────────────────────────────────────────

/** @type {Map<string, import("@anthropic-ai/claude-agent-sdk").SDKSession>} */
const sessions = new Map();

/** @type {Map<string, {model: string, opts: object}>} */
const sessionMeta = new Map();

// ── Helpers ────────────────────────────────────────────────────────────────

function emit(obj) {
  process.stdout.write(JSON.stringify(obj) + "\n");
}

function log(msg) {
  // Debug logging to stderr (doesn't pollute the NDJSON protocol on stdout)
  process.stderr.write(`[bridge] ${msg}\n`);
}

// ── Command Handlers ───────────────────────────────────────────────────────

async function handleCreate(cmd) {
  const { id, agent, model, systemPrompt, mcpConfig, settingsPath,
          permissionMode, env: extraEnv, resumeSessionId, cwd,
          allowedTools } = cmd;

  try {
    // Merge environment — pass MCP config and system prompt via env vars
    const env = { ...process.env, ...(extraEnv || {}) };

    // Build SDK session options using proper SDK API (NOT CLI flags)
    const opts = {
      model,
      env,
      permissionMode: permissionMode || "auto",
      allowedTools: allowedTools || [
        "Read", "Edit", "Write", "Bash", "Glob", "Grep",
        "NotebookEdit", "WebFetch", "WebSearch", "Agent",
        "Task", "mcp__*",
      ],
    };

    // Set working directory to agent's workspace if provided
    if (cwd) {
      opts.cwd = cwd;
    }

    let session;
    if (resumeSessionId) {
      session = unstable_v2_resumeSession(resumeSessionId, opts);
      log(`Resuming session for ${agent}: ${resumeSessionId}`);
    } else {
      session = unstable_v2_createSession(opts);
      log(`Creating session for ${agent} (model=${model})`);
    }

    // Close existing session for this agent if any
    if (sessions.has(agent)) {
      try {
        sessions.get(agent).close();
      } catch (_) {}
    }

    sessions.set(agent, session);
    sessionMeta.set(agent, { model, opts });

    // Wait for sessionId to be available (first stream event)
    // We don't block — sessionId will be captured on first send
    emit({ event: "created", id, agent, sessionId: null });
  } catch (err) {
    emit({ event: "error", id, agent, message: err.message, recoverable: false });
  }
}

async function handleSend(cmd) {
  const { id, agent, prompt, context } = cmd;

  const session = sessions.get(agent);
  if (!session) {
    emit({ event: "error", id, agent, message: "No session for agent", recoverable: true });
    return;
  }

  try {
    // Build the full message with optional dynamic context
    let fullMessage = prompt;
    if (context) {
      fullMessage =
        `[Additional context for this message — read but do not mention this framing]\n` +
        `${context}\n\n` +
        `[User message]\n${prompt}`;
    }

    // Send the message
    await session.send(fullMessage);

    // Consume the stream
    // resultText = full accumulated text across all assistant messages in this send
    // currentMsgText = text within the in-progress assistant message (for delta diff)
    // currentMsgId = message id of the in-progress assistant message
    let resultText = "";
    let currentMsgText = "";
    let currentMsgId = null;
    let sessionId = null;
    let cost = 0;
    let inputTokens = 0;
    let outputTokens = 0;
    let durationMs = 0;
    let stopReason = null;
    const startTime = Date.now();

    for await (const msg of session.stream()) {
      const type = msg.type;

      if (type === "assistant") {
        // Full or partial assistant message
        if (msg.message?.stop_reason) {
          stopReason = msg.message.stop_reason;
        }
        // Detect a new assistant message (multi-turn: text -> tool_use -> text)
        // so we don't mis-slice the second message against the first's length.
        const msgId = msg.message?.id || null;
        if (msgId && msgId !== currentMsgId) {
          if (currentMsgText) {
            resultText += currentMsgText;
          }
          currentMsgId = msgId;
          currentMsgText = "";
        }
        const content = msg.message?.content;
        if (typeof content === "string" && content.length > currentMsgText.length) {
          const delta = content.slice(currentMsgText.length);
          currentMsgText = content;
          emit({ event: "text", id, agent, text: delta });
        } else if (Array.isArray(content)) {
          // Concatenate all text blocks in this message (tool_use blocks are skipped).
          let msgText = "";
          for (const block of content) {
            if (block.type === "text" && block.text) {
              msgText += block.text;
            }
          }
          if (msgText.length > currentMsgText.length) {
            const delta = msgText.slice(currentMsgText.length);
            currentMsgText = msgText;
            emit({ event: "text", id, agent, text: delta });
          }
        }
      } else if (type === "result") {
        // Final result — extract metadata
        sessionId = msg.session_id || msg.sessionId || null;
        cost = msg.total_cost_usd || msg.cost_usd || msg.cost || 0;
        inputTokens = msg.input_tokens || msg.usage?.input_tokens || 0;
        outputTokens = msg.output_tokens || msg.usage?.output_tokens || 0;
        durationMs = msg.duration_ms || (Date.now() - startTime);
        if (msg.stop_reason || msg.message?.stop_reason) {
          stopReason = msg.stop_reason || msg.message.stop_reason;
        }

        // Flush the in-progress assistant message into the accumulated result.
        if (currentMsgText) {
          resultText += currentMsgText;
          currentMsgText = "";
        }

        // Extract result text if we didn't get it from streaming
        if (!resultText && msg.result) {
          resultText = typeof msg.result === "string"
            ? msg.result
            : JSON.stringify(msg.result);
        }

        // Done — break out of stream
        break;
      } else if (type === "system") {
        // System messages (init, session info)
        if (msg.session_id) {
          sessionId = msg.session_id;
        }
        if (msg.subtype === "init" && msg.session_id) {
          sessionId = msg.session_id;
        }
      }
      // Ignore other message types (tool_use, status, etc.)
    }

    emit({
      event: "result", id, agent, sessionId,
      result: resultText, cost, inputTokens, outputTokens,
      durationMs: durationMs || (Date.now() - startTime),
      stopReason,
    });
  } catch (err) {
    log(`Send error for ${agent}: ${err.message}`);

    // Check if session is dead
    const isSessionDead = err.message?.includes("closed") ||
                          err.message?.includes("terminated") ||
                          err.message?.includes("EPIPE");

    if (isSessionDead) {
      sessions.delete(agent);
      sessionMeta.delete(agent);
    }

    emit({
      event: "error", id, agent,
      message: err.message,
      recoverable: true,
    });
  }
}

async function handleClose(cmd) {
  const { id, agent } = cmd;
  const session = sessions.get(agent);
  if (session) {
    try {
      session.close();
    } catch (_) {}
    sessions.delete(agent);
    sessionMeta.delete(agent);
    log(`Closed session for ${agent}`);
  }
  emit({ event: "closed", id, agent });
}

async function handleShutdown(cmd) {
  const { id } = cmd;
  log("Shutting down all sessions...");
  for (const [agent, session] of sessions) {
    try {
      session.close();
    } catch (_) {}
    log(`Closed session for ${agent}`);
  }
  sessions.clear();
  sessionMeta.clear();
  emit({ event: "shutdown", id });

  // Give stdout time to flush, then exit
  setTimeout(() => process.exit(0), 100);
}

// ── Main Loop ──────────────────────────────────────────────────────────────

const rl = readline.createInterface({ input: process.stdin, terminal: false });

rl.on("line", async (line) => {
  if (!line.trim()) return;

  let cmd;
  try {
    cmd = JSON.parse(line);
  } catch (err) {
    emit({ event: "error", id: null, agent: null, message: `Parse error: ${err.message}`, recoverable: false });
    return;
  }

  try {
    switch (cmd.cmd) {
      case "create":   await handleCreate(cmd); break;
      case "send":     await handleSend(cmd); break;
      case "close":    await handleClose(cmd); break;
      case "shutdown": await handleShutdown(cmd); break;
      default:
        emit({ event: "error", id: cmd.id, agent: cmd.agent, message: `Unknown command: ${cmd.cmd}`, recoverable: false });
    }
  } catch (err) {
    emit({ event: "error", id: cmd.id, agent: cmd.agent, message: `Handler error: ${err.message}`, recoverable: false });
  }
});

rl.on("close", () => {
  log("stdin closed, shutting down");
  for (const session of sessions.values()) {
    try { session.close(); } catch (_) {}
  }
  process.exit(0);
});

// Signal ready
emit({ event: "ready" });
log("SDK bridge ready");
