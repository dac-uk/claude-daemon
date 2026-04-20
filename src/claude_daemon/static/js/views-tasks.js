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


CC._discussionsCache = [];
CC._discFilters = { type: '', outcome: '', agent: '', search: '' };

CC.renderTasksView = async function() {
  await CC._loadDiscussions();
  CC._wireDiscussionFilters();
  CC._renderDiscussionList();
};

CC._loadDiscussions = async function() {
  var data = await CC.api('/api/discussions');
  CC._discussionsCache = (data && data.discussions) || [];

  // Populate agent filter from participants seen across all discussions
  var agentSel = document.getElementById('discFilterAgent');
  if (agentSel && agentSel.options.length <= 1) {
    var names = {};
    CC._discussionsCache.forEach(function(d) {
      try { JSON.parse(d.participants).forEach(function(p) { names[p] = 1; }); } catch(e) {}
      if (d.initiator) names[d.initiator] = 1;
    });
    Object.keys(names).sort().forEach(function(n) {
      var opt = document.createElement('option');
      opt.value = n; opt.textContent = n;
      agentSel.appendChild(opt);
    });
  }
};

CC._wireDiscussionFilters = function() {
  if (CC._discFiltersWired) return;
  CC._discFiltersWired = true;
  var inputs = ['discFilterType', 'discFilterOutcome', 'discFilterAgent', 'discFilterSearch'];
  inputs.forEach(function(id) {
    var el = document.getElementById(id);
    if (!el) return;
    var evt = (id === 'discFilterSearch') ? 'input' : 'change';
    el.addEventListener(evt, function() {
      CC._discFilters.type = document.getElementById('discFilterType').value;
      CC._discFilters.outcome = document.getElementById('discFilterOutcome').value;
      CC._discFilters.agent = document.getElementById('discFilterAgent').value;
      CC._discFilters.search = document.getElementById('discFilterSearch').value.toLowerCase();
      CC._renderDiscussionList();
    });
  });
};

CC._discussionMatches = function(d, f) {
  if (f.type && d.discussion_type !== f.type) return false;
  if (f.outcome && d.outcome !== f.outcome) return false;
  if (f.agent) {
    var participants = [];
    try { participants = JSON.parse(d.participants); } catch(e) {}
    if (participants.indexOf(f.agent) === -1 && d.initiator !== f.agent) return false;
  }
  if (f.search) {
    var hay = ((d.topic || '') + ' ' + (d.synthesis || '') + ' ' + (d.transcript || '') +
               ' ' + (d.initiator || '') + ' ' + (d.participants || '')).toLowerCase();
    if (hay.indexOf(f.search) === -1) return false;
  }
  return true;
};

CC._renderDiscussionList = function() {
  var el = document.getElementById('discList');
  var countEl = document.getElementById('discCount');
  if (!el) return;
  var all = CC._discussionsCache || [];
  var filtered = all.filter(function(d) { return CC._discussionMatches(d, CC._discFilters); });
  if (countEl) {
    countEl.textContent = filtered.length === all.length
      ? (all.length + ' discussion' + (all.length === 1 ? '' : 's'))
      : (filtered.length + ' of ' + all.length);
  }
  if (all.length === 0) {
    el.innerHTML = '<div class="empty"><div class="icon">\u{1f4ac}</div>No discussions yet</div>';
    return;
  }
  if (filtered.length === 0) {
    el.innerHTML = '<div class="empty"><div class="icon">\u{1f50d}</div>No discussions match the current filters</div>';
    return;
  }
  el.innerHTML = filtered.map(function(d) {
    var typeClass = d.discussion_type === 'council' ? 'badge-council' : 'badge-discuss';
    var typeLabel = d.discussion_type === 'council' ? 'COUNCIL' : 'BILATERAL';
    var outcomeColor = d.outcome === 'converged' ? 'var(--green)' : d.outcome === 'error' ? 'var(--red)' : 'var(--text-secondary)';
    var participants = '';
    try { participants = JSON.parse(d.participants).join(', '); } catch(e) { participants = d.participants || ''; }
    var ts = d.completed_at ? new Date(d.completed_at).toLocaleString() : '';

    // Show linked action tasks if present
    var actionIds = [];
    try { actionIds = JSON.parse(d.action_task_ids || '[]'); } catch(e) {}
    var actionsHtml = actionIds.length
      ? '<div class="disc-actions"><strong>Actions spawned:</strong> ' + actionIds.length +
        ' task' + (actionIds.length === 1 ? '' : 's') +
        ' <a href="#operations" onclick="CC.navigate(\'operations\');return false;">' +
        'View in Operations &rarr;</a></div>'
      : '';

    return '<div class="disc-card glass-sm" data-discussion-id="' + (d.id || '') + '" onclick="this.classList.toggle(\'expanded\')">' +
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
      actionsHtml +
      (d.synthesis ? '<div class="disc-transcript"><strong>Synthesis:</strong><br>' + CC.escHtml(d.synthesis).replace(/\n/g, '<br>') + '</div>' : '') +
      (d.transcript ? '<div class="disc-transcript"><strong>Transcript:</strong>' + CC.formatTranscript(d.transcript) + '</div>' : '') +
    '</div>';
  }).join('');

  // Auto-expand a deep-linked discussion card
  if (CC._pendingDiscussionId) {
    var target = el.querySelector('[data-discussion-id="' + CC._pendingDiscussionId + '"]');
    if (target) {
      target.classList.add('expanded');
      target.scrollIntoView({ behavior: 'smooth', block: 'center' });
    }
    CC._pendingDiscussionId = null;
  }
};
