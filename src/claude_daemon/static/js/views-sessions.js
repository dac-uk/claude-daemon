/* ── Sessions drill-down modal ────────────────────────────────
 * Three levels:
 *   1. Agents list  (counts: chat / spawn / total)
 *   2. Sessions for one agent
 *   3. Session detail — for spawn sessions, shows the linked task
 * Back button walks up one level at a time.
 */

CC.sessState = {
  view: 'agents',        // 'agents' | 'sessions' | 'session'
  selectedAgent: null,
  selectedSession: null,
  summary: null,
  sessions: [],
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
    title.textContent = 'Sessions: ' + s.selectedAgent;
    back.style.display = 'inline-block';
    if (!s.sessions || s.sessions.length === 0) {
      body.innerHTML = '<div class="empty">Loading...</div>';
      return;
    }
    var shtml = '<div class="sess-list">';
    s.sessions.forEach(function(sess, i) {
      var kindClass = 'sess-kind-' + (sess.kind || 'unknown');
      var cost = (sess.cost_usd || 0).toFixed(4);
      var when = CC.formatRelativeTime
        ? CC.formatRelativeTime(sess.last_active) : (sess.last_active || '');
      var label = sess.kind === 'spawn'
        ? (sess.task && sess.task.prompt
            ? sess.task.prompt.substring(0, 80)
            : 'Spawned task ' + (sess.task_id || '').substring(0, 8))
        : 'Chat session ' + (sess.session_id || '').substring(0, 8);
      shtml += '<button class="sess-row" data-idx="' + i + '">' +
        '<span class="sess-kind-badge ' + kindClass + '">' + (sess.kind || '?') + '</span>' +
        '<span class="sess-label">' + CC.escHtml(label) + '</span>' +
        '<span class="sess-msgs">' + (sess.message_count || 0) + ' msg</span>' +
        '<span class="sess-cost">$' + cost + '</span>' +
        '<span class="sess-when">' + CC.escHtml(when) + '</span>' +
      '</button>';
    });
    shtml += '</div>';
    body.innerHTML = shtml;
    body.querySelectorAll('.sess-row').forEach(function(btn) {
      btn.addEventListener('click', function() {
        CC._sessOpenDetail(parseInt(btn.dataset.idx, 10));
      });
    });
    return;
  }

  if (s.view === 'session') {
    title.textContent = 'Session detail';
    back.style.display = 'inline-block';
    var sess = s.selectedSession || {};
    var dhtml = '<dl class="sess-detail">' +
      '<dt>Session ID</dt><dd><code>' + CC.escHtml(sess.session_id || '') + '</code></dd>' +
      '<dt>Kind</dt><dd>' + (sess.kind || 'unknown') + '</dd>' +
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
      dhtml += '<h4 class="sess-task-heading">Linked task</h4>' +
        '<dl class="sess-detail">' +
          '<dt>Task ID</dt><dd><code>' + CC.escHtml(t.id || '') + '</code></dd>' +
          '<dt>Status</dt><dd>' + CC.escHtml(t.status || '') + '</dd>' +
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

CC._sessOpenAgent = async function(name) {
  CC.sessState.view = 'sessions';
  CC.sessState.selectedAgent = name;
  CC.sessState.sessions = [];
  CC._sessRender();  // loading spinner via empty state
  var url = '/api/sessions/history?agent=' + encodeURIComponent(name) + '&limit=200';
  CC.cache[url] = null;
  var data = await CC.api(url);
  CC.sessState.sessions = (data && data.sessions) || [];
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
