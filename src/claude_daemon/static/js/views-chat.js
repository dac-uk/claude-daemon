/* ── Chat view — per-agent channels with live streaming ───── */

CC.chat = {
  activeChannel: 'team',
  histories: {},
  unread: new Set(),
  // Concurrent streams keyed by streamId → {abortCtrl, channel, agentMsg}
  activeStreams: new Map(),
  // Set by Operations Reply button — cleared after one send
  resumeSessionId: null,
  resumeTaskId: null,
};

CC.chatNewStreamId = function() {
  return 's' + Date.now().toString(36) + Math.random().toString(36).slice(2, 8);
};

CC.initChat = function() {
  CC.chat.histories = {};
  var channels = ['team'].concat(Object.keys(CC.agents).sort());
  channels.forEach(function(ch) {
    var saved = localStorage.getItem('chat_' + ch);
    CC.chat.histories[ch] = saved ? JSON.parse(saved) : [];
  });

  document.getElementById('chatSend').addEventListener('click', function() {
    CC.chatSendFromInput();
  });
  var input = document.getElementById('chatInput');
  input.addEventListener('keydown', function(e) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      CC.chatSendFromInput();
    }
  });

  // Delegated click handler for inline per-message stop buttons.
  var msgEl = document.getElementById('chatMessages');
  msgEl.addEventListener('click', function(e) {
    var btn = e.target.closest('[data-stop-stream]');
    if (!btn) return;
    CC.chatAbortStream(btn.getAttribute('data-stop-stream'));
  });

  CC.renderChatChannels();
  CC.renderChatMessages();
  CC.renderChatHeader();
};

CC.chatSendFromInput = function() {
  var input = document.getElementById('chatInput');
  var text = input.value.trim();
  if (!text) return;
  input.value = '';
  input.style.height = 'auto';
  CC.chatSend(text);
};

CC.chatAbortStream = function(streamId) {
  var s = CC.chat.activeStreams.get(streamId);
  if (!s) return;
  // Optimistic UI: mark stopping immediately so the user sees feedback
  // before the fetch teardown completes. The fetch-read loop may still
  // be mid-event when we abort; actual AbortError arrives on next tick.
  s.agentMsg._stopping = true;
  if (s.channel === CC.chat.activeChannel) {
    CC.renderChatMessages();
  }
  try { s.abortCtrl.abort(); } catch (_) {}
};

CC.renderChatChannels = function() {
  var el = document.getElementById('chatChannels');
  var channels = ['team'].concat(Object.keys(CC.agents).sort());
  var html = '<div class="chat-channels-title">Channels</div>';

  channels.forEach(function(ch) {
    var isActive = ch === CC.chat.activeChannel;
    var hasUnread = CC.chat.unread.has(ch) && !isActive;
    var agent = CC.agents[ch];
    var name, emoji, role, statusCls;

    if (ch === 'team') {
      name = 'Team'; emoji = '#'; role = 'Auto-route';
      statusCls = '';
    } else if (agent) {
      name = ch.charAt(0).toUpperCase() + ch.slice(1);
      emoji = agent.emoji || CC.AGENT_EMOJI[ch] || '';
      role = agent.role || '';
      statusCls = agent.status === 'busy' ? ' busy' : '';
    } else {
      name = ch; emoji = ''; role = ''; statusCls = '';
    }

    html += '<div class="chat-channel' + (isActive ? ' active' : '') + statusCls + '" data-channel="' + ch + '">'
      + '<span class="ch-emoji">' + emoji + '</span>'
      + '<div class="ch-info"><div class="ch-name">' + name + '</div>'
      + '<div class="ch-role">' + role + '</div></div>'
      + (hasUnread ? '<span class="unread-dot"></span>' : '')
      + '</div>';
  });

  el.innerHTML = html;

  el.querySelectorAll('.chat-channel').forEach(function(btn) {
    btn.addEventListener('click', function() {
      CC.chatSwitchChannel(btn.dataset.channel);
    });
  });
};

CC.chatSwitchChannel = function(ch) {
  CC.chat.activeChannel = ch;
  CC.chat.unread.delete(ch);
  CC.renderChatChannels();
  CC.renderChatMessages();
  CC.renderChatHeader();
  document.getElementById('chatInput').focus();
};

CC.renderChatHeader = function() {
  var el = document.getElementById('chatHeader');
  var ch = CC.chat.activeChannel;
  var agent = CC.agents[ch];

  var headerHtml = '';
  if (ch === 'team') {
    headerHtml = '<span class="chat-h-emoji">#</span> <strong>Team</strong> <span class="chat-h-role">Messages auto-route to the best agent</span>';
  } else if (agent) {
    var name = ch.charAt(0).toUpperCase() + ch.slice(1);
    var statusBadge = agent.status === 'busy'
      ? '<span class="status-badge busy">busy</span>'
      : '<span class="status-badge idle">idle</span>';
    headerHtml = '<span class="chat-h-emoji">' + (agent.emoji || '') + '</span> '
      + '<strong>' + name + '</strong> '
      + '<span class="chat-h-role">' + (agent.role || '') + '</span> '
      + statusBadge;
  }

  if (CC.chat.resumeSessionId && CC.chat.activeChannel === ch) {
    var tid = CC.chat.resumeTaskId || '';
    var tidShort = tid.length > 8 ? tid.slice(0, 8) : tid;
    headerHtml += '<div class="chat-resume-banner">'
      + 'Continuing task <code>' + CC.escHtml(tidShort) + '</code> — next message resumes this session. '
      + '<button class="chat-resume-cancel">Cancel</button>'
      + '</div>';
  }

  el.innerHTML = headerHtml;

  var cancelBtn = el.querySelector('.chat-resume-cancel');
  if (cancelBtn) {
    cancelBtn.addEventListener('click', function() {
      CC.chat.resumeSessionId = null;
      CC.chat.resumeTaskId = null;
      CC.renderChatHeader();
    });
  }
};

CC.renderChatMessages = function() {
  var el = document.getElementById('chatMessages');
  var ch = CC.chat.activeChannel;
  var msgs = CC.chat.histories[ch] || [];

  if (msgs.length === 0) {
    var agentName = ch === 'team' ? 'the team' : ch.charAt(0).toUpperCase() + ch.slice(1);
    el.innerHTML = '<div class="chat-empty">No messages yet. Say hello to ' + agentName + '!</div>';
    return;
  }

  var html = '';
  msgs.forEach(function(m) {
    var cls = m.role === 'user' ? 'user' : 'agent';
    if (m.streaming) cls += ' streaming';
    if (m._stopping) cls += ' stopping';
    var color = m.role !== 'user' && m.agent ? CC.agentColor(m.agent) : '';
    var borderStyle = color ? ' style="border-left: 3px solid ' + color + '"' : '';
    var time = m.timestamp ? new Date(m.timestamp).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : '';
    var streamAttr = m.streamId ? ' data-stream-id="' + m.streamId + '"' : '';

    var meta = '';
    if (m.role === 'user') {
      meta = '<div class="meta">you ' + time + '</div>';
    } else {
      var name = m.agent || 'agent';
      var stopBtn = (m.streaming && m.streamId)
        ? '<button class="stop-stream-btn" data-stop-stream="' + m.streamId + '" title="Stop this response" aria-label="Stop response">'
          + '<svg viewBox="0 0 24 24" width="10" height="10"><rect x="6" y="6" width="12" height="12" rx="2" fill="currentColor"/></svg>'
          + '</button>'
        : '';
      meta = '<div class="meta">' + (CC.AGENT_EMOJI[name] || '') + ' ' + name + ' ' + time + stopBtn + '</div>';
    }

    html += '<div class="chat-message ' + cls + '"' + borderStyle + streamAttr + '>'
      + meta + '<div class="msg-text">' + CC.chatFormatText(m.text || '') + '</div></div>';
  });

  el.innerHTML = html;
  el.scrollTop = el.scrollHeight;
};

CC.chatFormatText = function(text) {
  return text
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>')
    .replace(/\n/g, '<br>');
};

CC.chatSend = async function(prompt) {
  var ch = CC.chat.activeChannel;
  if (!CC.chat.histories[ch]) CC.chat.histories[ch] = [];

  CC.chat.histories[ch].push({
    role: 'user', text: prompt, timestamp: Date.now()
  });
  CC.renderChatMessages();
  CC.chatPersist(ch);

  var body = { message: prompt, user_id: 'dashboard' };
  if (ch !== 'team') body.agent = ch;
  if (CC.chat.resumeSessionId) {
    body.session_id = CC.chat.resumeSessionId;
  }
  CC.chat.resumeSessionId = null;
  CC.chat.resumeTaskId = null;
  CC.renderChatHeader();

  var streamId = CC.chatNewStreamId();
  var agentMsg = {
    role: 'agent', agent: ch === 'team' ? 'johnny' : ch,
    text: '', timestamp: Date.now(), streaming: true, streamId: streamId,
  };
  CC.chat.histories[ch].push(agentMsg);
  CC.renderChatMessages();

  var abortCtrl = new AbortController();
  CC.chat.activeStreams.set(streamId, { abortCtrl: abortCtrl, channel: ch, agentMsg: agentMsg });

  try {
    var resp = await fetch('/api/message/stream', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      signal: abortCtrl.signal,
    });

    var reader = resp.body.getReader();
    var decoder = new TextDecoder();
    var buffer = '';

    while (true) {
      var chunk = await reader.read();
      if (chunk.done) break;
      buffer += decoder.decode(chunk.value, { stream: true });

      var lines = buffer.split('\n');
      buffer = lines.pop();

      for (var i = 0; i < lines.length; i++) {
        var line = lines[i].trim();
        if (!line.startsWith('data: ')) continue;
        try {
          var evt = JSON.parse(line.slice(6));
          if (evt.text) {
            agentMsg.text += evt.text;
            CC.chatUpdateMsg(streamId, ch);
          } else if (evt.done) {
            agentMsg.streaming = false;
            if (evt.error) {
              agentMsg.text = (agentMsg.text ? agentMsg.text + '\n\n' : '') + 'Error: ' + evt.error;
            }
          }
        } catch (_) {}
      }
    }
  } catch (e) {
    if (e.name === 'AbortError') {
      agentMsg.text = (agentMsg.text ? agentMsg.text + '\n\n' : '') + '(stopped)';
    } else {
      agentMsg.text = agentMsg.text || 'Error: ' + e.message;
    }
  } finally {
    agentMsg.streaming = false;
    agentMsg.streamId = null;
    agentMsg._stopping = false;
    if (!agentMsg.text) {
      agentMsg.text = '(No response. Check daemon log: `tail -50 ~/.config/claude-daemon/logs/daemon.log`)';
    }
    CC.chat.activeStreams.delete(streamId);
    if (ch === CC.chat.activeChannel) {
      CC.renderChatMessages();
    }
    CC.chatPersist(ch);
  }
};

CC.chatUpdateMsg = function(streamId, ch) {
  // Only called from our own chatSend — the user initiated this stream,
  // so never mark the source channel as "unread". If they've switched
  // away, the channel's busy indicator already reflects work-in-flight.
  if (ch !== CC.chat.activeChannel) return;
  var el = document.getElementById('chatMessages');
  var msgEl = el.querySelector('[data-stream-id="' + streamId + '"] .msg-text');
  var s = CC.chat.activeStreams.get(streamId);
  if (msgEl && s) {
    msgEl.innerHTML = CC.chatFormatText(s.agentMsg.text);
    el.scrollTop = el.scrollHeight;
  }
};

CC.chatPersist = function(ch) {
  var msgs = (CC.chat.histories[ch] || []).slice(-100);
  msgs = msgs.map(function(m) {
    return { role: m.role, agent: m.agent, text: m.text, timestamp: m.timestamp };
  });
  try {
    localStorage.setItem('chat_' + ch, JSON.stringify(msgs));
  } catch (_) {}
};

CC.chatHandleStreamDelta = function(agentName, text) {
  // Route WebSocket stream deltas (messages arriving from other platforms).
  // Suppress if we already have a local stream for this agent — our own
  // chatSend fetch() is already consuming deltas from the SSE endpoint.
  var ch = agentName;
  var streams = CC.chat.activeStreams;
  var iter = streams.values();
  for (var entry = iter.next(); !entry.done; entry = iter.next()) {
    if (entry.value.channel === ch) return;
  }

  if (!CC.chat.histories[ch]) CC.chat.histories[ch] = [];
  var msgs = CC.chat.histories[ch];
  var last = msgs[msgs.length - 1];

  if (last && last.streaming) {
    last.text += text;
  } else {
    msgs.push({ role: 'agent', agent: agentName, text: text, timestamp: Date.now(), streaming: true });
  }

  if (ch !== CC.chat.activeChannel) {
    CC.chat.unread.add(ch);
    if (CC.currentView === 'chat') CC.renderChatChannels();
  } else if (CC.currentView === 'chat') {
    CC.renderChatMessages();
  }
};

CC.chatHandleAgentIdle = function(agentName) {
  var ch = agentName;
  var msgs = CC.chat.histories[ch];
  if (msgs && msgs.length) {
    var last = msgs[msgs.length - 1];
    if (last.streaming && !last.streamId) {
      // Only clear externally-tracked streams (no streamId means it wasn't
      // started by our own chatSend). Local streams clear themselves.
      last.streaming = false;
      CC.chatPersist(ch);
      if (ch === CC.chat.activeChannel && CC.currentView === 'chat') {
        CC.renderChatMessages();
      }
    }
  }
};

CC.renderChatView = function() {
  if (!CC.chat._initialized) {
    CC.initChat();
    CC.chat._initialized = true;
  } else {
    CC.renderChatChannels();
    CC.renderChatMessages();
    CC.renderChatHeader();
  }
};
