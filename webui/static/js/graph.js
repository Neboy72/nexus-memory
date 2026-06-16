/* nexus-memory — D3.js v7 Force Graph (SVG, Mike Bostock Vorlage) */
const CC = { fact:'#3b82f6', belief:'#8b5cf6', session:'#f59e0b', rule:'#10b981', preference:'#ec4899', temp:'#6b7280' };

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
    this.svg.call(d3.zoom().scaleExtent([0.1,8]).on('zoom', (e) => {
      this.g.attr('transform', e.transform);
    }));
    this.svg.on('dblclick.zoom', null);
  },

  load(memories, edges) {
    const idSet = new Set(memories.map(m => m.id));
    const seen = new Set();
    const links = [];
    (edges||[]).forEach(e => {
      if (!idSet.has(e.source)||!idSet.has(e.target)) return;
      const k = [e.source,e.target].sort().join('|');
      if (!seen.has(k)) { seen.add(k); links.push({source: e.source, target: e.target}); }
    });

    const nodes = memories.map(m => ({
      id: m.id, text: (m.text||'').slice(0,80),
      title: m.title || '', category: m.category||'fact',
      confidence: m.confidence||0.7, drift: m.drift||'fresh',
      source: m.source||'', created_at: m.created_at||null,
    }));

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
        let t = d.text.replace(/\b(logo|logos)\b/gi, '').replace(/\s+/g, ' ').trim();
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
    this.node.attr('opacity', d => {
      if (f.category&&f.category!=='all'&&d.category!==f.category) return 0.05;
      return 1;
    });
  },

  resetZoom() {
    this.svg.transition().duration(500).call(
      d3.zoom().transform, d3.zoomIdentity
    );
  },
};
