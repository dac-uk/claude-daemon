/* ── Operations view — native task queue + submission ─────── */

CC.opsState = {
  filter: 'all',       // all | pending | running | completed | failed | cancelled
  tasks: [],           // merged recent list
  expandedId: null,
  budgets: [],         // budget gauge data
  goals: [],           // active goals
};

CC.renderOperationsView = async function() {
  await CC._opsLoad();
  CC._opsRender();
};

CC._opsLoad = async function() {
  CC.cache['/api/v1/tasks/recent?limit=100'] = null;
  CC.cache['/api/v1/budgets'] = null;
  var results = await Promise.all([
    CC.api('/api/v1/tasks/recent?limit=100'),
    CC.api('/api/v1/budgets'),
    CC.api('/api/v1/goals?status=active'),
  ]);
  CC.opsState.tasks = (results[0] && results[0].tasks) || [];
  CC.opsState.budgets = (results[1] && results[1].budgets) || [];
  CC.opsState.goals = (results[2] && results[2].goals) || [];
};

CC._opsFilterChip = function(key, label) {
  var active = CC.opsState.filter === key;
  return '<button class="ops-chip' + (active ? ' active' : '') + '" data-filter="' +
    key + '">' + label + '</button>';
};

CC._opsBudgetGauges = function() {
  var budgets = CC.opsState.budgets.filter(function(b) { return b.enabled; });
  if (budgets.length === 0) return '';
  var html = '<div class="budget-gauges">';
  budgets.forEach(function(b) {
    var pct = b.limit_usd > 0 ? Math.min(100, (b.current_spend / b.limit_usd) * 100) : 0;
    var warn = pct >= 80;
    var label = b.scope === 'global' ? 'Global' :
      (b.scope.charAt(0).toUpperCase() + b.scope.slice(1) + ': ' + (b.scope_value || '*'));
    var deg = (pct / 100) * 360;
    html += '<div class="budget-gauge ' + (warn ? 'warn' : '') + '">' +
      '<div class="budget-gauge-ring" style="background:conic-gradient(' +
        (warn ? 'var(--red)' : 'var(--accent)') + ' ' + deg + 'deg, ' +
        'rgba(255,255,255,0.05) ' + deg + 'deg)">' +
        '<div class="budget-gauge-inner">' +
          '<span class="budget-gauge-pct">' + Math.round(pct) + '%</span>' +
        '</div>' +
      '</div>' +
      '<div class="budget-gauge-label">' + CC.escHtml(label) + '</div>' +
      '<div class="budget-gauge-detail">$' + b.current_spend.toFixed(2) +
        ' / $' + b.limit_usd.toFixed(2) + '</div>' +
      '<div class="budget-gauge-period">' + b.period + '</div>' +
    '</div>';
  });
  html += '</div>';
  return html;
};

CC._opsGoalCards = function() {
  var goals = CC.opsState.goals;
  if (goals.length === 0) return '';
  var html = '<div class="goal-cards">';
  goals.forEach(function(g) {
    var owner = g.owner_agent || 'unassigned';
    var ownerColor = CC.agentColor ? CC.agentColor(owner) : 'var(--text-secondary)';
    var target = g.target_date ? new Date(g.target_date).toLocaleDateString() : '';
    html += '<div class="goal-card glass-sm" data-goal-id="' + g.id + '">' +
      '<div class="goal-card-header">' +
        '<span class="goal-card-title">' + CC.escHtml(g.title) + '</span>' +
        '<span class="goal-card-status">' + g.status + '</span>' +
      '</div>' +
      (g.description
        ? '<div class="goal-card-desc">' + CC.escHtml(g.description).substring(0, 120) + '</div>'
        : '') +
      '<div class="goal-card-bar"><div class="goal-card-fill" id="goalFill' + g.id + '"></div></div>' +
      '<div class="goal-card-meta">' +
        '<span class="goal-card-owner" style="color:' + ownerColor + '">' +
          (CC.AGENT_EMOJI && CC.AGENT_EMOJI[owner] ? CC.AGENT_EMOJI[owner] + ' ' : '') + owner +
        '</span>' +
        (target ? '<span class="goal-card-target">Due ' + target + '</span>' : '') +
      '</div>' +
    '</div>';
  });
  html += '</div>';
  return html;
};

CC._opsLoadGoalProgress = function() {
  CC.opsState.goals.forEach(function(g) {
    CC.api('/api/v1/goals/' + g.id + '/progress').then(function(p) {
      if (!p) return;
      var fill = document.getElementById('goalFill' + g.id);
      if (fill) {
        fill.style.width = (p.pct || 0) + '%';
        fill.title = (p.completed || 0) + '/' + (p.total || 0) + ' tasks';
      }
    });
  });
};

CC._opsStatusColor = function(status) {
  switch (status) {
    case 'running': return 'var(--accent)';
    case 'completed': return 'var(--green)';
    case 'failed': return 'var(--red)';
    case 'cancelled': return 'var(--text-dim)';
    default: return 'var(--yellow, #d29922)';
  }
};

CC._opsRender = function() {
  var el = document.getElementById('view-operations');
  if (!el) return;

  var s = CC.opsState;
  var tasks = s.tasks.filter(function(t) {
    if (s.filter === 'all') return true;
    return t.status === s.filter;
  });

  var counts = { all: s.tasks.length, pending: 0, running: 0, completed: 0, failed: 0, cancelled: 0 };
  s.tasks.forEach(function(t) { counts[t.status] = (counts[t.status] || 0) + 1; });

  var html = '' +
    '<div class="ops-header">' +
      '<h2>Operations</h2>' +
      '<button class="ops-submit-btn" id="opsSubmitBtn">+ New Task</button>' +
    '</div>' +
    '<div class="ops-filters">' +
      CC._opsFilterChip('all', 'All (' + counts.all + ')') +
      CC._opsFilterChip('pending', 'Pending (' + (counts.pending || 0) + ')') +
      CC._opsFilterChip('running', 'Running (' + (counts.running || 0) + ')') +
      CC._opsFilterChip('completed', 'Completed (' + (counts.completed || 0) + ')') +
      CC._opsFilterChip('failed', 'Failed (' + (counts.failed || 0) + ')') +
      CC._opsFilterChip('cancelled', 'Cancelled (' + (counts.cancelled || 0) + ')') +
    '</div>' +
    CC._opsBudgetGauges() +
    CC._opsGoalCards();

  if (tasks.length === 0) {
    html += '<div class="empty glass"><div class="icon">\u26A1</div>' +
            'No tasks match this filter. Submit one above.</div>';
  } else {
    html += '<div class="ops-task-list">';
    tasks.forEach(function(t) {
      var tid = t.id || t.task_id;
      var agent = t.agent_name || t.agent || '?';
      var agentColor = CC.agentColor(agent);
      var prompt = (t.prompt || '').substring(0, 140);
      var cost = (t.cost_usd != null ? t.cost_usd : (t.cost || 0)).toFixed(4);
      var created = t.created_at ? new Date(t.created_at).toLocaleString() : '';
      var statusCol = CC._opsStatusColor(t.status);
      var expanded = s.expandedId === tid;
      html += '<div class="ops-task glass-sm ' + (expanded ? 'expanded' : '') +
              '" data-task-id="' + tid + '">' +
        '<div class="ops-task-row">' +
          '<span class="ops-task-agent" style="color:' + agentColor + '">' +
            (CC.AGENT_EMOJI[agent] || '') + ' ' + agent +
          '</span>' +
          '<span class="ops-task-prompt">' + CC.escHtml(prompt) + '</span>' +
          '<span class="ops-task-status" style="color:' + statusCol + '">' +
            t.status + '</span>' +
          '<span class="ops-task-cost">$' + cost + '</span>' +
        '</div>';
      if (expanded) {
        html += '<div class="ops-task-detail">' +
          '<div><strong>ID:</strong> <code>' + tid + '</code></div>' +
          (created ? '<div><strong>Created:</strong> ' + created + '</div>' : '') +
          (t.user_id ? '<div><strong>User:</strong> ' + CC.escHtml(t.user_id) + '</div>' : '') +
          (t.platform ? '<div><strong>Platform:</strong> ' + CC.escHtml(t.platform) + '</div>' : '') +
          (t.task_type ? '<div><strong>Type:</strong> ' + CC.escHtml(t.task_type) + '</div>' : '') +
          '<div class="ops-task-full-prompt"><strong>Prompt:</strong><br>' +
            CC.escHtml(t.prompt || '') + '</div>' +
          (t.result ? '<div class="ops-task-result"><strong>Result:</strong><br>' +
            CC.escHtml(t.result).replace(/\n/g, '<br>') + '</div>' : '') +
          (t.error ? '<div class="ops-task-error"><strong>Error:</strong> ' +
            CC.escHtml(t.error) + '</div>' : '') +
          (t.status === 'pending' || t.status === 'running'
            ? '<button class="ops-cancel-btn" data-cancel="' + tid + '">Cancel</button>'
            : '') +
        '</div>';
      }
      html += '</div>';
    });
    html += '</div>';
  }

  // Submit modal
  html += '<div class="ops-modal-overlay" id="opsModalOverlay"></div>' +
    '<div class="ops-modal glass" id="opsModal">' +
      '<h3>Submit New Task</h3>' +
      '<label>Agent</label>' +
      '<select id="opsTaskAgent">' +
        '<option value="">Auto-route</option>' +
        Object.keys(CC.agents).map(function(n) {
          return '<option value="' + n + '">' + n + '</option>';
        }).join('') +
      '</select>' +
      '<label>Prompt</label>' +
      '<textarea id="opsTaskPrompt" rows="5" placeholder="Describe the task..."></textarea>' +
      '<div class="ops-modal-actions">' +
        '<button class="ops-modal-cancel" id="opsModalCancel">Cancel</button>' +
        '<button class="ops-modal-submit" id="opsModalSubmit">Submit</button>' +
      '</div>' +
    '</div>';

  el.innerHTML = html;
  CC._opsBindEvents();
  CC._opsLoadGoalProgress();
};

CC._opsBindEvents = function() {
  var el = document.getElementById('view-operations');
  if (!el) return;

  el.querySelectorAll('.ops-chip').forEach(function(btn) {
    btn.addEventListener('click', function() {
      CC.opsState.filter = btn.dataset.filter;
      CC._opsRender();
    });
  });

  el.querySelectorAll('.ops-task-row').forEach(function(row) {
    row.addEventListener('click', function() {
      var tid = row.parentElement.dataset.taskId;
      CC.opsState.expandedId = CC.opsState.expandedId === tid ? null : tid;
      CC._opsRender();
    });
  });

  el.querySelectorAll('.ops-cancel-btn').forEach(function(btn) {
    btn.addEventListener('click', async function(e) {
      e.stopPropagation();
      var tid = btn.dataset.cancel;
      btn.disabled = true;
      btn.textContent = 'Cancelling...';
      await fetch('/api/v1/tasks/' + tid + '/cancel', { method: 'POST' });
      await CC.renderOperationsView();
    });
  });

  var submitBtn = document.getElementById('opsSubmitBtn');
  var modal = document.getElementById('opsModal');
  var overlay = document.getElementById('opsModalOverlay');
  var close = function() { modal.classList.remove('open'); overlay.classList.remove('open'); };
  if (submitBtn) {
    submitBtn.addEventListener('click', function() {
      modal.classList.add('open'); overlay.classList.add('open');
    });
  }
  overlay.addEventListener('click', close);
  document.getElementById('opsModalCancel').addEventListener('click', close);
  document.getElementById('opsModalSubmit').addEventListener('click', async function() {
    var prompt = document.getElementById('opsTaskPrompt').value.trim();
    var agent = document.getElementById('opsTaskAgent').value || null;
    if (!prompt) return;
    var body = { prompt: prompt };
    if (agent) body.agent = agent;
    var res = await fetch('/api/v1/tasks', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (res.ok) {
      close();
      await CC.renderOperationsView();
    } else {
      alert('Task submission failed');
    }
  });
};

CC.escHtml = CC.escHtml || function(s) {
  if (s == null) return '';
  return String(s).replace(/[&<>"']/g, function(c) {
    return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
  });
};

/* Hook for WebSocket events to live-refresh */
CC.opsHandleEvent = function(evt) {
  if (CC.currentView !== 'operations') return;
  var live = ['task_created', 'task_update', 'task_cancelled',
              'budget_update', 'budget_exceeded',
              'goal_update', 'goal_progress'];
  if (live.indexOf(evt.type) >= 0) {
    CC.renderOperationsView();
  }
};
