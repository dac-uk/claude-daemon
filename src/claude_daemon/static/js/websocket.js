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
        CC.totalCost += evt.cost || 0;
        document.getElementById('statCost').textContent = '$' + CC.totalCost.toFixed(2);
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
      break;

    case 'metrics_tick':
      if (evt.metrics) {
        evt.metrics.forEach(function(m) {
          if (CC.agents[m.agent_name]) CC.agents[m.agent_name].cost = m.total_cost || 0;
        });
      }
      break;

    case 'auto_parallel':
      CC.addFeed(evt.agent, 'parallel', 'Auto-spawned parallel session');
      break;

    default:
      break;
  }
};
