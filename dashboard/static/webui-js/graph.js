/* nexus-memory — D3.js v7 Force Graph (SVG, Mike Bostock Vorlage) */
/* Category colors live in colors.js (shared with app.js). */
const CC = NEXUS_CATEGORY_COLORS;

const MemoryGraph = {
  svg: null, sim: null, g: null, link: null, node: null, _container: null,

  init(el) {
    this._container = el;
    el.innerHTML = '';
    const w = el.clientWidth || 800, h = el.clientHeight || 500;
    this.svg = d3.select(el).append('svg')
      .attr('width', '100%').attr('height', '100%')
      .style('background', '#0a0e1a').style('cursor', 'grab')
      .style('overflow', 'visible');
    // Hintergrund-Rect für Maus-Events (direkt im SVG, nicht in g)
    this.svg.append('rect')
      .attr('width', '100%').attr('height', '100%')
      .attr('fill', 'none').attr('pointer-events', 'all');
    this.g = this.svg.append('g');
    // Keep the bound zoom behaviour so resetZoom() can reuse it instead of
    // building a throwaway d3.zoom() each call (which loses the bound extent).
    this._zoom = d3.zoom().scaleExtent([0.1,8]).on('zoom', (e) => {
      this.g.attr('transform', e.transform);
    });
    this.svg.call(this._zoom);
    this.svg.on('dblclick.zoom', null);
  },

  load(memories, edges) {
    // A previous simulation keeps ticking (and mutating detached nodes)
    // unless it is stopped explicitly.
    if (this.sim) this.sim.stop();
    // A missing/partial API response (memData.memories undefined) must not
    // throw where the edges list is already defaulted. Both inputs get the
    // same treatment so a memoryless response renders an empty graph.
    const mems = Array.isArray(memories) ? memories : [];
    const edgeList = Array.isArray(edges) ? edges : [];
    // load() is reachable before/without a successful init(); without `this.g`
    // there is no SVG to draw into.
    if (!this.g) return;
    const idSet = new Set(mems.map(m => m.id));
    const seen = new Set();
    const links = [];
    edgeList.forEach(e => {
      if (!idSet.has(e.source)||!idSet.has(e.target)) return;
      const k = [e.source,e.target].sort().join('|');
      if (!seen.has(k)) { seen.add(k); links.push({source: e.source, target: e.target}); }
    });

    const nodes = mems.map(m => {
      const fullText = (m.text || '');
      return {
        id: m.id, text: (m.text || '').slice(0, 80),
        fullText: fullText,
        title: m.title || '',
        category: m.category ?? 'fact',
        access_level: m.access_level ?? 'unknown',
        // `??` not `||`: a real confidence of 0 must stay 0, not become 0.7.
        confidence: m.confidence ?? 0.7, drift: m.drift ?? 'not_tracked',
        source: m.source || '', created_at: m.created_at || null,
      };
    });

    this.g.selectAll('*').remove();
    this.link = this.g.append('g').selectAll('line').data(links).join('line')
      .attr('stroke', '#8bc4f0').attr('stroke-opacity', 0.5).attr('stroke-width', 1.2);

    this.node = this.g.append('g').selectAll('g').data(nodes).join('g')
      .style('cursor', 'pointer');

    this.node.append('circle')
      .attr('r', 5).attr('fill', d => CC[d.category]||'#6b7280')
      .attr('stroke', '#fff').attr('stroke-width', 0.5);

    this.node.append('text')
      .attr('dx', 8).attr('dy', 4)
      .attr('font-size', '7px').attr('fill', '#ffffffaa')
      .attr('font-family', 'sans-serif')
      .text(d => {
        let t = d.text.replace(/\s+/g, ' ').trim();
        t = t.split(' ').slice(0,4).join(' ');
        return `[${d.category}] ${t}`;
      });

    this.sim = d3.forceSimulation(nodes)
      .force('link', d3.forceLink(links).id(d=>d.id).distance(40).strength(0.3))
      .force('charge', d3.forceManyBody().strength(-30))
      .force('center', d3.forceCenter(this._container.clientWidth/2, this._container.clientHeight/2))
      .force('collide', d3.forceCollide(8))
      .alphaDecay(0.02)
      .on('tick', () => {
        this.link.attr('x1',d=>d.source.x).attr('y1',d=>d.source.y)
          .attr('x2',d=>d.target.x).attr('y2',d=>d.target.y);
        this.node.attr('transform', d=>`translate(${d.x},${d.y})`);
      });

    this.node.on('click', (e,d) => {
      e.stopPropagation();
      if (this.onNodeSelect) this.onNodeSelect(d);
    });
    this.svg.on('click', () => { if(this.onNodeDeselect) this.onNodeDeselect(); });
  },

  updateFilters(f) {
    if (!f) return;
    // Reachable before a successful load() (e.g. a filter change after a failed
    // fetch); without bound selections there is nothing to filter.
    if (!this.node || !this.link) return;
    const q = (f.search || '').toLowerCase();
    const visible = {};
    this.node.attr('opacity', d => {
      let ok = true;
      if (q && !(d.fullText || d.text || '').toLowerCase().includes(q) && !(d.title || '').toLowerCase().includes(q)) ok = false;
      visible[d.id] = ok;
      return ok ? 1 : 0.05;
    });
    this.link.attr('opacity', l => {
      const sId = typeof l.source === 'object' ? l.source.id : l.source;
      const tId = typeof l.target === 'object' ? l.target.id : l.target;
      return (visible[sId] && visible[tId]) ? 0.5 : 0.05;
    });
  },

  resetZoom() {
    // `this.svg`/`this._zoom` only exist after a successful init(); the button
    // that calls this is reachable regardless.
    if (!this.svg || !this._zoom) return;
    this.svg.transition().duration(500).call(
      this._zoom.transform, d3.zoomIdentity
    );
  },
};
