/* nexus-memory Web UI — Main Application */

document.addEventListener('DOMContentLoaded', async () => {

  // ─── State ───
  const state = {
    memories: [],
    edges: [],
    filters: {
      category: 'all',
      access_level: 'all',
      drift: 'all',
      search: '',
    },
  };

  // ─── Theme (optional — nur wenn Button existiert) ───
  const themeToggle = document.getElementById('themeToggle');
  const html = document.documentElement;

  function setTheme(theme) {
    html.setAttribute('data-theme', theme);
    localStorage.setItem('nexus-theme', theme);
  }

  // Load saved theme or respect system preference
  const saved = localStorage.getItem('nexus-theme');
  if (saved) {
    setTheme(saved);
  } else {
    const prefersDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
    setTheme(prefersDark ? 'dark' : 'light');
  }

  if (themeToggle) {
    themeToggle.addEventListener('click', () => {
      const current = html.getAttribute('data-theme');
      setTheme(current === 'dark' ? 'light' : 'dark');
    });
  }

  // ─── Header Scroll Effect ───
  const header = document.querySelector('.header');
  let lastScroll = 0;

  window.addEventListener('scroll', () => {
    const scrollY = window.scrollY;
    if (scrollY > 50) {
      header.classList.add('header--scrolled');
    } else {
      header.classList.remove('header--scrolled');
    }
    lastScroll = scrollY;
  }, { passive: true });

  // ─── Mobile Menu ───
  const menuToggle = document.getElementById('menuToggle');
  const headerLinks = document.querySelector('.header__links');

  menuToggle?.addEventListener('click', () => {
    const isOpen = headerLinks.classList.toggle('header__links--open');
    menuToggle.setAttribute('aria-expanded', isOpen);
  });

  // Close mobile menu on link click
  headerLinks?.querySelectorAll('.header__link').forEach(link => {
    link.addEventListener('click', () => {
      headerLinks.classList.remove('header__links--open');
      menuToggle.setAttribute('aria-expanded', 'false');
    });
  });

  // ─── Hero Particles (optional) ───
  function createParticles() {
    const container = document.getElementById('heroParticles');
    if (!container) return;
    const count = 40;
    for (let i = 0; i < count; i++) {
      const p = document.createElement('div');
      p.className = 'hero__particle';
      p.style.left = Math.random() * 100 + '%';
      p.style.width = (1 + Math.random() * 3) + 'px';
      p.style.height = p.style.width;
      p.style.animationDuration = (10 + Math.random() * 30) + 's';
      p.style.animationDelay = (Math.random() * 20) + 's';
      const colors = ['rgba(59,130,246,0.4)', 'rgba(99,102,241,0.3)', 'rgba(139,92,246,0.3)'];
      p.style.background = colors[Math.floor(Math.random() * colors.length)];
      container.appendChild(p);
    }
  }
  createParticles();

  // ─── Graph ───
  const graphSvg = document.getElementById('graphSvg');
  const graphTooltip = document.getElementById('graphTooltip');
  const graphLoading = document.getElementById('graphLoading');

  // Set SVG dimensions
  const graphContainer = document.getElementById('graphContainer');
  const initW = Math.max(graphContainer.clientWidth, 800);
  const initH = Math.max(graphContainer.clientHeight, 500);
  graphSvg.setAttribute('width', initW);
  graphSvg.setAttribute('height', initH);

  MemoryGraph.init(graphSvg);

  // Node selection → detail panel
  MemoryGraph.onNodeSelect = (d) => showDetail(d);
  MemoryGraph.onNodeDeselect = () => hideDetail();

  // Reset graph view button
  document.getElementById('resetGraphBtn').addEventListener('click', () => {
    MemoryGraph.resetZoom();
  });

  // ─── Load Data ───
  async function loadData() {
    try {
      graphLoading.style.display = 'flex';

      const [memData, statsData] = await Promise.all([
        API.getMemories(),
        API.getStats(),
      ]);

      state.memories = memData.memories;
      state.edges = memData.edges;

      MemoryGraph.load(state.memories, state.edges);

      renderStats(statsData);
      graphLoading.style.display = 'none';

    } catch (err) {
      console.error('Failed to load data:', err);
      graphLoading.innerHTML = `
        <p style="color:var(--color-drift-drifted)">⚠️ Failed to load graph data</p>
        <p style="font-size:0.8rem;opacity:0.5;margin-top:8px">${err.message}</p>
      `;
    }
  }

  await loadData();

  // ─── Stats ───
  function renderStats(stats) {
    // Legacy stats grid (marketing page)
    const el = (id) => document.getElementById(id);
    if (el('statTotal')) el('statTotal').textContent = stats.total_memories;
    if (el('statEdges')) el('statEdges').textContent = stats.total_edges;
    if (el('statConfidence')) el('statConfidence').textContent = (stats.avg_confidence * 100).toFixed(0) + '%';
    if (el('statSources')) el('statSources').textContent = stats.total_unique_sources;
    if (el('statCategories')) el('statCategories').textContent = Object.keys(stats.by_category).length;

    // Stats cards (graph view)
    if (el('statTotalMemories')) el('statTotalMemories').textContent = stats.total_memories;
    if (el('statConnections')) el('statConnections').textContent = stats.total_edges;
    if (el('statAvgConfidence')) el('statAvgConfidence').textContent = (stats.avg_confidence * 100).toFixed(0) + '%';
    if (el('statSources')) el('statSources').textContent = stats.total_unique_sources;

    // Drift Ampel
    const drift = stats.by_drift_status || {};
    if (el('driftFresh')) el('driftFresh').textContent = drift.fresh || 0;
    if (el('driftDrifting')) el('driftDrifting').textContent = drift.drifting || 0;
    if (el('driftDrifted')) el('driftDrifted').textContent = drift.drifted || 0;

    // Drift (legacy)
    const statDrift = el('statDrift');
    if (statDrift) {
      const drift = stats.by_drift_status || {};
      const ampel = [];
      const colors = {'fresh':'#22c55e','drifting':'#eab308','drifted':'#ef4444'};
      for (const [key, color] of Object.entries(colors)) {
        if (drift[key] && drift[key] > 0) {
          ampel.push(`<div style="display:flex;align-items:center;gap:12px;padding:3px 8px">
            <span style="width:12px;height:12px;border-radius:50%;background:${color};display:inline-block;box-shadow:0 0 5px ${color}50;flex-shrink:0"></span>
            <span style="font-size:1.1rem;font-weight:700;font-variant-numeric:tabular-nums;color:#fff;text-align:right;flex:1">${drift[key]}</span>
          </div>`);
        }
      }
      statDrift.innerHTML = ampel.join('');
    }
  }

  // ─── Filters ───
  function applyFilters() {
    MemoryGraph.updateFilters(state.filters);
  }

  document.getElementById('filterCategory').addEventListener('change', (e) => {
    state.filters.category = e.target.value;
    applyFilters();
  });

  document.getElementById('filterAccess').addEventListener('change', (e) => {
    state.filters.access_level = e.target.value;
    applyFilters();
  });

  document.getElementById('filterDrift').addEventListener('change', (e) => {
    state.filters.drift = e.target.value;
    applyFilters();
  });

  let searchTimeout;
  document.getElementById('searchInput').addEventListener('input', (e) => {
    clearTimeout(searchTimeout);
    searchTimeout = setTimeout(() => {
      state.filters.search = e.target.value.trim();
      applyFilters();
    }, 200);
  });

  // ─── Detail Panel ───
  const detailPanel = document.getElementById('detailPanel');
  const detailBody = document.getElementById('detailBody');
  const detailClose = document.getElementById('detailClose');
  const detailBackdrop = document.getElementById('detailBackdrop');

  function showDetail(d) {
    const catColors = {
      fact: '#3b82f6', belief: '#8b5cf6', session: '#f59e0b',
      rule: '#10b981', preference: '#ec4899', temp: '#6b7280',
    };
    const driftIcons = { fresh: '🟢', drifting: '🟡', drifted: '🔴' };
    const color = catColors[d.category] || '#888';

    const created = d.created_at ? new Date(d.created_at).toLocaleDateString('en-US', {
      year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit'
    }) : '-';

    detailBody.innerHTML = `
      <div class="detail-node">
        <div class="detail-node__header">
          <span class="detail-node__category" style="background:${color}20;color:${color}">${d.category}</span>
          <span class="detail-node__access">${d.access_level || d.access || 'public'}</span>
          <span style="margin-left:auto;font-size:0.8rem;opacity:0.4">${driftIcons[d.drift] || '⚪'} ${d.drift}</span>
        </div>
        <div class="detail-node__text">${d.fullText}</div>
        <div class="detail-node__meta">
          <div class="detail-node__meta-item">
            <span class="detail-node__meta-label">Confidence</span>
            <span>${(d.confidence * 100).toFixed(0)}%</span>
            <div class="detail-node__confidence-bar" style="width:${d.confidence * 100}%"></div>
          </div>
          <div class="detail-node__meta-item">
            <span class="detail-node__meta-label">Source</span>
            <span>${d.source}</span>
          </div>
          <div class="detail-node__meta-item">
            <span class="detail-node__meta-label">Memory ID</span>
            <span style="font-family:var(--font-mono);font-size:0.75rem">${d.id}</span>
          </div>
          <div class="detail-node__meta-item">
            <span class="detail-node__meta-label">Created</span>
            <span>${created}</span>
          </div>
        </div>
      </div>
    `;

    detailPanel.classList.add('detail-panel--open');
    document.body.style.overflow = 'hidden';
  }

  function hideDetail() {
    detailPanel.classList.remove('detail-panel--open');
    document.body.style.overflow = '';
  }

  detailClose.addEventListener('click', hideDetail);
  detailBackdrop.addEventListener('click', hideDetail);
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') hideDetail();
  });

  // ─── Smooth scroll for anchor links ───
  document.querySelectorAll('a[href^="#"]').forEach(anchor => {
    anchor.addEventListener('click', (e) => {
      e.preventDefault();
      const target = document.querySelector(anchor.getAttribute('href'));
      if (target) {
        target.scrollIntoView({ behavior: 'smooth', block: 'start' });
      }
    });
  });

  // ─── Badge Tooltips ───
  let tooltipEl = null;

  document.querySelectorAll('[data-author][data-quote]').forEach(el => {
    el.classList.add('has-tooltip');

    el.addEventListener('mouseenter', (e) => {
      const author = el.getAttribute('data-author');
      const quote = el.getAttribute('data-quote');
      if (!quote) return;

      tooltipEl = document.createElement('div');
      tooltipEl.className = 'tooltip-badge';
      tooltipEl.innerHTML = `<span class="tooltip-badge__author">${author}</span><span class="tooltip-badge__quote">${quote}</span>`;
      document.body.appendChild(tooltipEl);

      positionTooltip(e);
    });

    el.addEventListener('mousemove', (e) => {
      if (tooltipEl) positionTooltip(e);
    });

    el.addEventListener('mouseleave', () => {
      if (tooltipEl) { tooltipEl.remove(); tooltipEl = null; }
    });
  });

  function positionTooltip(e) {
    if (!tooltipEl) return;
    const x = e.clientX;
    const y = e.clientY + 16;
    tooltipEl.style.left = x + 'px';
    tooltipEl.style.top = y + 'px';

    // Keep in viewport
    const rect = tooltipEl.getBoundingClientRect();
    if (rect.right > window.innerWidth) {
      tooltipEl.style.left = (window.innerWidth - rect.width - 10) + 'px';
    }
    if (rect.bottom > window.innerHeight) {
      tooltipEl.style.top = (e.clientY - rect.height - 10) + 'px';
    }
  }

  console.log('🔷 Nexus Memory Web UI loaded');
});
