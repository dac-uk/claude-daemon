/* ── D3 Force Graph ──────────────────────────────────────── */

CC.graphSim = null;
CC.graphSvg = null;
CC.graphNodes = [];
CC.graphLinks = [];

CC.initGraph = function() {
  var container = document.getElementById('graphEl');
  if (!container) return;
  var w = container.clientWidth || 600;
  var h = container.clientHeight || 400;

  var svg = d3.select('#graphEl').append('svg').attr('width', w).attr('height', h);
  CC.graphSvg = svg;

  // Glow filter
  var defs = svg.append('defs');
  var filter = defs.append('filter').attr('id', 'glow');
  filter.append('feGaussianBlur').attr('stdDeviation', '4').attr('result', 'blur');
  var merge = filter.append('feMerge');
  merge.append('feMergeNode').attr('in', 'blur');
  merge.append('feMergeNode').attr('in', 'SourceGraphic');

  // Groups
  svg.append('g').attr('class', 'links');
  svg.append('g').attr('class', 'nodes');

  CC.graphSim = d3.forceSimulation()
    .force('link', d3.forceLink().id(function(d) { return d.id; }).distance(120))
    .force('charge', d3.forceManyBody().strength(-300))
    .force('center', d3.forceCenter(w / 2, h / 2))
    .force('collision', d3.forceCollide(45));

  // Resize
  window.addEventListener('resize', function() {
    var nw = container.clientWidth;
    var nh = container.clientHeight;
    svg.attr('width', nw).attr('height', nh);
    CC.graphSim.force('center', d3.forceCenter(nw / 2, nh / 2));
    CC.graphSim.alpha(0.3).restart();
  });
};

CC.updateGraph = function() {
  if (!CC.graphSvg) return;
  var svg = CC.graphSvg;
  var agents = Object.values(CC.agents);
  if (agents.length === 0) return;

  // Build nodes & links
  var orch = agents.find(function(a) { return a.is_orchestrator; });
  var orchName = orch ? orch.name : agents[0].name;

  CC.graphNodes = agents.map(function(a) {
    return { id: a.name, emoji: a.emoji || CC.AGENT_EMOJI[a.name] || '', role: a.role, color: a.color, status: a.status };
  });
  CC.graphLinks = agents.filter(function(a) { return a.name !== orchName; }).map(function(a) {
    return { source: orchName, target: a.name };
  });

  // Links
  var linkSel = svg.select('.links').selectAll('line').data(CC.graphLinks, function(d) { return d.source.id || d.source + '-' + (d.target.id || d.target); });
  linkSel.exit().remove();
  var linkEnter = linkSel.enter().append('line')
    .attr('stroke', 'rgba(88,166,255,0.15)').attr('stroke-width', 2);
  var allLinks = linkEnter.merge(linkSel);

  // Nodes
  var nodeSel = svg.select('.nodes').selectAll('.node-g').data(CC.graphNodes, function(d) { return d.id; });
  nodeSel.exit().remove();

  var nodeEnter = nodeSel.enter().append('g').attr('class', 'node-g').style('cursor', 'pointer');

  // Outer circle (glow ring for busy)
  nodeEnter.append('circle').attr('class', 'ring').attr('r', 32)
    .attr('fill', 'none').attr('stroke-width', 3);

  // Main circle
  nodeEnter.append('circle').attr('class', 'main').attr('r', 28)
    .attr('stroke-width', 2);

  // Emoji
  nodeEnter.append('text').attr('class', 'emoji')
    .attr('text-anchor', 'middle').attr('dominant-baseline', 'central')
    .attr('font-size', '20px').attr('pointer-events', 'none');

  // Label
  nodeEnter.append('text').attr('class', 'label')
    .attr('text-anchor', 'middle').attr('dy', 46)
    .attr('font-size', '11px').attr('font-weight', '600')
    .attr('fill', '#e6edf3').attr('pointer-events', 'none');

  // Role sublabel
  nodeEnter.append('text').attr('class', 'sublabel')
    .attr('text-anchor', 'middle').attr('dy', 60)
    .attr('font-size', '9px').attr('fill', '#8b949e').attr('pointer-events', 'none');

  // Click
  nodeEnter.on('click', function(event, d) {
    CC.openStream(d.id);
  });

  // Drag
  nodeEnter.call(d3.drag()
    .on('start', function(event, d) { if (!event.active) CC.graphSim.alphaTarget(0.3).restart(); d.fx = d.x; d.fy = d.y; })
    .on('drag', function(event, d) { d.fx = event.x; d.fy = event.y; })
    .on('end', function(event, d) { if (!event.active) CC.graphSim.alphaTarget(0); d.fx = null; d.fy = null; })
  );

  var allNodes = nodeEnter.merge(nodeSel);

  // Update visuals
  allNodes.select('.main')
    .attr('fill', function(d) { return d.color + '22'; })
    .attr('stroke', function(d) { return d.color; });

  allNodes.select('.ring')
    .attr('stroke', function(d) { return d.status === 'busy' ? d.color : 'transparent'; })
    .attr('opacity', function(d) { return d.status === 'busy' ? 0.6 : 0; })
    .attr('filter', function(d) { return d.status === 'busy' ? 'url(#glow)' : 'none'; });

  // Pulse animation for busy
  allNodes.select('.ring').each(function(d) {
    var el = d3.select(this);
    if (d.status === 'busy') {
      (function animatePulse() {
        el.transition().duration(1000).attr('r', 38).attr('opacity', 0.2)
          .transition().duration(1000).attr('r', 32).attr('opacity', 0.6)
          .on('end', function() { if (d.status === 'busy') animatePulse(); });
      })();
    } else {
      el.interrupt();
    }
  });

  allNodes.select('.emoji').text(function(d) { return d.emoji; });
  allNodes.select('.label').text(function(d) { return d.id.charAt(0).toUpperCase() + d.id.slice(1); });
  allNodes.select('.sublabel').text(function(d) { return d.role || ''; });

  // Simulation
  CC.graphSim.nodes(CC.graphNodes).on('tick', function() {
    allLinks.attr('x1', function(d) { return d.source.x; }).attr('y1', function(d) { return d.source.y; })
            .attr('x2', function(d) { return d.target.x; }).attr('y2', function(d) { return d.target.y; });
    allNodes.attr('transform', function(d) { return 'translate(' + d.x + ',' + d.y + ')'; });
  });
  CC.graphSim.force('link').links(CC.graphLinks);
  CC.graphSim.alpha(0.5).restart();
};
