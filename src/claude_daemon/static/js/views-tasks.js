/* ── Tasks & Discussions view ─────────────────────────────── */

CC.renderTasksView = async function() {
  await CC._renderTaskList();
  await CC._renderDiscussionList();
};

CC._renderTaskList = async function() {
  var el = document.getElementById('taskList');
  if (!el) return;
  var data = await CC.api('/api/tasks');
  if (!data || !data.tasks || data.tasks.length === 0) {
    el.innerHTML = '<div class="empty"><div class="icon">\u2705</div>No active tasks</div>';
    return;
  }
  el.innerHTML = data.tasks.map(function(t) {
    var statusClass = t.status === 'running' ? 'status-busy' : t.status === 'failed' ? 'badge-error' : 'status-idle';
    return '<div class="task-item glass-sm">' +
      '<span class="task-agent" style="color:' + CC.agentColor(t.agent) + '">' + t.agent + '</span>' +
      '<span class="task-prompt">' + CC.escHtml((t.prompt || '').substring(0, 120)) + '</span>' +
      '<span class="task-status ' + statusClass + '">' + t.status + '</span>' +
      '<span class="task-cost">$' + (t.cost || 0).toFixed(4) + '</span></div>';
  }).join('');
};

CC._renderDiscussionList = async function() {
  var el = document.getElementById('discList');
  if (!el) return;
  var data = await CC.api('/api/discussions');
  if (!data || !data.discussions || data.discussions.length === 0) {
    el.innerHTML = '<div class="empty"><div class="icon">\u{1f4ac}</div>No discussions yet</div>';
    return;
  }
  el.innerHTML = data.discussions.map(function(d) {
    var typeClass = d.discussion_type === 'council' ? 'badge-council' : 'badge-discuss';
    var typeLabel = d.discussion_type === 'council' ? 'COUNCIL' : 'BILATERAL';
    var outcomeColor = d.outcome === 'converged' ? 'var(--green)' : d.outcome === 'error' ? 'var(--red)' : 'var(--text-secondary)';
    var participants = '';
    try { participants = JSON.parse(d.participants).join(', '); } catch(e) { participants = d.participants || ''; }
    var ts = d.completed_at ? new Date(d.completed_at).toLocaleString() : '';

    return '<div class="disc-card glass-sm" onclick="this.classList.toggle(\'expanded\')">' +
      '<div class="disc-header">' +
        '<span class="disc-type ' + typeClass + '">' + typeLabel + '</span>' +
        '<span class="disc-topic">' + CC.escHtml(d.topic || 'Untitled') + '</span>' +
      '</div>' +
      '<div class="disc-meta">' +
        '<span>Initiated by <strong>' + (d.initiator || '?') + '</strong></span>' +
        '<span>Participants: ' + CC.escHtml(participants) + '</span>' +
        '<span style="color:' + outcomeColor + '">' + (d.outcome || 'unknown') + '</span>' +
        '<span>' + (d.total_turns || 0) + ' turns</span>' +
        '<span>$' + (d.total_cost_usd || 0).toFixed(4) + '</span>' +
        (ts ? '<span>' + ts + '</span>' : '') +
      '</div>' +
      (d.synthesis ? '<div class="disc-transcript"><strong>Synthesis:</strong>\n' + CC.escHtml(d.synthesis) + '</div>' : '') +
      (d.transcript ? '<div class="disc-transcript"><strong>Full Transcript:</strong>\n' + CC.escHtml(d.transcript) + '</div>' : '') +
    '</div>';
  }).join('');
};
