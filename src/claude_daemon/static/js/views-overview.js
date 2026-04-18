/* ── Overview view: metrics, agent sidebar, feed ─────────── */

CC.renderOverviewMetrics = async function() {
  var el = document.getElementById('overviewMetrics');
  var status = await CC.api('/api/status');
  var discData = await CC.api('/api/discussions');
  var taskData = await CC.api('/api/tasks');
  var s = status || {};
  var dStats = (discData && discData.stats) || {};
  var running = taskData ? taskData.tasks.filter(function(t) { return t.status === 'running'; }).length : 0;

  var cards = [
    { label: 'Agents', value: s.agents || 0, sub: Object.values(CC.agents).filter(function(a){ return a.status==='busy'; }).length + ' active' },
    { label: 'Tasks', value: running, sub: (taskData ? taskData.tasks.length : 0) + ' total' },
    { label: 'Cost Today', value: '$' + (s.total_cost || 0).toFixed(2), sub: s.total_messages + ' messages' },
    { label: 'Sessions', value: s.total_sessions || 0, sub: s.active_sessions + ' active' },
    { label: 'Discussions', value: dStats.total || 0, sub: (dStats.converged || 0) + ' converged' }
  ];

  el.innerHTML = cards.map(function(c) {
    return '<div class="metric-card glass-sm"><div class="label">' + c.label + '</div><div class="value">' + c.value + '</div><div class="sub">' + (c.sub || '') + '</div></div>';
  }).join('');
};

CC.renderAgentSidebar = function() {
  var el = document.getElementById('agentSidebar');
  if (!el) return;
  var agents = Object.values(CC.agents);
  if (agents.length === 0) { el.innerHTML = '<div class="empty"><div class="icon">...</div>Loading agents</div>'; return; }

  // Sort: orchestrator first, then busy first, then alpha
  agents.sort(function(a, b) {
    if (a.is_orchestrator !== b.is_orchestrator) return a.is_orchestrator ? -1 : 1;
    if (a.status !== b.status) return a.status === 'busy' ? -1 : 1;
    return a.name.localeCompare(b.name);
  });

  el.innerHTML = '<div class="feed-title">Agents</div>' + agents.map(function(a) {
    var dotClass = a.status === 'busy' ? 'busy' : '';
    var bg = a.color + '18';
    return '<div class="agent-mini" data-agent="' + a.name + '">' +
      '<div class="avatar" style="background:' + bg + '">' + (a.emoji || CC.AGENT_EMOJI[a.name] || '') + '</div>' +
      '<div class="info"><div class="name">' + a.name.charAt(0).toUpperCase() + a.name.slice(1) + '</div>' +
      '<div class="role">' + (a.status === 'busy' ? (a.currentPrompt || 'Working...').substring(0, 40) : a.role) + '</div></div>' +
      '<div class="status-dot ' + dotClass + '"></div></div>';
  }).join('');
  el.querySelectorAll('.agent-mini').forEach(function(row) {
    row.addEventListener('click', function() {
      if (CC.openAgentDetail) CC.openAgentDetail(row.dataset.agent);
    });
  });
};

/* ── Feed / Event log ─────────────────────────────────────── */
CC.addFeed = function(agent, type, msg) {
  var now = new Date();
  var time = now.toTimeString().substring(0, 8);
  CC.events.unshift({ agent: agent, type: type, msg: msg, time: time });
  if (CC.events.length > CC.MAX_EVENTS) CC.events.length = CC.MAX_EVENTS;
  if (CC.currentView === 'overview') CC.renderFeed();
};

CC.renderFeed = function() {
  var el = document.getElementById('feedList');
  if (!el) return;
  el.innerHTML = CC.events.slice(0, 50).map(function(e) {
    var badgeClass = 'badge-' + e.type;
    var color = CC.agentColor(e.agent);
    return '<div class="feed-item"><span class="time">' + e.time + '</span>' +
      '<span class="badge ' + badgeClass + '">' + (e.agent || 'sys') + '</span>' +
      '<span class="msg">' + CC.escHtml(e.msg) + '</span></div>';
  }).join('');
};

/* ── Stream panel ─────────────────────────────────────────── */
CC.openStream = function(agentName) {
  CC.selectedAgent = agentName;
  var ag = CC.agents[agentName];
  var panel = document.getElementById('streamPanel');
  var overlay = document.getElementById('streamOverlay');
  document.getElementById('streamTitle').textContent = (ag ? ag.emoji + ' ' : '') + agentName.charAt(0).toUpperCase() + agentName.slice(1) + ' — Live Output';
  panel.classList.add('open');
  overlay.classList.add('visible');
  CC.renderStream();
};

CC.closeStream = function() {
  CC.selectedAgent = null;
  document.getElementById('streamPanel').classList.remove('open');
  document.getElementById('streamOverlay').classList.remove('visible');
};

CC.renderStream = function() {
  var body = document.getElementById('streamBody');
  if (!CC.selectedAgent || !body) return;
  var ag = CC.agents[CC.selectedAgent];
  if (!ag) return;
  if (ag.streams.length === 0) {
    body.textContent = ag.status === 'busy' ? 'Waiting for output...' : 'Agent is idle. Output appears here when agent is working.';
    return;
  }
  body.textContent = ag.streams.join('');
  body.scrollTop = body.scrollHeight;
};

CC.escHtml = function(s) {
  if (!s) return '';
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
};
