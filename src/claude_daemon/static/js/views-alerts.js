/* ── Alerts view ───────────────────────────────────────────── */

CC._alertsCache = [];
CC._alertsFilter = { severity: '', search: '' };
CC._alertsBound = false;
CC._alertsLoadPending = false;

(function loadDismissed() {
  try {
    var raw = localStorage.getItem('CC.alertsDismissed');
    var arr = raw ? JSON.parse(raw) : [];
    CC._alertsDismissed = new Set(Array.isArray(arr) ? arr : []);
  } catch (e) {
    CC._alertsDismissed = new Set();
  }
})();

CC._persistDismissed = function() {
  try {
    localStorage.setItem(
      'CC.alertsDismissed',
      JSON.stringify(Array.from(CC._alertsDismissed)),
    );
  } catch (e) {}
};

CC.renderAlertsView = async function() {
  CC._bindAlertsControls();
  await CC._loadAlerts();
  CC._renderAlertsList();
};

CC._loadAlerts = async function() {
  if (CC._alertsLoadPending) return;
  CC._alertsLoadPending = true;
  try {
    var data = await CC.api('/api/alerts?limit=200');
    CC._alertsCache = (data && data.alerts) || [];
  } finally {
    CC._alertsLoadPending = false;
  }
};

CC._bindAlertsControls = function() {
  if (CC._alertsBound) return;
  CC._alertsBound = true;

  var chips = document.querySelectorAll('#alertFilters .chip');
  chips.forEach(function(c) {
    c.addEventListener('click', function() {
      chips.forEach(function(x) { x.classList.remove('active'); });
      c.classList.add('active');
      CC._alertsFilter.severity = c.dataset.sev || '';
      CC._renderAlertsList();
    });
  });

  var search = document.getElementById('alertSearch');
  if (search) {
    search.addEventListener('input', function() {
      CC._alertsFilter.search = (search.value || '').toLowerCase();
      CC._renderAlertsList();
    });
  }

  var refresh = document.getElementById('alertsRefresh');
  if (refresh) {
    refresh.addEventListener('click', async function() {
      refresh.disabled = true;
      try { await CC._loadAlerts(); CC._renderAlertsList(); }
      finally { refresh.disabled = false; }
    });
  }

  var clear = document.getElementById('alertsClearDismissed');
  if (clear) {
    clear.addEventListener('click', function() {
      CC._alertsDismissed = new Set();
      CC._persistDismissed();
      CC._renderAlertsList();
    });
  }

  var dismissAll = document.getElementById('alertsDismissAll');
  if (dismissAll) {
    dismissAll.addEventListener('click', function() {
      var visible = CC._alertsVisibleList();
      if (visible.length === 0) return;
      var msg = 'Dismiss ' + visible.length + ' alert' +
                (visible.length === 1 ? '' : 's') +
                ' currently shown? Use "Reset dismissed" to undo.';
      if (!window.confirm(msg)) return;
      visible.forEach(function(a) { CC._alertsDismissed.add(a.id); });
      CC._persistDismissed();
      CC._renderAlertsList();
    });
  }

  // Action buttons (delegated)
  var list = document.getElementById('alertsList');
  if (list) {
    list.addEventListener('click', function(e) {
      var btn = e.target.closest('[data-alert-action]');
      if (!btn) return;
      var action = btn.dataset.alertAction;
      var entity = btn.dataset.alertEntity;
      var id = btn.dataset.alertId;
      CC._handleAlertAction(action, entity, id);
    });
  }
};

CC._handleAlertAction = async function(action, entity, alertId) {
  if (action === 'dismiss' && alertId) {
    CC._alertsDismissed.add(alertId);
    CC._persistDismissed();
    CC._renderAlertsList();
    return;
  }
  if (action === 'approve' && entity) {
    await CC.api('/api/v1/approvals/' + encodeURIComponent(entity) + '/approve', {
      method: 'POST',
      body: JSON.stringify({ approver: 'dashboard' }),
    });
    await CC._loadAlerts();
    CC._renderAlertsList();
    return;
  }
  if (action === 'reject' && entity) {
    await CC.api('/api/v1/approvals/' + encodeURIComponent(entity) + '/reject', {
      method: 'POST',
      body: JSON.stringify({ approver: 'dashboard' }),
    });
    await CC._loadAlerts();
    CC._renderAlertsList();
    return;
  }
  if (action === 'view_task' && entity) {
    // Navigate to operations view (best-effort — tasks live there).
    if (typeof CC.navigate === 'function') CC.navigate('operations');
    return;
  }
};

CC._alertMatches = function(a, f) {
  if (f.severity && a.severity !== f.severity) return false;
  if (f.search) {
    var hay = ((a.title || '') + ' ' + (a.message || '') + ' ' +
               (a.agent || '') + ' ' + (a.source || '')).toLowerCase();
    if (hay.indexOf(f.search) === -1) return false;
  }
  return true;
};

CC._alertsVisibleList = function() {
  var all = CC._alertsCache || [];
  return all
    .filter(function(a) { return !CC._alertsDismissed.has(a.id); })
    .filter(function(a) { return CC._alertMatches(a, CC._alertsFilter); });
};

CC._renderAlertsList = function() {
  var el = document.getElementById('alertsList');
  var countEl = document.getElementById('alertsCount');
  if (!el) return;
  var all = CC._alertsCache || [];
  var visible = all.filter(function(a) { return !CC._alertsDismissed.has(a.id); });
  var filtered = CC._alertsVisibleList();

  var unresolved = visible.filter(function(a) {
    return ['critical', 'error', 'warning'].indexOf(a.severity) >= 0;
  }).length;
  CC.setAlertsBadge(unresolved);

  if (countEl) {
    if (all.length === 0) {
      countEl.textContent = 'No alerts';
    } else {
      countEl.textContent = filtered.length + ' of ' + all.length +
        ' (' + CC._alertsDismissed.size + ' dismissed)';
    }
  }

  if (all.length === 0) {
    el.innerHTML = '<div class="empty"><div class="icon">\u2705</div>Nothing to worry about.</div>';
    return;
  }
  if (filtered.length === 0) {
    el.innerHTML = '<div class="empty"><div class="icon">\u{1f50d}</div>No alerts match the current filters</div>';
    return;
  }

  el.innerHTML = filtered.map(CC._renderAlertCard).join('');
};

CC._renderAlertCard = function(a) {
  var sev = a.severity || 'info';
  var ts = a.timestamp ? new Date(a.timestamp).toLocaleString() : '';
  var actionsHtml = '';
  var actions = Array.isArray(a.actions) ? a.actions.slice() : [];
  actions.push({ label: 'Dismiss', action: 'dismiss' });
  actionsHtml = '<div class="alert-actions">' + actions.map(function(act) {
    return '<button class="page-btn small" ' +
      'data-alert-action="' + CC.escHtml(act.action) + '" ' +
      'data-alert-entity="' + CC.escHtml(String(act.entity != null ? act.entity : '')) + '" ' +
      'data-alert-id="' + CC.escHtml(a.id) + '">' +
      CC.escHtml(act.label) + '</button>';
  }).join('') + '</div>';

  var tbHtml = '';
  if (a.traceback) {
    tbHtml = '<pre class="alert-traceback">' + CC.escHtml(a.traceback) + '</pre>';
  }

  var agentChip = a.agent
    ? '<span class="alert-agent-chip">' + CC.escHtml(a.agent) + '</span>'
    : '';

  return '<div class="alert-card glass-sm sev-' + sev + '">' +
    '<div class="alert-row">' +
      '<span class="alert-sev sev-' + sev + '">' + sev.toUpperCase() + '</span>' +
      '<span class="alert-source">' + CC.escHtml(a.source || '') + '</span>' +
      agentChip +
      (ts ? '<span class="alert-ts">' + ts + '</span>' : '') +
    '</div>' +
    '<div class="alert-title">' + CC.escHtml(a.title || '') + '</div>' +
    '<div class="alert-message">' + CC.escHtml(a.message || '') + '</div>' +
    tbHtml +
    actionsHtml +
  '</div>';
};

CC.setAlertsBadge = function(count) {
  var badge = document.getElementById('alertsBadge');
  if (!badge) return;
  if (count && count > 0) {
    badge.textContent = count > 99 ? '99+' : String(count);
    badge.hidden = false;
  } else {
    badge.hidden = true;
  }
};

CC.alertsHandleEvent = function(evt) {
  if (!evt) return;
  var triggers = [
    'task_update', 'task_cancelled', 'task_created',
    'budget_exceeded', 'budget_update',
    'approval_requested', 'approval_resolved',
  ];
  if (triggers.indexOf(evt.type) === -1) return;

  // Refresh the cache in the background so the list & badge stay live.
  // No-op if a load is already in flight.
  CC._loadAlerts().then(function() {
    if (CC.currentView === 'alerts') CC._renderAlertsList();
    else {
      var unresolved = (CC._alertsCache || []).filter(function(a) {
        return !CC._alertsDismissed.has(a.id) &&
          ['critical', 'error', 'warning'].indexOf(a.severity) >= 0;
      }).length;
      CC.setAlertsBadge(unresolved);
    }
  });
};
