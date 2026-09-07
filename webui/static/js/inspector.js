/* nexus-memory Web UI — Memory Inspector (Astra-Punkt 3) */
/* WAS (Liste+Filter) · WARUM (/why) · KORRIGIEREN (edit) · ZURÜCKROLLEN (soft) */

const Inspector = {
  items: [],
  filter: '',
  open: false,

  async init(containerId) {
    this.el = document.getElementById(containerId);
    if (!this.el) return;
    this.render();
    await this.load();
  },

  async load() {
    try {
      const data = await API.getMemories({ limit: 2000 });
      this.items = (data.memories || []).slice().sort(
        (a, b) => new Date(b.created_at) - new Date(a.created_at));
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
        <input id="inspSearch" class="graph-controls__input" placeholder="Filter by text, source, category…"
               value="${this.filter.replace(/"/g, '&quot;')}" autocomplete="off">
        <span class="insp-count">${list.length} of ${this.items.length} memories</span>
        <button class="btn btn--secondary btn--sm" id="inspReload" title="Reload from Qdrant">↻</button>
      </div>
      <div class="insp-list" id="inspList">
        ${list.slice(0, 200).map(m => `
          <div class="insp-row" data-id="${m.id}">
            <span class="insp-row__cat insp-row__cat--${m.category}">${m.category}</span>
            <span class="insp-row__text">${this.esc((m.text || '').slice(0, 140))}</span>
            <span class="insp-row__meta">${m.source || 'unknown'} · ${(m.confidence * 100).toFixed(0)}%</span>
            <button class="insp-row__btn" data-act="why" title="Why this memory?">?</button>
            <button class="insp-row__btn" data-act="edit" title="Edit text">✎</button>
            <button class="insp-row__btn insp-row__btn--danger" data-act="dep" title="Soft-delete (recoverable)">🗑</button>
          </div>`).join('')}
        ${list.length > 200 ? `<div class="insp-more">…and ${list.length - 200} more (use the filter)</div>` : ''}
      </div>`;

    // Events
    document.getElementById('inspSearch').addEventListener('input', (e) => {
      clearTimeout(this._t);
      this._t = setTimeout(() => { this.filter = e.target.value.trim(); this.render(); this.restoreFocus(); }, 180);
    });
    document.getElementById('inspReload').addEventListener('click', () => this.load());
    this.el.querySelectorAll('.insp-row__btn').forEach(btn => {
      btn.addEventListener('click', (e) => this.action(btn.dataset.act, btn.closest('.insp-row').dataset.id, e));
    });
  },

  restoreFocus() {
    const s = document.getElementById('inspSearch');
    if (s) { s.focus(); s.setSelectionRange(s.value.length, s.value.length); }
  },

  esc(t) { const d = document.createElement('div'); d.textContent = t || ''; return d.innerHTML; },

  async action(act, id, ev) {
    if (act_guard === ev.timeStamp) return; act_guard = ev.timeStamp;
    if (ev) ev.stopPropagation();
    try {
      if (act === 'why') return await this.showWhy(id);
      if (act === 'edit') return await this.edit(id);
      if (act === 'dep') {
        if (!confirm('Soft-delete this memory? It is marked deprecated and can be restored anytime (nothing is erased).')) return;
        await this.post(`/api/memories/${id}/deprecate`);
        alert('Marked deprecated. Restore is one click away in the WHY view.');
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
      <h3>Why this memory</h3>
      <div class="insp-why__text">${this.esc(data.text)}</div>
      <table class="insp-why__table">
        <tr><td>Category</td><td>${data.category || '—'}</td></tr>
        <tr><td>Access level</td><td>${data.access_level || '—'}</td></tr>
        <tr><td>Area (scope)</td><td>${data.scope || 'default'}</td></tr>
        <tr><td>Lifecycle</td><td>${data.lifecycle_status || 'canonical (legacy)'}</td></tr>
        <tr><td>Source</td><td>${data.source || '—'} ${data.source_url ? `· <a href="${data.source_url}" target="_blank" rel="noopener">link</a>` : ''}</td></tr>
        <tr><td>Confidence</td><td>${data.confidence != null ? (data.confidence * 100).toFixed(0) + '%' : '—'}</td></tr>
        <tr><td>Created</td><td>${data.created_at || '—'}</td></tr>
        <tr><td>Agent</td><td>${data.agent || '—'}</td></tr>
        ${data.superseded ? `<tr><td>Superseded</td><td>${JSON.stringify(data.superseded).slice(0, 80)}</td></tr>` : ''}
      </table>
      ${dep ? `<button class="btn btn--secondary btn--sm" onclick="Inspector.action('restore','${id}',event)">Restore to canonical</button>` : ''}
    `);
  },

  async edit(id) {
    const row = this.items.find(m => m.id === id);
    if (!row) return;
    const full = await API.fetch(`/api/memories/${encodeURIComponent(id)}/why`);
    const current = full.text || row.text || '';
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
                     <div class="insp-modal__content"><button class="detail-panel__close" onclick="Inspector.close()">&times;</button><div id="inspModalBody"></div></div>`;
      document.body.appendChild(m);
    }
    document.getElementById('inspModalBody').innerHTML = html;
    m.classList.add('insp-modal--open');
  },

  close() { const m = document.getElementById('inspModal'); if (m) m.classList.remove('insp-modal--open'); },
};

let act_guard = 0;