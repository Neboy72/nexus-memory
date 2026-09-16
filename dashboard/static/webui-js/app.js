/* nexus-memory Web UI — Main Application */

// Escape untrusted values before interpolating them into innerHTML templates.
// Everything rendered into the DOM must pass through this helper.
function escapeHtml(s) {
  return String(s ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

// Shared drift labels (used by the legacy drift Ampel). The colors come from
// NEXUS_DRIFT_COLORS (colors.js) so both stay in one place.
const DRIFT_LABELS = {'fresh':'Fresh','drifting':'Drifting','drifted':'Drifted','not_tracked':'Not Tracked'};

// Paperless titles follow "Datum – Kategorie – Beschreibung" (en dash,
// space-separated). Parsed in one named helper so the expected format and the
// delimiter live in a single documented spot if the upstream format changes.
const PAPERLESS_TITLE_SEPARATOR = ' – ';
function parsePaperlessCategory(title) {
  if (!title) return '';
  return title.split(PAPERLESS_TITLE_SEPARATOR)[1] || '';
}

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
  // graph.html loads this file but has no `.header` element — skip the
  // listener entirely rather than throwing on the first scroll event.
  const header = document.querySelector('.header');

  if (header) {
    window.addEventListener('scroll', () => {
      const scrollY = window.scrollY;
      if (scrollY > 50) {
        header.classList.add('header--scrolled');
      } else {
        header.classList.remove('header--scrolled');
      }
    }, { passive: true });
  }

  // ─── Mobile Menu ───
  const menuToggle = document.getElementById('menuToggle');
  const headerLinks = document.querySelector('.header__links');

  // Both elements are optional (graph.html has no header) — guard them
  // together so a missing links container cannot throw on click.
  if (menuToggle && headerLinks) {
    menuToggle.addEventListener('click', () => {
      const isOpen = headerLinks.classList.toggle('header__links--open');
      menuToggle.setAttribute('aria-expanded', isOpen);
    });
  }

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
  const graphLoading = document.getElementById('graphLoading');

  // Both are optional (graph.html omits graphLoading). Guard the init/lookup
  // so a markup change degrades gracefully instead of aborting the rest of
  // the DOMContentLoaded handler.
  if (graphSvg) MemoryGraph.init(graphSvg);

  // Node selection → detail panel
  MemoryGraph.onNodeSelect = (d) => showDetail(d);
  MemoryGraph.onNodeDeselect = () => hideDetail();

  // Reset graph view button — clears ALL filters back to All + resets zoom
  const resetGraphBtn = document.getElementById('resetGraphBtn');
  resetGraphBtn?.addEventListener('click', () => {
    state.filters = { category: 'all', access_level: 'all', drift: 'all', search: '' };
    const filterCategory = document.getElementById('filterCategory');
    if (filterCategory) filterCategory.value = 'all';
    const filterAccess = document.getElementById('filterAccess');
    if (filterAccess) filterAccess.value = 'all';
    const filterDrift = document.getElementById('filterDrift');
    if (filterDrift) filterDrift.value = 'all';
    const searchInput = document.getElementById('searchInput');
    if (searchInput) searchInput.value = '';
    MemoryGraph.updateFilters(state.filters);
    MemoryGraph.resetZoom();
  });

  // ─── Load Data ───
  // Monotonic request id: overlapping loadData() calls (debounced search,
  // rapid filter changes) must not let an older response overwrite a newer
  // one. Only the latest-started request is allowed to commit state.
  let loadSeq = 0;

  // Captured before the first load so a retry restores the spinner instead of
  // leaving the stale error markup visible while the new request is in flight.
  const graphLoadingDefaultHtml = graphLoading ? graphLoading.innerHTML : '';

  async function loadData() {
    const seq = ++loadSeq;
    try {
      if (graphLoading) {
        graphLoading.innerHTML = graphLoadingDefaultHtml;
        graphLoading.style.display = 'flex';
      }

      const [memData, statsData] = await Promise.all([
        API.getMemories(state.filters),
        API.getStats(),
      ]);

      // A newer request superseded this one while it was in flight.
      if (seq !== loadSeq) return;

      state.memories = memData.memories || [];
      state.edges = memData.edges || [];

      if (graphSvg) MemoryGraph.load(state.memories, state.edges, memData.category_counts || {});

      renderStats(statsData);
      if (graphLoading) graphLoading.style.display = 'none';

    } catch (err) {
      if (seq !== loadSeq) return;
      // The detail (URL/stack) goes to the console; the overlay stays generic.
      console.error('Failed to load data:', err);
      // Drop the stale graph + state so the previous load's data is not left
      // on screen as if it were current, and offer a retry action.
      state.memories = [];
      state.edges = [];
      if (graphSvg) MemoryGraph.load([], [], {});
      if (graphLoading) {
        graphLoading.innerHTML = `
          <p style="color:var(--color-drift-drifted)">⚠️ Failed to load graph data</p>
          <p style="font-size:0.8rem;opacity:0.5;margin-top:8px">${escapeHtml(err.message)}</p>
          <button type="button" id="graphRetryBtn" class="btn btn--secondary" style="margin-top:12px">Retry</button>
        `;
        document.getElementById('graphRetryBtn')?.addEventListener('click', () => { void loadData(); });
      }
    }
  }

  await loadData();

  // ─── Stats ───
  function timeAgo(isoStr) {
    const date = new Date(isoStr);
    if (isNaN(date.getTime())) return '—';
    const diff = Date.now() - date.getTime();
    const mins = Math.floor(diff / 60000);
    if (mins < 1) return 'just now';
    if (mins < 60) return `${mins} min ago`;
    const hours = Math.floor(mins / 60);
    if (hours < 24) return `${hours}h ago`;
    const days = Math.floor(hours / 24);
    return `${days}d ago`;
  }

  // Confidence is a 0..1 ratio; a missing/NaN value must not render as "NaN%".
  // The type check must come FIRST: `null * 100 === 0` and Number.isFinite(0)
  // is true, so a null confidence would otherwise render as "0%".
  function formatConfidence(v) {
    return (typeof v === 'number' && Number.isFinite(v * 100)) ? (v * 100).toFixed(0) + '%' : '-';
  }

  function renderStats(stats) {
    // Legacy stats grid (marketing page)
    const el = (id) => document.getElementById(id);
    if (el('statTotal')) el('statTotal').textContent = stats.total_memories;
    if (el('statEdges')) el('statEdges').textContent = stats.total_edges;
    if (el('statConfidence')) el('statConfidence').textContent = formatConfidence(stats.avg_confidence);
    if (el('statSources')) el('statSources').textContent = stats.total_unique_sources;
    if (el('statCategories')) el('statCategories').textContent = Object.keys(stats.by_category || {}).length;

    // Stats cards (graph view)
    if (el('statTotalMemories')) el('statTotalMemories').textContent = stats.total_memories;
    // Newbie-truth metrics instead of formula numbers (Connections/AvgConfidence
    // were "points minus categories" and a 0.7-default average — meaningless
    // to a fresh user; growth + freshness tell if the memory is ALIVE).
    if (el('statGrownWeek')) el('statGrownWeek').textContent = stats.grown_this_week ?? '-';
    if (el('statLastMemory')) {
      el('statLastMemory').textContent = stats.last_memory_at ? timeAgo(stats.last_memory_at) : '—';
    }

    // Drift Ampel
    const drift = stats.by_drift_status || {};
    if (el('driftFresh')) el('driftFresh').textContent = drift.fresh || 0;
    if (el('driftDrifting')) el('driftDrifting').textContent = drift.drifting || 0;
    if (el('driftDrifted')) el('driftDrifted').textContent = drift.drifted || 0;
    if (el('driftNotTracked')) el('driftNotTracked').textContent = drift.not_tracked || 0;

    // Drift (legacy) — reuses the `drift` map resolved above. Shares
    // DRIFT_LABELS with the Ampel so the labels cannot drift apart.
    const statDrift = el('statDrift');
    if (statDrift) {
      const ampel = [];
      for (const [key, color] of Object.entries(NEXUS_DRIFT_COLORS)) {
        if (drift[key] && drift[key] > 0) {
          ampel.push(`<div style="display:flex;align-items:center;gap:12px;padding:3px 8px">
            <span style="width:12px;height:12px;border-radius:50%;background:${color};display:inline-block;box-shadow:0 0 5px ${color}50;flex-shrink:0"></span>
            <span style="font-size:0.8rem;color:#aaa;flex-shrink:0">${DRIFT_LABELS[key]}</span>
            <span style="font-size:1.1rem;font-weight:700;font-variant-numeric:tabular-nums;color:#fff;text-align:right;flex:1">${drift[key]}</span>
          </div>`);
        }
      }
      statDrift.innerHTML = ampel.join('');
    }
  }

  // ─── Filters ───
  // Access/Drift/Category filtern SERVERSEITIG (Graph lädt nur echte Treffer —
  // clientseitiges Ausgrauen wäre mit nur 500 geladenen Punkten irreführend).
  // Search läuft clientseitig auf dem Volltext der geladenen Punkte.
  async function applyFilters() {
    await loadData();
    MemoryGraph.updateFilters(state.filters);
  }

  // Fire-and-forget refresh from the event handlers: applyFilters() is async,
  // so an unawaited rejection (e.g. loadData's fetch failing) would surface as
  // an unhandled promise rejection. Log it instead of crashing the handler.
  function queueApplyFilters() {
    void applyFilters().catch((err) => { console.error('filter failed', err); });
  }

  // Optional lookups (same pattern as resetGraphBtn): a page variant without
  // one of these controls must not abort the remaining listener registration.
  document.getElementById('filterCategory')?.addEventListener('change', (e) => {
    state.filters.category = e.target.value;
    queueApplyFilters();
  });

  document.getElementById('filterAccess')?.addEventListener('change', (e) => {
    state.filters.access_level = e.target.value;
    queueApplyFilters();
  });

  document.getElementById('filterDrift')?.addEventListener('change', (e) => {
    state.filters.drift = e.target.value;
    queueApplyFilters();
  });

  let searchTimeout;
  document.getElementById('searchInput')?.addEventListener('input', (e) => {
    clearTimeout(searchTimeout);
    searchTimeout = setTimeout(() => {
      state.filters.search = e.target.value.trim();
      queueApplyFilters();
    }, 200);
  });

  // ─── Detail Panel ───
  const detailPanel = document.getElementById('detailPanel');
  const detailBody = document.getElementById('detailBody');
  const detailClose = document.getElementById('detailClose');
  const detailBackdrop = document.getElementById('detailBackdrop');

  function cleanLabel(t) {
    return t.replace(/\b(logo|logos)\b/gi, '').replace(/\s+/g, ' ').trim() || '—';
  }

  function showDetail(d) {
    const driftIcons = { fresh: '🟢', drifting: '🟡', drifted: '🔴' };
    const color = NEXUS_CATEGORY_COLORS[d.category] || '#888';

    const created = d.created_at ? new Date(d.created_at).toLocaleDateString('en-US', {
      year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit'
    }) : '-';

    // Paperless-Kategorie aus Titel parsen: "Datum – Kategorie – Beschreibung"
    const paperCat = parsePaperlessCategory(d.title);

    // Bar width needs a real percentage; a missing confidence must not emit
    // `width:NaN%` (invalid CSS, silently dropped by the browser).
    const confWidth = Number.isFinite(d.confidence * 100) ? (d.confidence * 100) + '%' : '0%';

    detailBody.innerHTML = `
      <div class="detail-node">
        <div class="detail-node__header">
          <span class="detail-node__category" style="background:${color}20;color:${color}">${escapeHtml(d.category || d._category || 'fact')}</span>
          <span class="detail-node__access">${escapeHtml(d.access_level || d.access || 'unknown')}</span>
          <span style="margin-left:auto;font-size:0.8rem;opacity:0.4">${driftIcons[d.drift] || '⚪'} ${escapeHtml(d.drift || 'unknown')}</span>
        </div>
        ${paperCat ? `<div class="detail-node__paper-cat" style="margin-top:4px;font-size:0.85rem;opacity:0.5">${escapeHtml(paperCat)}</div>` : ''}
        <div class="detail-node__text">${escapeHtml(cleanLabel(d.fullText || d.text || d.title || ''))}</div>
        <div class="detail-node__meta">
          <div class="detail-node__meta-item">
            <span class="detail-node__meta-label">Confidence</span>
            <span>${formatConfidence(d.confidence)}</span>
            <div class="detail-node__confidence-bar" style="width:${confWidth}"></div>
          </div>
          <div class="detail-node__meta-item">
            <span class="detail-node__meta-label">Source</span>
            <span>${escapeHtml(d.source)}</span>
          </div>
          <div class="detail-node__meta-item">
            <span class="detail-node__meta-label">Memory ID</span>
            <span style="font-family:var(--font-mono);font-size:0.75rem">${escapeHtml(d.id)}</span>
          </div>
          <div class="detail-node__meta-item">
            <span class="detail-node__meta-label">Created</span>
            <span>${escapeHtml(created)}</span>
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

  detailClose?.addEventListener('click', hideDetail);
  detailBackdrop?.addEventListener('click', hideDetail);
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') hideDetail();
  });

  // ─── Smooth scroll for anchor links ───
  document.querySelectorAll('a[href^="#"]').forEach(anchor => {
    anchor.addEventListener('click', (e) => {
      e.preventDefault();
      // href="#" (and bare "#") is not a valid selector — querySelector('#')
      // throws a SyntaxError. Only resolve/scroll when there is a real target.
      const href = anchor.getAttribute('href');
      if (href && href.length > 1) {
        // A length check is not a selector check: "#!", "#123" or "#a b" make
        // querySelector throw a SyntaxError and abort the click handler.
        let target = null;
        try {
          target = document.querySelector(href);
        } catch (err) {
          console.warn('ignoring invalid anchor selector', href, err);
        }
        if (target) {
          target.scrollIntoView({ behavior: 'smooth', block: 'start' });
        }
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
      tooltipEl.innerHTML = `<span class="tooltip-badge__author">${escapeHtml(author)}</span><span class="tooltip-badge__quote">${escapeHtml(quote)}</span>`;
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
