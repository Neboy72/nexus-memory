/* nexus-memory Landing Page — D3.js v7 Force Graph (DEMO, separate Instanz) */
const CC = { fact:'#3b82f6', belief:'#8b5cf6', session:'#f59e0b', rule:'#10b981', preference:'#ec4899', temp:'#6b7280' };
const CAT_ORDER = ['fact','belief','session','rule','preference','temp'];

const MemoryGraph = {
  svg: null, sim: null, g: null, link: null, node: null, _container: null,

  init(el) {
    this._container = el;
    el.innerHTML = '';

    // Warte auf Layout → nimm Eltern-Grösse
    const parent = el.parentElement;
    const w = parent ? parent.clientWidth : 800;
    const h = parent ? parent.clientHeight : 600;

    this.svg = d3.select(el).append('svg')
      .attr('width', w).attr('height', h)
      .style('background', '#0a0e1a').style('cursor', 'grab');

    this.svg.append('rect')
      .attr('width', w).attr('height', h)
      .attr('fill', 'none').attr('pointer-events', 'all');

    this.g = this.svg.append('g');

    // Zentrum für Force
    this._cx = w / 2;
    this._cy = h / 2;

    this.svg.call(d3.zoom().scaleExtent([0.1, 8]).on('zoom', (e) => {
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

    // Nodes mit zufälligen Start-Positionen um das Zentrum, nach Kategorie gruppiert
    const catAngle = {};
    CAT_ORDER.forEach((c, i) => { catAngle[c] = (i / CAT_ORDER.length) * 2 * Math.PI; });
    const nodes = memories.map((m, i) => {
      const cat = m.category||'fact';
      const angle = catAngle[cat] || (i * 0.7);
      const radius = 80 + (i % 6) * 25;
      return {
        id: m.id,
        text: (m.text||'').slice(0, 100),
        title: m.title || '',
        category: cat,
        confidence: m.confidence||0.7,
        drift: m.drift||'fresh',
        source: m.source||'',
        created_at: m.created_at||null,
        // Initialpositionen um Zentrum verteilt
        x: this._cx + Math.cos(angle) * radius,
        y: this._cy + Math.sin(angle) * radius,
      };
    });

    this.g.selectAll('*').remove();

    this.link = this.g.append('g').selectAll('line').data(links).join('line')
      .attr('stroke', '#8bc4f0').attr('stroke-opacity', 0.5).attr('stroke-width', 1.2);

    this.node = this.g.append('g').selectAll('g').data(nodes).join('g')
      .style('cursor', 'pointer');

    this.node.append('circle')
      .attr('r', 7).attr('fill', d => CC[d.category]||'#6b7280')
      .attr('stroke', '#fff').attr('stroke-width', 0.5);

    this.node.append('text')
      .attr('dx', 10).attr('dy', 4)
      .attr('font-size', '8px').attr('fill', '#ffffffcc')
      .attr('font-family', 'sans-serif')
      .text(d => {
        const cleanText = d.title && d.title.length > 0 ? d.title : d.text.replace(/\b(logo|logos)\b/gi, '').replace(/\s+/g, ' ').trim();
        const short = cleanText.split(' ').slice(0, 5).join(' ');
        return `[${d.category}] ${short}`;
      });

    this.sim = d3.forceSimulation(nodes)
      .force('link', d3.forceLink(links).id(d => d.id).distance(60).strength(0.4))
      .force('charge', d3.forceManyBody().strength(-120))
      .force('center', d3.forceCenter(this._cx, this._cy))
      .force('collide', d3.forceCollide(20))
      .alpha(0.6)
      .alphaDecay(0.02)
      .on('tick', () => {
        this.link
          .attr('x1', d => d.source.x).attr('y1', d => d.source.y)
          .attr('x2', d => d.target.x).attr('y2', d => d.target.y);
        this.node.attr('transform', d => `translate(${d.x},${d.y})`);
      });

    this.node.on('click', (e, d) => {
      e.stopPropagation();
      if (this.onNodeSelect) this.onNodeSelect(d);
    });
    this.svg.on('click', () => { if(this.onNodeDeselect) this.onNodeDeselect(); });
  },

  updateFilters(f) {
    this.node.attr('opacity', d => {
      if (f.category && f.category !== 'all' && d.category !== f.category) return 0.05;
      return 1;
    });
  },

  resetZoom() {
    if (!this.svg) return;
    this.svg.transition().duration(500).call(
      d3.zoom().transform, d3.zoomIdentity
    );
  },
};
