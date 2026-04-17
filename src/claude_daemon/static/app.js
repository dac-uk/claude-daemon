/* ── App initialization & routing ─────────────────────────── */

CC.navigate = function(viewName) {
  CC.currentView = viewName;

  // Update nav buttons
  document.querySelectorAll('.nav-btn').forEach(function(btn) {
    btn.classList.toggle('active', btn.dataset.view === viewName);
  });

  // Show/hide views
  document.querySelectorAll('.view').forEach(function(v) {
    v.classList.toggle('active', v.id === 'view-' + viewName);
  });

  // Load view data
  switch (viewName) {
    case 'overview':
      CC.renderOverviewMetrics();
      CC.renderAgentSidebar();
      CC.renderFeed();
      CC.updateGraph();
      break;
    case 'agents':
      CC.renderAgentsView();
      break;
    case 'chat':
      CC.renderChatView();
      break;
    case 'tasks':
      CC.renderTasksView();
      break;
    case 'analytics':
      CC.renderAnalyticsView();
      break;
    case 'activity':
      CC.renderActivityView();
      break;
    case 'settings':
      CC.renderSettingsView();
      break;
  }

  // Update hash
  location.hash = viewName;
};

CC.init = async function() {
  // Navigation
  document.querySelectorAll('.nav-btn').forEach(function(btn) {
    btn.addEventListener('click', function() { CC.navigate(btn.dataset.view); });
  });

  // Tabs
  document.querySelectorAll('.tab-btn').forEach(function(btn) {
    btn.addEventListener('click', function() {
      var tabId = btn.dataset.tab;
      btn.parentElement.querySelectorAll('.tab-btn').forEach(function(b) { b.classList.remove('active'); });
      btn.classList.add('active');
      btn.parentElement.parentElement.querySelectorAll('.tab-content').forEach(function(c) { c.classList.remove('active'); });
      document.getElementById(tabId).classList.add('active');
    });
  });

  // Stream panel
  document.getElementById('streamClose').addEventListener('click', CC.closeStream);
  document.getElementById('streamOverlay').addEventListener('click', CC.closeStream);

  // Activity filter
  document.getElementById('filterGo').addEventListener('click', function() { CC.auditPage = 0; CC._loadAudit(); });

  // Load initial data
  await CC.fetchAgents();
  await CC.fetchStatus();

  // Initialize force graph
  CC.initGraph();
  CC.updateGraph();

  // Connect WebSocket
  CC.connectWS();

  // Route from hash
  var hash = location.hash.replace('#', '') || 'overview';
  CC.navigate(hash);

  // Refresh status every 30s
  setInterval(CC.fetchStatus, 30000);
};

// Boot
document.addEventListener('DOMContentLoaded', CC.init);
