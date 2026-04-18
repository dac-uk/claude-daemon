/* ── Tasks & Discussions view ─────────────────────────────── */

CC.formatTranscript = function(transcript) {
  if (!transcript) return '';
  // Parse turn markers like "## albert (CIO):" or "**johnny:**" or "### Turn 1: albert"
  var lines = transcript.split('\n');
  var html = '<div class="transcript-turns">';
  var currentAgent = '';
  var currentText = '';

  function flush() {
    if (currentAgent && currentText.trim()) {
      var color = CC.agentColor(currentAgent.toLowerCase());
      var emoji = CC.AGENT_EMOJI[currentAgent.toLowerCase()] || '';
      html += '<div class="transcript-turn" style="border-left:3px solid ' + color + ';padding:8px 12px;margin:8px 0">'
        + '<div style="font-weight:600;font-size:12px;color:' + color + '">' + emoji + ' ' + currentAgent + '</div>'
        + '<div style="font-size:13px;margin-top:4px;white-space:pre-wrap">' + CC.escHtml(currentText.trim()) + '</div>'
        + '</div>';
    }
    currentText = '';
  }

  for (var i = 0; i < lines.length; i++) {
    var line = lines[i];
    // Match patterns like "## albert (CIO):", "**albert:**", "### Turn N: albert"
    var match = line.match(/^(?:#{1,3}\s+(?:Turn\s+\d+:\s+)?)?(\w+)\s*(?:\([^)]*\))?\s*:/i) ||
                line.match(/^\*\*(\w+)(?:\s*\([^)]*\))?\s*:\*\*/i);
    if (match && CC.agents[match[1].toLowerCase()]) {
      flush();
      currentAgent = match[1];
    } else {
      currentText += line + '\n';
    }
  }
  flush();
  html += '</div>';
  return html;
};


CC.renderTasksView = async function() {
  await CC._renderDiscussionList();
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
      (d.synthesis ? '<div class="disc-transcript"><strong>Synthesis:</strong><br>' + CC.escHtml(d.synthesis).replace(/\n/g, '<br>') + '</div>' : '') +
      (d.transcript ? '<div class="disc-transcript"><strong>Transcript:</strong>' + CC.formatTranscript(d.transcript) + '</div>' : '') +
    '</div>';
  }).join('');
};
