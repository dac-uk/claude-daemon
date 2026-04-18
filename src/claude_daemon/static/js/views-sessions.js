/* ── Sessions drill-down modal ────────────────────────────────
 * Three levels:
 *   1. Agents list  (counts: chat / spawn / total)
 *   2. Sessions for one agent — grouped by category
 *   3. Session detail — for spawn sessions, shows the linked task
 * Back button walks up one level at a time.
 */

CC.sessState = {
  view: 'agents',        // 'agents' | 'sessions' | 'session'
  selectedAgent: null,
  selectedSession: null,
  summary: null,
  sessions: [],
  loading: false,
};

CC.openSessionsDrilldown = async function() {
  CC.sessState.view = 'agents';
  CC.sessState.selectedAgent = null;
  CC.sessState.selectedSession = null;
  var modal = document.getElementById('sessModal');
  var overlay = document.getElementById('sessOverlay');
  modal.classList.add('open'); overlay.classList.add('open');
  CC.cache['/api/sessions/summary'] = null;
  CC.sessState.summary = await CC.api('/api/sessions/summary');
  CC._sessRender();
};

CC.openAgentDetail = async function(name) {
  if (!name) return;
  var modal = document.getElementById('sessModal');
  var overlay = document.getElementById('sessOverlay');
  modal.classList.add('open'); overlay.classList.add('open');
  if (!CC.sessState.summary) {
    CC.cache['/api/sessions/summary'] = null;
    CC.sessState.summary = await CC.api('/api/sessions/summary');
  }
  await CC._sessOpenAgent(name);
};

CC.closeSessionsDrilldown = function() {
  document.getElementById('sessModal').classList.remove('open');
  document.getElementById('sessOverlay').classList.remove('open');
};

CC._sessRender = function() {
  var title = document.getElementById('sessTitle');
  var body = document.getElementById('sessBody');
  var back = document.getElementById('sessBack');
  var s = CC.sessState;
  if (!body) return;

  if (s.view === 'agents') {
    title.textContent = 'Sessions by agent';
    back.style.display = 'none';
    var sm = s.summary || { by_agent: {}, unattributed: 0, total: 0 };
    var names = Object.keys(sm.by_agent || {}).sort();
    if (names.length === 0 && !sm.unattributed) {
      body.innerHTML = '<div class="empty">No sessions recorded yet.</div>';
      return;
    }
    var html = '<div class="sess-meta">Total: <strong>' + (sm.total || 0) +
               '</strong> sessions across ' + names.length + ' agents' +
               (sm.unattributed ? ' + ' + sm.unattributed + ' unattributed' : '') +
               '</div><div class="sess-agent-list">';
    names.forEach(function(name) {
      var b = sm.by_agent[name] || { chat: 0, spawn: 0, total: 0 };
      var color = CC.agentColor(name);
      html += '<button class="sess-agent-row" data-agent="' + name + '">' +
        '<span class="sess-agent-name" style="color:' + color + '">' +
          (CC.AGENT_EMOJI[name] || '') + ' ' + name +
        '</span>' +
        '<span class="sess-agent-counts">' +
          '<span title="Chat sessions">' + b.chat + ' chat</span>' +
          '<span title="Spawned sessions">' + b.spawn + ' spawn</span>' +
          '<span class="sess-agent-total">' + b.total + ' total</span>' +
        '</span>' +
      '</button>';
    });
    html += '</div>';
    body.innerHTML = html;
    body.querySelectorAll('.sess-agent-row').forEach(function(btn) {
      btn.addEventListener('click', function() {
        CC._sessOpenAgent(btn.dataset.agent);
      });
    });
    return;
  }

  if (s.view === 'sessions') {
    var ag = CC.agents[s.selectedAgent] || {};
    var emoji = ag.emoji || CC.AGENT_EMOJI[s.selectedAgent] || '';
    var displayName = s.selectedAgent.charAt(0).toUpperCase() + s.selectedAgent.slice(1);
    title.textContent = emoji + ' ' + displayName;
    back.style.display = 'inline-block';

    if (s.loading) {
      body.innerHTML = '<div class="sess-loading"><div class="sess-spinner"></div>Loading sessions\u2026</div>';
      return;
    }

    var sessions = s.sessions || [];
    var chatSessions = sessions.filter(function(x) { return x.kind === 'chat'; });
    var spawnSessions = sessions.filter(function(x) { return x.kind === 'spawn'; });
    var otherSessions = sessions.filter(function(x) { return x.kind !== 'chat' && x.kind !== 'spawn'; });

    var totalCost = sessions.reduce(function(sum, x) { return sum + (x.cost_usd || 0); }, 0);
    var totalMsgs = sessions.reduce(function(sum, x) { return sum + (x.message_count || 0); }, 0);

    var html = '<div class="sess-agent-header">' +
      '<div class="sess-agent-info">' +
        '<span class="sess-agent-role">' + CC.escHtml(ag.role || '') + '</span>' +
        '<span class="sess-agent-model">' + CC.escHtml(ag.model || '') + '</span>' +
      '</div>' +
      '<div class="sess-agent-stats">' +
        '<div class="sess-stat"><span class="sess-stat-val">' + sessions.length + '</span><span class="sess-stat-label">Sessions</span></div>' +
        '<div class="sess-stat"><span class="sess-stat-val">' + totalMsgs + '</span><span class="sess-stat-label">Messages</span></div>' +
        '<div class="sess-stat"><span class="sess-stat-val">$' + totalCost.toFixed(4) + '</span><span class="sess-stat-label">Total Cost</span></div>' +
      '</div>' +
    '</div>';

    if (sessions.length === 0) {
      html += '<div class="sess-empty-state">' +
        '<div class="sess-empty-icon">\u{1f4ad}</div>' +
        '<div class="sess-empty-title">No sessions yet</div>' +
        '<div class="sess-empty-sub">This agent has no recorded chat or task sessions. ' +
          'Start a conversation via the Chat tab or submit a task to generate activity.</div>' +
      '</div>';
      body.innerHTML = html;
      return;
    }

    html += CC._sessRenderCategory('Chat Sessions', chatSessions, 'chat');
    html += CC._sessRenderCategory('Spawned Tasks', spawnSessions, 'spawn');
    if (otherSessions.length > 0) {
      html += CC._sessRenderCategory('Other Sessions', otherSessions, 'unknown');
    }

    body.innerHTML = html;
    body.querySelectorAll('.sess-row').forEach(function(btn) {
      btn.addEventListener('click', function() {
        CC._sessOpenDetail(parseInt(btn.dataset.idx, 10));
      });
    });
    return;
  }

  if (s.view === 'session') {
    title.textContent = 'Session Detail';
    back.style.display = 'inline-block';
    var sess = s.selectedSession || {};
    var kindLabel = sess.kind === 'spawn' ? 'Spawned Task' : sess.kind === 'chat' ? 'Chat Session' : 'Session';
    var statusClass = sess.status === 'active' ? 'sess-status-active' : '';

    var dhtml = '<div class="sess-detail-header">' +
      '<span class="sess-kind-badge sess-kind-' + (sess.kind || 'unknown') + '">' + kindLabel + '</span>' +
      (sess.status ? '<span class="sess-status-pill ' + statusClass + '">' + sess.status + '</span>' : '') +
    '</div>' +
    '<dl class="sess-detail">' +
      '<dt>Session ID</dt><dd><code>' + CC.escHtml(sess.session_id || '') + '</code></dd>' +
      '<dt>Agent</dt><dd>' + CC.escHtml(sess.agent || '\u2014') + '</dd>' +
      '<dt>User</dt><dd>' + CC.escHtml(sess.user_id || '') + '</dd>' +
      '<dt>Platform</dt><dd>' + CC.escHtml(sess.platform || '') + '</dd>' +
      '<dt>Started</dt><dd>' + CC.escHtml(sess.started_at || '') + '</dd>' +
      '<dt>Last active</dt><dd>' + CC.escHtml(sess.last_active || '') + '</dd>' +
      '<dt>Messages</dt><dd>' + (sess.message_count || 0) + '</dd>' +
      '<dt>Cost</dt><dd>$' + (sess.cost_usd || 0).toFixed(4) + '</dd>' +
    '</dl>';
    if (sess.task) {
      var t = sess.task;
      var taskStatusClass = 'sess-task-status-' + (t.status || 'unknown');
      dhtml += '<h4 class="sess-task-heading">Linked Task</h4>' +
        '<dl class="sess-detail">' +
          '<dt>Task ID</dt><dd><code>' + CC.escHtml(t.id || '') + '</code></dd>' +
          '<dt>Status</dt><dd><span class="sess-task-pill ' + taskStatusClass + '">' + CC.escHtml(t.status || '') + '</span></dd>' +
          '<dt>Type</dt><dd>' + CC.escHtml(t.task_type || '') + '</dd>' +
          '<dt>Created</dt><dd>' + CC.escHtml(t.created_at || '') + '</dd>' +
          '<dt>Cost</dt><dd>$' + (t.cost_usd || 0).toFixed(4) + '</dd>' +
        '</dl>' +
        '<div class="sess-task-prompt"><strong>Prompt</strong><br>' +
          CC.escHtml(t.prompt || '').replace(/\n/g, '<br>') + '</div>' +
        (t.result ? '<div class="sess-task-result"><strong>Result</strong><br>' +
          CC.escHtml(t.result).replace(/\n/g, '<br>') + '</div>' : '') +
        (t.error ? '<div class="sess-task-error"><strong>Error</strong><br>' +
          CC.escHtml(t.error) + '</div>' : '');
    }
    body.innerHTML = dhtml;
    return;
  }
};

CC._sessRenderCategory = function(label, sessions, kind) {
  var html = '<div class="sess-category">' +
    '<div class="sess-category-header">' +
      '<span class="sess-category-label">' + label + '</span>' +
      '<span class="sess-category-count">' + sessions.length + '</span>' +
    '</div>';

  if (sessions.length === 0) {
    html += '<div class="sess-category-empty">No ' + label.toLowerCase() + '</div>';
    html += '</div>';
    return html;
  }

  html += '<div class="sess-list">';
  var allSessions = CC.sessState.sessions || [];
  sessions.forEach(function(sess) {
    var globalIdx = allSessions.indexOf(sess);
    var cost = (sess.cost_usd || 0).toFixed(4);
    var when = CC.formatRelativeTime
      ? CC.formatRelativeTime(sess.last_active) : (sess.last_active || '');
    var label;
    if (kind === 'spawn') {
      label = (sess.task && sess.task.prompt)
        ? sess.task.prompt.substring(0, 80)
        : 'Spawned task ' + (sess.task_id || '').substring(0, 8);
    } else {
      label = 'Session ' + (sess.session_id || '').substring(0, 8);
    }
    var statusDot = sess.status === 'active' ? '<span class="sess-active-dot" title="Active"></span>' : '';
    html += '<button class="sess-row" data-idx="' + globalIdx + '">' +
      statusDot +
      '<span class="sess-kind-badge sess-kind-' + (sess.kind || 'unknown') + '">' + (sess.kind || '?') + '</span>' +
      '<span class="sess-label">' + CC.escHtml(label) + '</span>' +
      '<span class="sess-msgs">' + (sess.message_count || 0) + ' msg</span>' +
      '<span class="sess-cost">$' + cost + '</span>' +
      '<span class="sess-when">' + CC.escHtml(when) + '</span>' +
    '</button>';
  });
  html += '</div></div>';
  return html;
};

CC._sessOpenAgent = async function(name) {
  CC.sessState.view = 'sessions';
  CC.sessState.selectedAgent = name;
  CC.sessState.sessions = [];
  CC.sessState.loading = true;
  CC._sessRender();
  var url = '/api/sessions/history?agent=' + encodeURIComponent(name) + '&limit=200';
  CC.cache[url] = null;
  var data = await CC.api(url);
  CC.sessState.sessions = (data && data.sessions) || [];
  CC.sessState.loading = false;
  CC._sessRender();
};

CC._sessOpenDetail = function(idx) {
  var sess = CC.sessState.sessions[idx];
  if (!sess) return;
  CC.sessState.view = 'session';
  CC.sessState.selectedSession = sess;
  CC._sessRender();
};

CC._sessBack = function() {
  if (CC.sessState.view === 'session') {
    CC.sessState.view = 'sessions';
    CC.sessState.selectedSession = null;
  } else if (CC.sessState.view === 'sessions') {
    CC.sessState.view = 'agents';
    CC.sessState.selectedAgent = null;
    CC.sessState.sessions = [];
  }
  CC._sessRender();
};

CC._sessBind = function() {
  var close = document.getElementById('sessClose');
  var back = document.getElementById('sessBack');
  var overlay = document.getElementById('sessOverlay');
  var wrap = document.getElementById('statSessionsWrap');
  if (close) close.addEventListener('click', CC.closeSessionsDrilldown);
  if (back) back.addEventListener('click', CC._sessBack);
  if (overlay) overlay.addEventListener('click', CC.closeSessionsDrilldown);
  if (wrap) {
    wrap.style.cursor = 'pointer';
    wrap.addEventListener('click', CC.openSessionsDrilldown);
  }
};

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', CC._sessBind);
} else {
  CC._sessBind();
}
