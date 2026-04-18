/* ── WebSocket connection & event dispatch ────────────────── */

CC.connectWS = function() {
  var proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  var url = proto + '//' + location.host + '/ws';
  CC.ws = new WebSocket(url);
  var indicator = document.getElementById('wsIndicator');

  CC.ws.onopen = function() {
    CC.wsConnected = true;
    indicator.classList.add('connected');
    indicator.title = 'WebSocket connected';
    console.log('[WS] Connected');
  };

  CC.ws.onclose = function() {
    CC.wsConnected = false;
    indicator.classList.remove('connected');
    indicator.title = 'WebSocket disconnected — reconnecting...';
    console.log('[WS] Disconnected, reconnecting in 3s');
    setTimeout(CC.connectWS, 3000);
  };

  CC.ws.onerror = function(e) {
    console.error('[WS] Error', e);
  };

  CC.ws.onmessage = function(e) {
    try {
      var evt = JSON.parse(e.data);
      CC.handleEvent(evt);
      if (CC.alertsHandleEvent) CC.alertsHandleEvent(evt);
    } catch (err) {
      console.error('[WS] Parse error', err);
    }
  };
};

CC.handleEvent = function(evt) {
  switch (evt.type) {
    case 'agent_status':
      var ag = CC.agents[evt.agent];
      if (!ag) break;
      ag.status = evt.status;
      if (evt.status === 'busy') {
        ag.streams = [];
        ag.currentPrompt = evt.prompt || '';
        CC.addFeed(evt.agent, 'busy', 'Processing: ' + (evt.prompt || '').substring(0, 80));
      } else {
        ag.cost += evt.cost || 0;
        ag._wsUpdated = true;
        CC.fetchCosts();
        var dur = evt.duration_ms ? (evt.duration_ms / 1000).toFixed(1) + 's' : '';
        var cost = evt.cost ? ' $' + evt.cost.toFixed(4) : '';
        CC.addFeed(evt.agent, 'idle', 'Done ' + dur + cost);
        if (CC.chatHandleAgentIdle) CC.chatHandleAgentIdle(evt.agent);
      }
      CC.updateActiveCount();
      if (CC.updateGraph) CC.updateGraph();
      if (CC.currentView === 'overview') CC.renderAgentSidebar();
      if (CC.currentView === 'agents') CC.renderAgentsView();
      break;

    case 'stream_delta':
      var ag2 = CC.agents[evt.agent];
      if (ag2) ag2.streams.push(evt.text);
      if (CC.selectedAgent === evt.agent) CC.renderStream();
      if (CC.chatHandleStreamDelta) CC.chatHandleStreamDelta(evt.agent, evt.text);
      break;

    case 'task_update':
      CC.tasks[evt.task_id] = {
        task_id: evt.task_id, agent: evt.agent, status: evt.status,
        result: evt.result, cost: evt.cost
      };
      var label = evt.status === 'completed' ? 'Task completed' : evt.status === 'failed' ? 'Task failed' : 'Task ' + evt.status;
      CC.addFeed(evt.agent, evt.status === 'failed' ? 'error' : 'task', label);
      if (CC.currentView === 'tasks') CC.renderTasksView();
      if (CC.opsHandleEvent) CC.opsHandleEvent(evt);
      break;

    case 'task_created':
      CC.tasks[evt.task_id] = {
        task_id: evt.task_id, agent: evt.agent, status: 'pending',
        prompt: evt.prompt, cost: 0
      };
      CC.addFeed(evt.agent, 'task', 'Task queued: ' + (evt.prompt || '').substring(0, 60));
      if (CC.opsHandleEvent) CC.opsHandleEvent(evt);
      break;

    case 'task_cancelled':
      if (CC.tasks[evt.task_id]) CC.tasks[evt.task_id].status = 'cancelled';
      CC.addFeed(evt.agent, 'task', 'Task cancelled');
      if (CC.opsHandleEvent) CC.opsHandleEvent(evt);
      break;

    case 'metrics_tick':
      if (evt.metrics) {
        evt.metrics.forEach(function(m) {
          if (CC.agents[m.agent_name]) CC.agents[m.agent_name].cost = m.total_cost || 0;
        });
      }
      break;

    case 'goal_update':
    case 'goal_progress':
      if (CC.opsHandleEvent) CC.opsHandleEvent(evt);
      break;

    case 'approval_requested':
      CC.addFeed('system', 'task', 'Approval needed: ' + (evt.reason || '').substring(0, 60));
      if (CC.opsHandleEvent) CC.opsHandleEvent(evt);
      break;

    case 'approval_resolved':
      CC.addFeed(evt.approver || 'system', 'task',
        'Approval ' + evt.outcome + ' for task ' + (evt.task_id || '').substring(0, 12));
      if (CC.opsHandleEvent) CC.opsHandleEvent(evt);
      break;

    case 'budget_update':
      if (CC.opsHandleEvent) CC.opsHandleEvent(evt);
      break;

    case 'budget_exceeded':
      CC.addFeed(evt.scope_value || 'system', 'error',
        'Budget exceeded: ' + evt.scope + ':' + (evt.scope_value || '*') +
        ' $' + (evt.current_spend || 0).toFixed(2) + '/' + (evt.limit_usd || 0).toFixed(2));
      if (CC.opsHandleEvent) CC.opsHandleEvent(evt);
      break;

    case 'auto_parallel':
      CC.addFeed(evt.agent, 'parallel', 'Auto-spawned parallel session');
      break;

    default:
      break;
  }
};
