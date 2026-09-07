/* nexus-memory Dashboard — Memory Inspector v2 (echte Designsprache) */
/* Eigenständig: keine Abhängigkeit vom API-Global (dashboard.js überschreibt const API='') */

const Inspector = {
  items: [], filter: '', cat: 'all', view: 'list', current: null,

  async _get(url) {
    const res = await fetch(url);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return res.json();
  },

  async open() {
    document.getElementById('inspectorOverlay').classList.add('insp-overlay--open');
    document.body.style.overflow = 'hidden';
    await this.load();
  },

  close() {
    document.getElementById('inspectorOverlay').classList.remove('insp-overlay--open');
    document.body.style.overflow = '';
  },

  async load() {
    const body = document.getElementById('inspectorBody');
    try {
      body.innerHTML = this._toolbarHTML() + '<div class="insp-error" style="display:none"></div><div id="inspList"></div>';
      const res = await fetch('/api/memories?limit=2000');
      if (!res.ok) throw new Error('HTTP ' + res.status);
      const data = await res.json();
      this.items = (data.memories || []).map(m => ({ id: m.id, text: (m.text || m.title || ''), category: m.category, access_level: m.access_level, created_at: m.created_at }));
      this._renderList();
      document.getElementById('inspSearch').addEventListener('input', (e) => { this.filter = e.target.value.trim(); this._renderList(); });
      document.getElementById('inspCat').addEventListener('change', (e) => { this.cat = e.target.value; this._renderList(); });
    } catch (err) {
      body.innerHTML = `<div class="insp-error">Inspector load failed: ${this._esc(err.message || String(err))}</div>`;
    }
  },

  _toolbarHTML() {
    return `
      <div class="insp-toolbar">
        <input class="insp-input" id="inspSearch" placeholder="Search memories…" autocomplete="off">
        <select class="insp-select" id="inspCat">
          <option value="all">All</option>
          <option value="fact">Fact</option><option value="belief">Belief</option>
          <option value="session">Session</option><option value="rule">Rule</option>
          <option value="preference">Preference</option><option value="temp">Temp</option>
        </select>
      </div>`;
  },

  _renderList() {
    const el = document.getElementById('inspList');
    if (!el) return;
    const f = this.filter.toLowerCase();
    const rows = this.items
      .filter(m => (this.cat === 'all' || m.category === this.cat) &&
                   (!f || (m.text || '').toLowerCase().includes(f)))
      .slice(0, 300);
    el.innerHTML = rows.map(m => `
      <div class="insp-row" onclick="Inspector._detail('${this._esc(m.id)}')">
        <span class="insp-badge insp-badge--${this._esc(m.category || 'fact')}">${this._esc(m.category || 'fact')}</span>
        <span class="insp-row__text">${this._esc((m.text || '').slice(0, 220))}</span>
        <span style="font-size:11px;color:#636e72;flex:0 0 auto">${this._esc(m.access_level || '')}</span>
      </div>`).join('') || '<div class="insp-error">No memories match.</div>';
  },

  _detail(id) { this.why(id); },

  async why(id) {
    const el = document.getElementById('inspList');
    try {
      const res = await fetch(`/api/memories/${encodeURIComponent(id)}/why`);
      if (!res.ok) throw new Error('HTTP ' + res.status);
      const d = await res.json();
      el.innerHTML = `
        <button class="insp-back" onclick="Inspector.load()">← Back to list</button>
        <div class="insp-why" style="margin-top:12px">
          <div style="white-space:pre-wrap;font-size:14px;color:#e8ecf4">${this._esc(d.text || '')}</div>
          <dl class="insp-why-grid">
            <dt>Category</dt><dd>${this._esc(d.category || '—')}</dd>
            <dt>Access</dt><dd>${this._esc(d.access_level || '—')}</dd>
            <dt>Area (scope)</dt><dd>${this._esc(d.scope || 'default')}</dd>
            <dt>Lifecycle</dt><dd>${this._esc(d.lifecycle_status || 'canonical')}</dd>
            <dt>Source</dt><dd>${this._esc(d.source || '—')}</dd>
            <dt>Created</dt><dd>${this._esc(d.created_at || '—')}</dd>
            <dt>Memory ID</dt><dd>${this._esc(d.id)}</dd>
          </dl>
          <div class="insp-actions">
            <button class="insp-btn" onclick="Inspector.edit('${this._esc(d.id)}')">✎ Correct text</button>
            ${d.lifecycle_status === 'deprecated'
              ? `<button class="insp-btn" onclick="Inspector.restore('${this._esc(d.id)}')">↩ Restore</button>`
              : `<button class="insp-btn insp-btn--danger" onclick="Inspector.deprecate('${this._esc(d.id)}')">🗑 Roll back (soft)</button>`}
          </div>
          <div id="inspMsg" style="margin-top:10px;font-size:12.5px;color:#00b894"></div>
        </div>`;
      this.current = id;
    } catch (err) {
      el.innerHTML = `<div class="insp-error">Load failed: ${this._esc(String(err))}</div>`;
    }
  },

  async edit(id) {
    const el = document.getElementById('inspList');
    try {
      const res = await fetch(`/api/memories/${encodeURIComponent(id)}/why`);
      const d = await res.json();
      el.innerHTML = `
        <button class="insp-back" onclick="Inspector.why('${this._esc(id)}')">← Back</button>
        <textarea class="insp-textarea" id="inspTextarea">${this._esc(d.text || '')}</textarea>
        <div class="insp-actions">
          <button class="insp-btn" onclick="Inspector.save('${this._esc(id)}')">Save</button>
          <button class="insp-btn" onclick="Inspector.why('${this._esc(id)}')">Cancel</button>
        </div>`;
    } catch (err) { el.innerHTML = `<div class="insp-error">${this._esc(String(err))}</div>`; }
  },

  async save(id) {
    const text = document.getElementById('inspTextarea').value.trim();
    const msg = document.getElementById('inspMsg');
    try {
      const res = await fetch(`/api/memories/${encodeURIComponent(id)}/text`, {
        method: 'PATCH', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text }),
      });
      if (!res.ok) throw new Error('HTTP ' + res.status);
      if (msg) msg.textContent = 'Saved ✓';
      this.load();
    } catch (err) {
      if (msg) { msg.style.color = '#e74c3c'; msg.textContent = 'Save failed: ' + err; }
    }
  },

  async deprecate(id) {
    await fetch(`/api/memories/${encodeURIComponent(id)}/deprecate`, { method: 'POST' });
    this.why(id);
  },

  async restore(id) {
    await fetch(`/api/memories/${encodeURIComponent(id)}/restore`, { method: 'POST' });
    this.why(id);
  },

  _esc(s) { return String(s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;'); },
};

function openInspector() { Inspector.open(); }
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') Inspector.close();
});
