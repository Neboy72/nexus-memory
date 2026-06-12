/* nexus-memory Web UI — Force-Directed Graph (ChatGPT optimized) */

const MemoryGraph = {
  svg: null, sim: null, nodes: [], edges: [],
  g: null, zoom: null, _w: 800, _h: 600,

  init(svgEl) {
    this.svg = d3.select(svgEl);
    const c = svgEl.parentElement;
    this._w = c.clientWidth || 800;
    this._h = c.clientHeight || 500;
    svgEl.setAttribute('width', this._w);
    svgEl.setAttribute('height', this._h);

    const defs = this.svg.append('defs');
    defs.append('filter').attr('id','sd')
      .append('feDropShadow').attr('dx',0).attr('dy',1).attr('stdDeviation',1.5).attr('flood-opacity',0.2);

    this.g = this.svg.append('g');

    this.zoom = d3.zoom().scaleExtent([0.1,8])
      .on('zoom', e => this.g.attr('transform', e.transform));
    this.svg.call(this.zoom);

    this.svg.on('click', () => { if(this.onNodeDeselect) this.onNodeDeselect(); });
  },

  load(memories, edges) {
    const catColors = { fact:'#3b82f6', belief:'#8b5cf6', session:'#f59e0b', rule:'#10b981', preference:'#ec4899', temp:'#6b7280' };
    const driftColors = { fresh:'#22c55e', drifting:'#eab308', drifted:'#ef4444' };

    this.nodes = memories.map(m => ({
      id: m.id,
      label: m.text.split(' ').slice(0,3).join(' ').replace(/[^a-zA-Z0-9äöüß -]/g,'') || '…',
      fullText: m.text, category: m.category || 'fact', access_level: m.access_level || 'public',
      confidence: m.confidence || 0.7, drift: m.drift || 'fresh', source: m.source || '',
      created_at: m.created_at || null,
      r: 4 + (m.confidence||0.7) * 11,
      color: catColors[m.category] || '#6b7280', driftColor: driftColors[m.drift] || '#22c55e',
    }));

    const ids = new Set(this.nodes.map(n => n.id));
    this.edges = (edges||[]).filter(e => ids.has(e.source) && ids.has(e.target) && e.source !== e.target);
    this._render();
  },

  _render() {
    this.g.selectAll('*').remove();

    const edgeLayer = this.g.append('g');
    const nodeLayer = this.g.append('g');
    const labelLayer = this.g.append('g');

    // Edges
    const link = edgeLayer.selectAll('line').data(this.edges).join('line')
      .attr('stroke','#6a9fcf').attr('stroke-width',1.5).attr('stroke-opacity',0.45);

    // Nodes
    const node = nodeLayer.selectAll('g').data(this.nodes).join('g').style('cursor','pointer');

    node.append('circle').attr('class','dr')
      .attr('r', d => d.r+3).attr('fill','none')
      .attr('stroke', d => d.driftColor).attr('stroke-width', d => d.drift==='fresh'?1:1.8)
      .attr('stroke-opacity', d => d.drift==='fresh'?0.2:0.5);

    node.append('circle').attr('class','co')
      .attr('r', d => d.r).attr('fill', d => d.color)
      .attr('stroke', d => d3.color(d.color).darker(0.4)).attr('stroke-width',1)
      .attr('filter','url(#sd)').attr('opacity',0.85);

    // Labels
    labelLayer.selectAll('text').data(this.nodes).join('text')
      .attr('text-anchor','middle').attr('font-size','8px')
      .attr('font-family',"'Inter',sans-serif").attr('font-weight',500)
      .attr('fill','currentColor').attr('opacity',0.6).text(d => d.label);

    // Events
    node.on('mouseenter', (e,d) => {
      const connected = new Set();
      this.edges.forEach(e => { const s=e.source.id||e.source, t=e.target.id||e.target; if(s===d.id) connected.add(t); if(t===d.id) connected.add(s); });
      node.each(function(n) { d3.select(this).select('.co').transition(150).attr('opacity', (n.id===d.id||connected.has(n.id))?1:0.1); });
      link.transition(150).attr('stroke-opacity', e => { const s=e.source.id||e.source, t=e.target.id||e.target; return (s===d.id||t===d.id)?0.7:0.03; }).attr('stroke-width', e => { const s=e.source.id||e.source, t=e.target.id||e.target; return (s===d.id||t===d.id)?2.5:0.5; });
      labelLayer.selectAll('text').transition(150).attr('opacity', n => (n.id===d.id||connected.has(n.id))?1:0.08);
    });
    node.on('mouseleave', () => {
      node.select('.co').transition(200).attr('opacity',0.85);
      link.transition(200).attr('stroke-opacity',0.45).attr('stroke-width',1.5);
      labelLayer.selectAll('text').transition(200).attr('opacity',0.6);
    });
    node.on('click', (e,d) => {
      e.stopPropagation();
      node.select('.co').attr('stroke-width',1).attr('stroke', n => d3.color(n.color).darker(0.4));
      d3.select(e.currentTarget).select('.co').attr('stroke','#fff').attr('stroke-width',2.5);
      if(this.onNodeSelect) this.onNodeSelect(d);
    });

    // ─── Drag (standard D3 — no bounding box clamping!) ───
    const drag = d3.drag()
      .on('start', (e,d) => { if(!e.active) this.sim.alphaTarget(0.3).restart(); d.fx=d.x; d.fy=d.y; })
      .on('drag', (e,d) => { d.fx=e.x; d.fy=e.y; })
      .on('end', (e,d) => { if(!e.active) this.sim.alphaTarget(0); d.fx=null; d.fy=null; });
    node.call(drag);

    // ─── Simulation (ChatGPT recommendations) ───
    // No bounding box clamping! No Math.max() constraints on x/y!
    this.sim = d3.forceSimulation(this.nodes)
      .force('link', d3.forceLink(this.edges).id(d=>d.id).distance(120).strength(0.08))
      .force('charge', d3.forceManyBody().strength(-400))
      .force('collision', d3.forceCollide().radius(30).strength(1))
      .force('center', d3.forceCenter(this._w/2, this._h/2).strength(0.05))
      .alphaDecay(0.01).alphaMin(0.002).velocityDecay(0.3)
      .on('tick', () => {
        link.attr('x1', d=>d.source.x).attr('y1', d=>d.source.y)
            .attr('x2', d=>d.target.x).attr('y2', d=>d.target.y);
        node.attr('transform', d => `translate(${d.x},${d.y})`);
        labelLayer.selectAll('text').attr('x', d=>d.x).attr('y', d=>d.y+d.r+12);
      });

    setTimeout(() => this._fit(), 3500);
  },

  _fit() {
    if(!this.nodes.length) return;
    const pad = 60;
    let x0=Infinity, y0=Infinity, x1=-Infinity, y1=-Infinity;
    this.nodes.forEach(d => { if(d.x<x0) x0=d.x; if(d.y<y0) y0=d.y; if(d.x>x1) x1=d.x; if(d.y>y1) y1=d.y; });
    const bw = Math.max(x1-x0,1), bh = Math.max(y1-y0,1);
    const sc = Math.min((this._w-pad*2)/bw, (this._h-pad*2)/bh, 1.5);
    this.svg.transition().duration(500).call(this.zoom.transform,
      d3.zoomIdentity.translate(this._w/2-(x0+x1)/2*sc, this._h/2-(y0+y1)/2*sc).scale(sc));
  },

  updateFilters(filters) {
    const g=this.g; if(!g) return;
    const cat=filters.category, acc=filters.access_level, drf=filters.drift, q=(filters.search||'').toLowerCase();
    g.selectAll('g > g').attr('opacity', d => {
      if(!d||!d.category) return 1;
      if(cat&&cat!=='all'&&d.category!==cat) return 0.04;
      if(acc&&acc!=='all'&&d.access_level!==acc) return 0.04;
      if(drf&&drf!=='all'&&d.drift!==drf) return 0.04;
      if(q) {
        const txt = ((d.fullText||'') + ' ' + (d.label||'')).toLowerCase();
        if(!txt.includes(q)) return 0.04;
      }
      return 1;
    });
    g.selectAll('text').attr('opacity',0.6);
    g.selectAll('line').attr('stroke-opacity',0.25);
  },

  resetZoom() { this._fit(); },
};
