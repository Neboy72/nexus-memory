/* nexus-memory Dashboard — Memory Inspector v2 (echte Designsprache) */
/* Eigenständig: keine Abhängigkeit vom API-Global (dashboard.js überschreibt const API='') */

// Endpoints and caps live next to the module definition so the list and
// detail paths cannot drift apart.
const INSP_API = '/api/memories';
const INSP_FETCH_LIMIT = 2000;   // server-side fetch limit
const INSP_RENDER_CAP = 300;     // max rows rendered (subset of the fetch)
const INSP_TEXT_PREVIEW = 220;   // list-row text excerpt length

const Inspector = {
  items: [], filter: '', cat: 'all', view: 'list', current: null,

  // Monotonic request token: every load()/why() start bumps it, and a
  // response may only render if it is still the latest request. Without it a
  // slow earlier request would overwrite the newer view (out-of-order writes).
  _reqSeq: 0,

  async open() {
    const overlay = document.getElementById('inspectorOverlay');
    if (overlay) overlay.classList.add('insp-overlay--open');
    document.body.style.overflow = 'hidden';
    await this.load();
  },

  close() {
    const overlay = document.getElementById('inspectorOverlay');
    if (overlay) overlay.classList.remove('insp-overlay--open');
    document.body.style.overflow = '';
  },

  async load() {
    const body = document.getElementById('inspectorBody');
    const token = ++this._reqSeq;
    // The toolbar below is (re)rendered with its defaults (empty search,
    // "All"), so reset the state it reflects — otherwise _renderList() would
    // filter by a stale category/term no longer visible in the UI.
    this.filter = '';
    this.cat = 'all';
    try {
      body.innerHTML = this._toolbarHTML() + '<div class="insp-error" style="display:none"></div><div id="inspList"></div>';
      const res = await fetch(`${INSP_API}?limit=${INSP_FETCH_LIMIT}`);
      if (!res.ok) throw new Error('HTTP ' + res.status);
      const data = await res.json();
      if (token !== this._reqSeq) return;
      this.items = (data.memories || []).map(m => ({ id: m.id, text: (m.text || m.title || ''), category: m.category, access_level: m.access_level, created_at: m.created_at }));
      this._renderList();
      this.view = 'list';
      document.getElementById('inspSearch').addEventListener('input', (e) => { this.filter = e.target.value.trim(); this._renderList(); });
      document.getElementById('inspCat').addEventListener('change', (e) => { this.cat = e.target.value; this._renderList(); });
    } catch (err) {
      if (token !== this._reqSeq) return;
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
      .slice(0, INSP_RENDER_CAP);
    el.innerHTML = rows.map(m => `
      <div class="insp-row" data-mem-id="${this._esc(m.id)}">
        <span class="insp-badge insp-badge--${this._esc(m.category || 'fact')}">${this._esc(m.category || 'fact')}</span>
        <span class="insp-row__text">${this._esc((m.text || '').slice(0, INSP_TEXT_PREVIEW))}</span>
        <span style="font-size:11px;color:#636e72;flex:0 0 auto">${this._esc(m.access_level || '')}</span>
      </div>`).join('') || '<div class="insp-error">No memories match.</div>';
  },

  _detail(id) { this.why(id); },

  async why(id) {
    const el = document.getElementById('inspList');
    // Guard: why() can be invoked before load() rendered the list (or after
    // the body was replaced by an error) — a null el would otherwise throw
    // inside the catch block and mask the real error.
    if (!el) return;
    const token = ++this._reqSeq;
    try {
      const res = await fetch(`${INSP_API}/${encodeURIComponent(id)}/why`);
      if (!res.ok) throw new Error('HTTP ' + res.status);
      const d = await res.json();
      if (token !== this._reqSeq) return;
      el.innerHTML = `
        <button class="insp-back" data-insp-action="back">← Back to list</button>
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
            <button class="insp-btn" data-insp-action="edit" data-mem-id="${this._esc(d.id)}">✎ Correct text</button>
            ${d.lifecycle_status === 'deprecated'
              ? `<button class="insp-btn" data-insp-action="restore" data-mem-id="${this._esc(d.id)}">↩ Restore</button>`
              : `<button class="insp-btn insp-btn--danger" data-insp-action="deprecate" data-mem-id="${this._esc(d.id)}">🗑 Roll back (soft)</button>`}
          </div>
          <div id="inspMsg" style="margin-top:10px;font-size:12.5px;color:#00b894"></div>
        </div>`;
      this.current = id;
      this.view = 'why';
    } catch (err) {
      if (token !== this._reqSeq) return;
      el.innerHTML = `<div class="insp-error">Load failed: ${this._esc(String(err))}</div>`;
    }
  },

  async edit(id) {
    const el = document.getElementById('inspList');
    if (!el) return;
    // Same request-token protocol as load()/why(): a slow, stale edit
    // response must not repaint over a newer view.
    const token = ++this._reqSeq;
    try {
      const res = await fetch(`${INSP_API}/${encodeURIComponent(id)}/why`);
      if (!res.ok) throw new Error('HTTP ' + res.status);
      const d = await res.json();
      if (token !== this._reqSeq) return;
      this.view = 'edit';
      el.innerHTML = `
        <button class="insp-back" data-insp-action="back">← Back</button>
        <textarea class="insp-textarea" id="inspTextarea">${this._esc(d.text || '')}</textarea>
        <div class="insp-actions">
          <button class="insp-btn" data-insp-action="save" data-mem-id="${this._esc(id)}">Save</button>
          <button class="insp-btn" data-insp-action="back">Cancel</button>
        </div>
        <div id="inspMsg" style="margin-top:10px;font-size:12.5px;color:#00b894"></div>`;
    } catch (err) {
      if (token !== this._reqSeq) return;
      el.innerHTML = `<div class="insp-error">${this._esc(String(err))}</div>`;
    }
  },

  async save(id) {
    const text = document.getElementById('inspTextarea').value.trim();
    const msg = document.getElementById('inspMsg');
    try {
      const res = await fetch(`${INSP_API}/${encodeURIComponent(id)}/text`, {
        // W30-3: mutating routes require the dashboard guard header.
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json', 'X-Nexus-Dashboard': '1' },
        body: JSON.stringify({ text }),
      });
      if (!res.ok) throw new Error('HTTP ' + res.status);
      // Reload the list, then reopen the detail view so the confirmation is
      // actually visible (this.load() alone wipes the edit view's #inspMsg).
      await this.load();
      await this.why(id);
      const m = document.getElementById('inspMsg');
      if (m) m.textContent = 'Saved ✓';
    } catch (err) {
      if (msg) { msg.style.color = '#e74c3c'; msg.textContent = 'Save failed: ' + err; }
      else this._showError('Save failed: ' + err);
    }
  },

  async deprecate(id) {
    // W30-3: mutating routes require the dashboard guard header.
    const res = await fetch(`${INSP_API}/${encodeURIComponent(id)}/deprecate`, {
      method: 'POST', headers: { 'X-Nexus-Dashboard': '1' },
    });
    if (!res.ok) { this._showError('Roll back failed: HTTP ' + res.status); return; }
    this.why(id);
  },

  async restore(id) {
    // W30-3: mutating routes require the dashboard guard header.
    const res = await fetch(`${INSP_API}/${encodeURIComponent(id)}/restore`, {
      method: 'POST', headers: { 'X-Nexus-Dashboard': '1' },
    });
    if (!res.ok) { this._showError('Restore failed: HTTP ' + res.status); return; }
    this.why(id);
  },

  _showError(message) {
    const msg = document.getElementById('inspMsg');
    if (msg) { msg.style.color = '#e74c3c'; msg.textContent = message; return; }
    const el = document.getElementById('inspList');
    if (!el) return;
    const div = document.createElement('div');
    div.className = 'insp-error';
    div.textContent = message;
    el.prepend(div);
  },

  _esc(s) { return String(s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;'); },
};

function openInspector() { Inspector.open(); }
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') Inspector.close();
});

// Delegated click handling — replaces the former inline onclick handlers so
// that interpolated memory ids never live inside HTML attributes (an id like
// `');alert(document.cookie)//` escaped to &#39; is decoded back to ' by the
// HTML parser before JS compilation and would break out of the attribute).
document.addEventListener('click', (e) => {
  const row = e.target.closest('.insp-row');
  if (row) { Inspector._detail(row.getAttribute('data-mem-id')); return; }
  const btn = e.target.closest('[data-insp-action]');
  if (!btn) return;
  const action = btn.getAttribute('data-insp-action');
  const id = btn.getAttribute('data-mem-id') || Inspector.current;
  if (action === 'back') {
    // Back from the edit view returns to the detail view; back from the
    // detail view returns to the list.
    if (Inspector.view === 'edit') Inspector.why(Inspector.current);
    else Inspector.load();
  }
  else if (action === 'edit') Inspector.edit(id);
  else if (action === 'save') Inspector.save(id);
  else if (action === 'restore') Inspector.restore(id);
  else if (action === 'deprecate') Inspector.deprecate(id);
});
