/* nexus-memory Dashboard — Memory Inspector (Astra item 3) */
/* Parity with webui/static/js/inspector.js, adapted to the dashboard API shape. */

const Inspector = {
  items: [],
  filter: '',
  _t: null,

  async init(containerId) {
    this.el = document.getElementById(containerId);
    if (!this.el) return;
    this.render();
    await this.load();
  },

  async load() {
    try {
      const data = await API.getMemories({ limit: 2000 });
      const list = (data.memories || []).slice();
      list.sort((a, b) => new Date(b.created_at || 0) - new Date(a.created_at || 0));
      this.items = list;
      this.render();
    } catch (e) {
      this.el.innerHTML = `<div class="insp-error">Inspector load failed: ${e.message}</div>`;
    }
  },

  render() {
    const q = this.filter.toLowerCase();
    const list = q
      ? this.items.filter(m => (m.text || '').toLowerCase().includes(q) ||
          (m.source || '').toLowerCase().includes(q) || (m.category || '').includes(q))
      : this.items;

    this.el.innerHTML = `
      <div class="insp-toolbar">
        <input id="inspSearch" placeholder="Filter by text, source, category…"
               value="${this.filter.replace(/"/g, '&quot;')}" autocomplete="off">
        <span class="insp-count">${list.length} of ${this.items.length} memories</span>
        <button class="insp-btn" id="inspReload" title="Reload from Qdrant">↻</button>
      </div>
      <div class="insp-list" id="inspList">
        ${list.slice(0, 200).map(m => `
          <div class="insp-row" data-id="${this.escAttr(m.id)}">
            <span class="insp-row__cat insp-row__cat--${this.safeCat(m.category)}">${this.esc(m.category)}</span>
            <span class="insp-row__text">${this.esc((m.text || '').slice(0, 140))}</span>
            <span class="insp-row__meta">${this.esc(m.source || 'unknown')} · ${Math.round((m.confidence || 0.7) * 100)}%</span>
            <button class="insp-btn" data-act="why" title="Why this memory?">?</button>
            <button class="insp-btn" data-act="edit" title="Edit text">✎</button>
            <button class="insp-btn insp-btn--danger" data-act="dep" title="Soft-delete (recoverable)">🗑</button>
          </div>`).join('')}
        ${list.length > 200 ? `<div class="insp-more">…and ${list.length - 200} more (use the filter)</div>` : ''}
      </div>`;

    const search = document.getElementById('inspSearch');
    if (search) {
      search.addEventListener('input', (e) => {
        clearTimeout(this._t);
        this._t = setTimeout(() => { this.filter = e.target.value.trim(); this.render(); this.restoreFocus(); }, 180);
      });
    }
    const reload = document.getElementById('inspReload');
    if (reload) reload.addEventListener('click', () => this.load());
    this.el.querySelectorAll('.insp-btn[data-act]').forEach(btn => {
      btn.addEventListener('click', (e) => this.action(btn.dataset.act, btn.closest('.insp-row').dataset.id, e));
    });
  },

  restoreFocus() {
    const s = document.getElementById('inspSearch');
    if (s) { s.focus(); s.setSelectionRange(s.value.length, s.value.length); }
  },

  safeCat(c) { return ['fact','belief','session','rule','preference','temp'].includes(c) ? c : 'fact'; },
  escAttr(t) { return String(t || '').replace(/"/g, '&quot;').replace(/</g, '&lt;'); },
  esc(t) { const d = document.createElement('div'); d.textContent = t || ''; return d.innerHTML; },

  async action(act, id, ev) {
    if (ev) ev.stopPropagation();
    try {
      if (act === 'why') return await this.showWhy(id);
      if (act === 'edit') return await this.edit(id);
      if (act === 'dep') {
        if (!confirm('Soft-delete this memory? It is marked deprecated and can be restored anytime (nothing is erased).')) return;
        await this.post(`/api/memories/${id}/deprecate`);
        alert('Marked deprecated. Restore is one click in the WHY view.');
        await this.load();
      }
      if (act === 'restore') {
        await this.post(`/api/memories/${id}/restore`);
        alert('Restored to canonical.');
        await this.load();
      }
    } catch (e) { alert('Action failed: ' + e.message); }
  },

  async post(url) {
    const res = await fetch(url, { method: 'POST' });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return res.json();
  },

  async showWhy(id) {
    const data = await API.fetch(`/api/memories/${encodeURIComponent(id)}/why`);
    const dep = data.lifecycle_status === 'deprecated';
    this.modal(`
      <h3 style="margin:0 0 12px">Why this memory</h3>
      <div class="insp-why__text">${this.esc(data.text)}</div>
      <table class="insp-why__table">
        <tr><td>Category</td><td>${this.esc(data.category || '—')}</td></tr>
        <tr><td>Access level</td><td>${this.esc(data.access_level || '—')}</td></tr>
        <tr><td>Area (scope)</td><td>${this.esc(data.scope || 'default')}</td></tr>
        <tr><td>Lifecycle</td><td>${this.esc(data.lifecycle_status || 'canonical (legacy)')}</td></tr>
        <tr><td>Source</td><td>${this.esc(data.source || '—')} ${data.source_url ? `· <a href="${this.escAttr(data.source_url)}" target="_blank" rel="noopener">link</a>` : ''}</td></tr>
        <tr><td>Confidence</td><td>${data.confidence != null ? Math.round(data.confidence * 100) + '%' : '—'}</td></tr>
        <tr><td>Created</td><td>${this.esc(data.created_at || '—')}</td></tr>
        <tr><td>Agent</td><td>${this.esc(data.agent || '—')}</td></tr>
        ${data.superseded ? `<tr><td>Superseded</td><td>${this.esc(JSON.stringify(data.superseded).slice(0, 80))}</td></tr>` : ''}
      </table>
      ${dep ? `<button class="insp-btn" style="margin-top:12px" onclick="Inspector.action('restore','${this.escAttr(id)}',event)">Restore to canonical</button>` : ''}
    `);
  },

  async edit(id) {
    const row = this.items.find(m => m.id === id);
    const full = await API.fetch(`/api/memories/${encodeURIComponent(id)}/why`);
    const current = full.text || row?.text || '';
    const next = prompt('Edit memory text (saved in place — the old text is replaced):', current);
    if (next === null || next.trim() === '' || next.trim() === current.trim()) return;
    const res = await fetch(`/api/memories/${encodeURIComponent(id)}/text`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text: next.trim() }),
    });
    if (!res.ok) { const e = await res.json().catch(() => ({})); throw new Error(e.error || `HTTP ${res.status}`); }
    await this.load();
  },

  modal(html) {
    let m = document.getElementById('inspModal');
    if (!m) {
      m = document.createElement('div');
      m.id = 'inspModal';
      m.className = 'insp-modal';
      m.innerHTML = `<div class="insp-modal__backdrop" onclick="Inspector.close()"></div>
                     <div class="insp-modal__content"><button class="insp-close insp-close--abs" onclick="Inspector.close()">&times;</button><div id="inspModalBody"></div></div>`;
      document.body.appendChild(m);
    }
    document.getElementById('inspModalBody').innerHTML = html;
    m.classList.add('insp-modal--open');
  },

  close() {
    const m = document.getElementById('inspModal');
    if (m) m.classList.remove('insp-modal--open');
    const o = document.getElementById('inspectorOverlay');
    if (o) { o.classList.remove('insp-overlay--open'); o.setAttribute('aria-hidden', 'true'); }
  },
};

function openInspector() {
  const o = document.getElementById('inspectorOverlay');
  if (!o) return;
  o.classList.add('insp-overlay--open');
  o.setAttribute('aria-hidden', 'false');
  if (!window._inspectorInited) {
    Inspector.init('inspectorBody');
    window._inspectorInited = true;
  } else {
    Inspector.load();
  }
}

document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') Inspector.close();
});
Inspector.close(); // bind close to the overlay × button via JS too
(function () {
  const c = document.getElementById('inspectorCloseBtn');
  if (c) c.addEventListener('click', () => Inspector.close());
})();