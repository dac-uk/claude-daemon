/* ── Global state ─────────────────────────────────────────── */
window.CC = window.CC || {};

CC.agents = {};       // name -> {name, role, emoji, model, status, color, streams:[], is_orchestrator}
CC.tasks = {};        // task_id -> {agent, status, prompt, cost}
CC.events = [];       // feed items (max 100)
CC.selectedAgent = null;
CC.totalCost = 0;
CC.ws = null;
CC.wsConnected = false;
CC.currentView = 'overview';
CC.cache = {};        // api cache: url -> {data, ts}

CC.AGENT_COLORS = {
  johnny: '#f0883e', albert: '#58a6ff', luna: '#bc8cff', max: '#3fb950',
  penny: '#d29922', jeremy: '#f85149', sophie: '#a5d6ff'
};

CC.AGENT_EMOJI = {
  johnny: '\u{1f3af}', albert: '\u{1f4bb}', luna: '\u{1f3a8}', max: '\u{1f50d}',
  penny: '\u{1f4b0}', jeremy: '\u{1f6e1}', sophie: '\u2696\ufe0f'
};

CC.MAX_EVENTS = 100;

CC.agentColor = function(name) {
  return CC.AGENT_COLORS[name] || '#8b949e';
};

CC.formatMcpHealth = function(h) {
  if (!h || typeof h !== 'object') return h || 'n/a';
  var keys = Object.keys(h);
  if (keys.length === 0) return 'none';
  return keys.map(function(k) { return k + ': ' + h[k]; }).join(', ');
};
