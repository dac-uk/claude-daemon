/* ── API helpers with caching ─────────────────────────────── */

CC.CACHE_TTL = 10000; // 10s

CC.api = async function(path, opts) {
  const url = path;
  const cacheKey = url + JSON.stringify(opts || {});
  const cached = CC.cache[cacheKey];
  if (cached && Date.now() - cached.ts < CC.CACHE_TTL) return cached.data;
  try {
    const res = await fetch(url, opts);
    if (!res.ok) {
      // Preserve status + body for the caller so UI can surface a real
      // diagnostic instead of a generic "Could not load X".
      var body = null;
      try { body = await res.json(); } catch (_) {}
      CC.lastApiError = { path: path, status: res.status, body: body };
      console.error('API error:', path, res.status, body);
      return null;
    }
    const data = await res.json();
    CC.cache[cacheKey] = { data, ts: Date.now() };
    return data;
  } catch (e) {
    CC.lastApiError = { path: path, status: 0, body: { error: String(e) } };
    console.error('API error:', path, e);
    return null;
  }
};

CC.fetchAgents = async function() {
  const data = await CC.api('/api/agents');
  if (!data) return;
  data.agents.forEach(function(a) {
    if (!CC.agents[a.name]) {
      CC.agents[a.name] = {
        name: a.name, role: a.role, emoji: a.emoji || CC.AGENT_EMOJI[a.name] || '',
        model: a.model, status: 'idle', color: CC.agentColor(a.name),
        streams: [], is_orchestrator: a.is_orchestrator,
        has_mcp: a.has_mcp, mcp_health: a.mcp_health,
        heartbeat_tasks: a.heartbeat_tasks, cost: a.cost || 0
      };
    } else {
      var ag = CC.agents[a.name];
      ag.role = a.role; ag.model = a.model; ag.is_orchestrator = a.is_orchestrator;
      ag.has_mcp = a.has_mcp; ag.mcp_health = a.mcp_health;
      ag.heartbeat_tasks = a.heartbeat_tasks;
      if (!ag._wsUpdated) ag.cost = a.cost || 0;
    }
  });
};

CC.fetchStatus = async function() {
  var data = await CC.api('/api/status');
  if (!data) return;
  CC.totalCost = data.total_cost || 0;
  document.getElementById('statAgents').textContent = data.agents || 0;
  // "Sessions" now shows the total historical session count (chatted +
  // spawned). Clicking it opens a drill-down modal with per-agent breakdown.
  var total = (typeof data.total_sessions === 'number')
    ? data.total_sessions : (data.active_sessions || 0);
  document.getElementById('statSessions').textContent = total;
  document.getElementById('statCost').textContent = '$' + CC.totalCost.toFixed(2);
  if (typeof CC.setAlertsBadge === 'function') {
    CC.setAlertsBadge(data.alert_count || 0);
  }
  return data;
};

CC.fetchCosts = async function() {
  CC.cache['/api/costs' + JSON.stringify({})] = null;
  var data = await CC.api('/api/costs');
  if (!data) return;
  CC.totalCost = data.total_usd || 0;
  document.getElementById('statCost').textContent = '$' + CC.totalCost.toFixed(2);
  if (data.by_agent) {
    Object.keys(data.by_agent).forEach(function(name) {
      var ag = CC.agents[name];
      if (ag) {
        ag.cost = data.by_agent[name];
        ag._wsUpdated = false;
      }
    });
  }
  return data;
};

CC.updateActiveCount = function() {
  var n = Object.values(CC.agents).filter(function(a) { return a.status === 'busy'; }).length;
  document.getElementById('statActive').textContent = n;
};
