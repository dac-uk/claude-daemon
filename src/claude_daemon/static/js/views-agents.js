/* ── Agents view: detailed agent cards ────────────────────── */

CC.renderAgentsView = function() {
  var el = document.getElementById('agentsGrid');
  if (!el) return;
  var agents = Object.values(CC.agents);
  if (agents.length === 0) { el.innerHTML = '<div class="empty"><div class="icon">...</div>Loading agents</div>'; return; }

  agents.sort(function(a, b) {
    if (a.is_orchestrator !== b.is_orchestrator) return a.is_orchestrator ? -1 : 1;
    return a.name.localeCompare(b.name);
  });

  el.innerHTML = agents.map(function(a) {
    var statusClass = a.status === 'busy' ? 'status-busy' : 'status-idle';
    var statusLabel = a.status === 'busy' ? 'BUSY' : 'IDLE';
    var bg = a.color + '18';
    var orchBadge = a.is_orchestrator ? ' <span style="font-size:9px;color:var(--orange);font-weight:600">ORCHESTRATOR</span>' : '';

    return '<div class="agent-card glass" onclick="CC.openStream(\'' + a.name + '\')" style="cursor:pointer">' +
      '<div class="accent-bar" style="background:' + a.color + '"></div>' +
      '<div class="agent-card-header">' +
        '<div class="avatar" style="background:' + bg + '">' + (a.emoji || '') + '</div>' +
        '<div class="info"><h3>' + a.name.charAt(0).toUpperCase() + a.name.slice(1) + orchBadge + '</h3>' +
        '<p>' + (a.role || 'Agent') + '</p></div>' +
      '</div>' +
      '<div class="status-line"><span class="status-badge ' + statusClass + '">' + statusLabel + '</span>' +
        (a.status === 'busy' ? '<span style="font-size:11px;color:var(--text-secondary);margin-left:4px">' + CC.escHtml((a.currentPrompt || '').substring(0, 50)) + '</span>' : '') +
      '</div>' +
      '<dl class="meta">' +
        '<dt>Model</dt><dd>' + (a.model || 'default') + '</dd>' +
        '<dt>Cost</dt><dd>$' + (a.cost || 0).toFixed(4) + '</dd>' +
        '<dt>MCP</dt><dd>' + (a.mcp_health || 'n/a') + '</dd>' +
        '<dt>Heartbeats</dt><dd>' + (a.heartbeat_tasks || 0) + '</dd>' +
      '</dl></div>';
  }).join('');
};
