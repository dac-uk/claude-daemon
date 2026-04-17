/* ── Analytics, Activity, Settings views ──────────────────── */

/* ═══ ANALYTICS ═══ */
CC.chartInstances = {};

CC.renderAnalyticsView = async function() {
  var metricsData = await CC.api('/api/metrics?days=7');
  var failData = await CC.api('/api/failures');
  var discData = await CC.api('/api/discussions');
  var taskData = await CC.api('/api/tasks');

  // Stats cards
  var statsEl = document.getElementById('analyticsStats');
  var dStats = (discData && discData.stats) || {};
  var failCount = failData && failData.failures ? failData.failures.length : 0;
  var taskCount = taskData && taskData.tasks ? taskData.tasks.length : 0;
  var successCount = taskData ? taskData.tasks.filter(function(t) { return t.status === 'completed'; }).length : 0;

  statsEl.innerHTML = [
    { l: 'Total Cost (7d)', v: '$' + (metricsData && metricsData.metrics ? metricsData.metrics.reduce(function(s,m){return s+(m.total_cost||0);},0).toFixed(2) : '0.00') },
    { l: 'Discussions', v: dStats.total || 0 },
    { l: 'Failures (7d)', v: failCount },
    { l: 'Task Success', v: taskCount > 0 ? Math.round(successCount/taskCount*100)+'%' : 'N/A' },
  ].map(function(c) { return '<div class="metric-card glass-sm"><div class="label">'+c.l+'</div><div class="value">'+c.v+'</div></div>'; }).join('');

  // Charts
  CC._renderCostChart(metricsData);
  CC._renderTokenChart(metricsData);
  CC._renderTaskChart(taskData);
  CC._renderFailureChart(failData);
};

CC._chartColors = function() {
  var agents = Object.keys(CC.agents);
  return agents.map(function(n) { return CC.agentColor(n); });
};

CC._destroyChart = function(id) {
  if (CC.chartInstances[id]) { CC.chartInstances[id].destroy(); delete CC.chartInstances[id]; }
};

CC._renderCostChart = function(data) {
  CC._destroyChart('chartCost');
  if (!data || !data.metrics) return;
  var byAgent = {};
  data.metrics.forEach(function(m) { byAgent[m.agent_name] = (byAgent[m.agent_name]||0) + (m.total_cost||0); });
  var labels = Object.keys(byAgent);
  var values = labels.map(function(n) { return byAgent[n]; });
  var colors = labels.map(function(n) { return CC.agentColor(n); });
  CC.chartInstances.chartCost = new Chart(document.getElementById('chartCost'), {
    type: 'bar', data: { labels: labels, datasets: [{ data: values, backgroundColor: colors, borderRadius: 6 }] },
    options: { indexAxis: 'y', responsive: true, plugins: { legend: { display: false } }, scales: { x: { grid: { color: 'rgba(48,54,61,0.3)' }, ticks: { color: '#8b949e' } }, y: { grid: { display: false }, ticks: { color: '#e6edf3' } } } }
  });
};

CC._renderTokenChart = function(data) {
  CC._destroyChart('chartTokens');
  if (!data || !data.metrics) return;
  var byAgent = {};
  data.metrics.forEach(function(m) {
    if (!byAgent[m.agent_name]) byAgent[m.agent_name] = { input: 0, output: 0 };
    byAgent[m.agent_name].input += m.total_input || 0;
    byAgent[m.agent_name].output += m.total_output || 0;
  });
  var labels = Object.keys(byAgent);
  CC.chartInstances.chartTokens = new Chart(document.getElementById('chartTokens'), {
    type: 'bar', data: { labels: labels, datasets: [
      { label: 'Input', data: labels.map(function(n){return byAgent[n].input;}), backgroundColor: 'rgba(88,166,255,0.6)', borderRadius: 4 },
      { label: 'Output', data: labels.map(function(n){return byAgent[n].output;}), backgroundColor: 'rgba(188,140,255,0.6)', borderRadius: 4 }
    ]},
    options: { responsive: true, plugins: { legend: { labels: { color: '#8b949e' } } }, scales: { x: { grid: { display: false }, ticks: { color: '#8b949e' } }, y: { grid: { color: 'rgba(48,54,61,0.3)' }, ticks: { color: '#8b949e' } } } }
  });
};

CC._renderTaskChart = function(data) {
  CC._destroyChart('chartTasks');
  if (!data || !data.tasks) return;
  var counts = { completed: 0, running: 0, failed: 0, pending: 0 };
  data.tasks.forEach(function(t) { counts[t.status] = (counts[t.status]||0)+1; });
  CC.chartInstances.chartTasks = new Chart(document.getElementById('chartTasks'), {
    type: 'doughnut', data: { labels: Object.keys(counts), datasets: [{ data: Object.values(counts),
      backgroundColor: ['#3fb950','#58a6ff','#f85149','#8b949e'] }] },
    options: { responsive: true, plugins: { legend: { position: 'bottom', labels: { color: '#8b949e', padding: 12 } } } }
  });
};

CC._renderFailureChart = function(data) {
  CC._destroyChart('chartFailures');
  if (!data || !data.patterns || data.patterns.length === 0) return;
  var labels = data.patterns.map(function(p) { return p.category || 'unknown'; });
  var values = data.patterns.map(function(p) { return p.occurrences || 1; });
  CC.chartInstances.chartFailures = new Chart(document.getElementById('chartFailures'), {
    type: 'doughnut', data: { labels: labels, datasets: [{ data: values,
      backgroundColor: ['#f85149','#d29922','#58a6ff','#bc8cff','#3fb950','#8b949e'] }] },
    options: { responsive: true, plugins: { legend: { position: 'bottom', labels: { color: '#8b949e', padding: 12 } } } }
  });
};

/* ═══ ACTIVITY (Audit Log) ═══ */
CC.auditPage = 0;
CC.AUDIT_PER_PAGE = 20;

CC.renderActivityView = async function() {
  // Populate filter dropdowns
  var agentSel = document.getElementById('filterAgent');
  if (agentSel.options.length <= 1) {
    Object.keys(CC.agents).forEach(function(n) {
      var opt = document.createElement('option'); opt.value = n; opt.textContent = n;
      agentSel.appendChild(opt);
    });
  }
  CC.auditPage = 0;
  await CC._loadAudit();
};

CC._loadAudit = async function() {
  var agent = document.getElementById('filterAgent').value;
  var action = document.getElementById('filterAction').value;
  var offset = CC.auditPage * CC.AUDIT_PER_PAGE;
  var url = '/api/audit?limit=' + CC.AUDIT_PER_PAGE + '&offset=' + offset;
  if (agent) url += '&agent=' + agent;
  if (action) url += '&action=' + action;
  CC.cache[url] = null; // force fresh
  var data = await CC.api(url);
  if (!data) return;

  // Populate action dropdown from data
  var actionSel = document.getElementById('filterAction');
  if (data && data.audit && actionSel.options.length <= 1) {
    var actions = new Set();
    data.audit.forEach(function(a) { if (a.action) actions.add(a.action); });
    Array.from(actions).sort().forEach(function(act) {
      var opt = document.createElement('option'); opt.value = act; opt.textContent = act;
      actionSel.appendChild(opt);
    });
  }

  var body = document.getElementById('auditBody');
  if (!data || !data.audit || data.audit.length === 0) {
    body.innerHTML = '<tr><td colspan="7" style="text-align:center;color:var(--text-dim);padding:20px">No audit entries</td></tr>';
  } else {
    body.innerHTML = data.audit.map(function(a) {
      var ts = a.timestamp ? new Date(a.timestamp).toLocaleString() : '';
      var ok = a.success ? '<span class="success">\u2713</span>' : '<span class="failure">\u2717</span>';
      return '<tr><td style="font-family:var(--mono);font-size:11px;white-space:nowrap">' + ts + '</td>' +
        '<td style="color:' + CC.agentColor(a.agent_name) + ';font-weight:600">' + (a.agent_name || '-') + '</td>' +
        '<td>' + (a.action || '-') + '</td><td>' + (a.platform || '-') + '</td>' +
        '<td style="max-width:300px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="' + CC.escHtml(a.details || '') + '">' + CC.escHtml((a.details || '').substring(0, 100)) + '</td>' +
        '<td style="font-family:var(--mono)">$' + (a.cost_usd || 0).toFixed(4) + '</td>' +
        '<td>' + ok + '</td></tr>';
    }).join('');
  }

  // Pagination
  var pag = document.getElementById('auditPagination');
  var hasMore = data.audit && data.audit.length >= CC.AUDIT_PER_PAGE;
  pag.innerHTML = '<button class="page-btn" onclick="CC.auditPage=Math.max(0,CC.auditPage-1);CC._loadAudit()"' + (CC.auditPage === 0 ? ' disabled' : '') + '>\u25C0 Prev</button>' +
    '<span style="font-size:12px;color:var(--text-secondary)">Page ' + (CC.auditPage + 1) + '</span>' +
    '<button class="page-btn" onclick="CC.auditPage++;CC._loadAudit()"' + (!hasMore ? ' disabled' : '') + '>Next \u25B6</button>';
};

/* ═══ SETTINGS ═══ */
CC.renderSettingsView = async function() {
  // MCP servers
  var mcpData = await CC.api('/api/mcp');
  var body = document.getElementById('mcpBody');
  var servers = mcpData && (Array.isArray(mcpData) ? mcpData : mcpData.servers);
  if (servers && Array.isArray(servers)) {
    body.innerHTML = servers.map(function(s) {
      var tierClass = s.tier === 'T1' ? 'tier-t1' : s.tier === 'T2' ? 'tier-t2' : 'tier-t3';
      var statusColor = s.status === 'active' || s.status === 'configured' ? 'var(--green)' : 'var(--text-dim)';
      var isActive = s.status === 'active' || s.status === 'configured';
      var isDisabled = s.status === 'disabled';
      var btnHtml = '';
      if (isActive) {
        btnHtml = '<button class="mcp-toggle-btn disable" data-server="' + s.name + '">Disable</button>';
      } else if (isDisabled) {
        btnHtml = '<button class="mcp-toggle-btn enable" data-server="' + s.name + '">Enable</button>';
      }
      return '<tr><td style="font-weight:500">' + s.name + '</td>' +
        '<td>' + (s.category || '-') + '</td>' +
        '<td><span class="tier-badge ' + tierClass + '">' + (s.tier || '?') + '</span></td>' +
        '<td style="color:' + statusColor + '">' + (s.status || 'unknown') + '</td>' +
        '<td style="color:var(--text-secondary);font-size:11px">' + (s.description || '') + '</td>' +
        '<td>' + btnHtml + '</td></tr>';
    }).join('');
    body.querySelectorAll('.mcp-toggle-btn').forEach(function(btn) {
      btn.addEventListener('click', async function() {
        var action = btn.classList.contains('disable') ? 'disable' : 'enable';
        var name = btn.dataset.server;
        btn.disabled = true; btn.textContent = '...';
        await fetch('/api/mcp/' + action, {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ server_name: name })
        });
        CC.cache = {};
        CC.renderSettingsView();
      });
    });
  }

  // System info
  var status = await CC.api('/api/status');
  var infoEl = document.getElementById('systemInfo');
  if (status) {
    infoEl.innerHTML = '<div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;font-size:13px">' +
      '<div>Status: <strong style="color:var(--green)">' + status.status + '</strong></div>' +
      '<div>Agents: <strong>' + status.agents + '</strong></div>' +
      '<div>Total Sessions: <strong>' + status.total_sessions + '</strong></div>' +
      '<div>Total Messages: <strong>' + status.total_messages + '</strong></div>' +
      '<div>Total Cost: <strong>$' + (status.total_cost || 0).toFixed(2) + '</strong></div>' +
      '<div>Active Sessions: <strong>' + status.active_sessions + '</strong></div></div>';
  }
};
