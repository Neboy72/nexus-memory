// Nexus Memory Dashboard - Frontend Logic
// Brand colors: Purple #954EFF | Cyan #02C1FF | Dark #050810

const API = '';

// Agent logo mapping (id → filename in /assets/agent-logos/)
const AGENT_LOGOS = {
  'hermes': '/assets/agent-logos/hermes.png',
  'kilo-code': '/assets/agent-logos/kilo-code.png',
  'openclaw': '/assets/agent-logos/openclaw.png',
  'claude-code': '/assets/agent-logos/claude-code.png',
  'pi': '/assets/agent-logos/pi.svg',
  'cline': '/assets/agent-logos/cline.png',
  'codex': '/assets/agent-logos/codex.png',
  'openhands': '/assets/agent-logos/openhands.png',
  'roo-code': '/assets/agent-logos/roo-code.png',
  'qwen-code': '/assets/agent-logos/qwen-code.png',
  'cursor': '/assets/agent-logos/cursor.png',
  'antigravity-cli': '/assets/agent-logos/antigravity-cli.png',
  'opencode': '/assets/agent-logos/opencode.png',
  'windsurf': '/assets/agent-logos/windsurf.png',
  'crush': '/assets/agent-logos/crush.png',
};

function getAgentLogo(agentId, fallbackIcon) {
  const logo = AGENT_LOGOS[agentId];
  if (logo) {
    return `<img src="${logo}" class="agent-logo" alt="${agentId}" onerror="this.style.display='none'; this.nextElementSibling.style.display='inline';"><span class="agent-icon" style="display:none;">${fallbackIcon}</span>`;
  }
  return `<span class="agent-icon">${fallbackIcon}</span>`;
}

function getInstallBadge(installType) {
  if (!installType) return '';
  const cls = installType.replace('+', '-').replace('_', '-');
  const label = installType.replace('+', ' + ').toUpperCase();
  return `<span class="agent-install-badge ${cls}">${label}</span>`;
}

// ── API Helpers ──────────────────────────────────────────────

async function fetchAPI(endpoint, options = {}) {
  // W30-3: every dashboard call carries the local-mutation guard header. The
  // backend rejects POST/PATCH/DELETE without it — a cross-site page cannot
  // set a custom header without a CORS preflight, so this blocks CSRF.
  const opts = {
    ...options,
    headers: { 'X-Nexus-Dashboard': '1', ...(options.headers || {}) },
  };
  try {
    const resp = await fetch(endpoint, opts);
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || 'API Error');
    return data;
  } catch (err) {
    showToast(err.message, 'error');
    return null;
  }
}

function showToast(msg, type = '') {
  const toast = document.getElementById('toast');
  toast.textContent = msg;
  toast.className = 'toast show ' + type;
  setTimeout(() => toast.classList.remove('show'), 3000);
}

// ── Status ───────────────────────────────────────────────────

async function loadStatus() {
  const status = await fetchAPI('/api/status');
  if (!status) return;

  document.getElementById('version').textContent = `v${status.version}`;
  document.getElementById('stat-memories').textContent = status.points_count?.toLocaleString() || '0';
  // Der Wert ist der ECHTE Vektorraum der Sammlung ("qwen_qwen3-embedding-0_6b").
  // Die API liefert ihn roh; lesbar gemacht wird nur die Anzeige.
  const providerEl = document.getElementById('stat-provider');
  if (providerEl) {
    providerEl.textContent = prettyEmbedName(status.embedding_provider);
    providerEl.title = status.embedding_provider || 'unknown';
    fitOneLine(providerEl);
  }

  // Fuel-Zeile: eine Zeile, alles Wichtige drin (Modell · verbraucht von Cap)
  const fuelEl = document.getElementById('stat-fuel');
  if (fuelEl && status.fuel) {
    const f = status.fuel;
    // Zeile 1: Modellname (wächst — Auto-Fit per Länge), Zeile 2: Kosten (kurz, stabil)
    const nameEl = document.getElementById('stat-fuel-model');
    const costEl = document.getElementById('stat-fuel-cost');
    if (nameEl && costEl) {
      // Die Kachel darf nur nennen, was wirklich offen ist: ohne Station KEINEN
      // Modellnamen (vorher stand hier "glm-5.3-flash:cloud" auch dann, wenn der
      // User weder Ollama noch einen Schlüssel hatte — ein Name, der nicht stimmt).
      if (!f.enabled) {
        nameEl.textContent = 'OFF';
        fuelEl.title = 'Memory AI sorting is off — memories stay unsorted.';
      } else if (!f.provider) {
        nameEl.textContent = 'keine Station';
        fuelEl.title = 'Keine Station offen — Erinnerungen bleiben unsortiert.';
      } else {
        nameEl.textContent = prettyFuelModel(f.model);
        // "geprüft" nur, wenn der Name wirklich erreichbar ist; ein blosser
        // Schlüssel ist eine Erwartung, keine Gewissheit.
        const wie = f.verified ? 'geprüft' : 'Schlüssel vorhanden, ungeprüft';
        fuelEl.title = `Station: ${f.provider} (${wie}) · Modell: ${f.model}`;
      }
      costEl.textContent = f.enabled ? `$${f.spent_usd.toFixed(2)} / $${f.budget_usd.toFixed(2)}` : '—';
      const toggleEl = document.getElementById('fuel-toggle');
      if (toggleEl) {
        toggleEl.checked = f.enabled;
        toggleEl.disabled = false;
      }
      // Einzeilig halten: statt Umbruch wird die Schrift kleiner (fitOneLine).
      fitOneLine(nameEl, 20, 11);
    }
  }

  const healthDot = document.getElementById('health-indicator');
  const healthText = document.getElementById('health-text');
  // Zwei verschiedene Fehler, zwei verschiedene Namen. Vorher hiess beides
  // "Qdrant Offline" — auch dann, wenn nur die Sammlung fehlte und Qdrant lief.
  const storeStates = {
    ok: ['health-dot health-ok', 'Healthy'],
    collection_missing: ['health-dot health-error', 'Collection missing'],
    qdrant_unreachable: ['health-dot health-error', 'Qdrant Offline'],
  };
  const state = storeStates[status.store_state]
    || (status.qdrant_healthy ? storeStates.ok : ['health-dot health-error', 'Store not ready']);
  healthDot.className = state[0];
  healthText.textContent = state[1];
}

// ── Fuel-Toggle: KI-Sortierung an/aus ──
document.getElementById('fuel-toggle')?.addEventListener('change', async (e) => {
  const enabled = e.target.checked;
  e.target.disabled = true;
  try {
    const res = await fetch('/api/fuel/paid', {
      method: 'POST',
      headers: {'Content-Type': 'application/json', 'X-Nexus-Dashboard': '1'},  // W30-3
      body: JSON.stringify({enabled})
    });
    const d = await res.json();
    if (!d.ok) throw new Error(d.error || 'failed');
    if (d.message) showToast(d.message, enabled ? 'ok' : 'warn');
    loadStatus();
  } catch (err) {
    e.target.checked = !enabled;  // zurückrollen bei Fehler
    showToast('Toggle failed: ' + err.message, 'error');
    e.target.disabled = false;
  }
});

// ── Recall ordering toggle ──
async function loadRecallOrdering() {
  const data = await fetchAPI('/api/settings/rerank');
  if (!data) return;
  const modelEl = document.getElementById('stat-recall-model');
  const toggle = document.getElementById('recall-toggle');
  const card = document.getElementById('recall-card');
  const hint = document.getElementById('stat-recall-hint');
  if (modelEl) {
    modelEl.textContent = data.llm_model || 'unknown';
    // Mono data is 11.5-13px (DESIGN.md); fit below the CSS size, never above.
    fitOneLine(modelEl, 13, 11);
  }
  if (toggle) {
    toggle.checked = data.enabled;
    toggle.disabled = false;
  }
  if (card) {
    card.classList.toggle('stat-card--warning', !data.llm_configured);
  }
  if (hint) {
    hint.textContent = data.llm_configured
      ? 'Reordering uses tokens of your own model — one call per search.'
      : 'Model not configured — old order stays, no calls made.';
  }
}

document.getElementById('recall-toggle')?.addEventListener('change', async (e) => {
  const enabled = e.target.checked;
  e.target.disabled = true;
  try {
    const res = await fetch('/api/settings/rerank', {
      method: 'POST',
      headers: {'Content-Type': 'application/json', 'X-Nexus-Dashboard': '1'},  // W30-3
      body: JSON.stringify({enabled})
    });
    const d = await res.json();
    if (!res.ok) throw new Error(d.detail || d.error || 'failed');
    showToast(`Recall ordering ${enabled ? 'enabled' : 'disabled'}`, enabled ? 'success' : 'warn');
    loadRecallOrdering();
  } catch (err) {
    e.target.checked = !enabled;
    showToast('Recall toggle failed: ' + err.message, 'error');
    e.target.disabled = false;
  }
});

// ── Agents ───────────────────────────────────────────────────

async function loadAgents() {
  const [registry, detection] = await Promise.all([
    fetchAPI('/api/agents'),
    fetchAPI('/api/detect')
  ]);

  if (!registry || !registry.agents) {
    document.getElementById('agents-list').innerHTML = '<div class="loading">No agents connected</div>';
    return;
  }

  document.getElementById('stat-agents').textContent = registry.agents.length;

  // Connected agents
  const connectedIds = new Set(registry.agents.map(a => a.id));
  const list = document.getElementById('agents-list');
  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const hostBadge = agent => {
    const isLocal = (agent.host_type || 'local') === 'local';
    const cls = isLocal ? 'local' : 'remote';
    const tag = isLocal ? 'Local' : 'Remote';
    const label = esc(agent.host_label || (isLocal ? 'this machine' : 'unknown host'));
    const provider = agent.host_provider ? `<div class="tt-provider">Provider: ${esc(agent.host_provider)}</div>` : '';
    return `
      <span class="host-wrap">
        <span class="host-badge ${cls}">${tag}</span>
        <span class="host-tooltip">
          <div class="tt-title">${tag}</div>
          <div class="tt-label">Seat: ${label}</div>
          ${provider}
          <div class="tt-id">ID: ${esc(agent.id)}</div>
        </span>
      </span>`;
  };
  // Seat badge (Nebo design): shows the machine name as a pill next to
  // the Local/Remote badge — "Mac Mini" instead of a dash-in-title.
  const seatBadge = (agent, seat) => {
    if (!seat) return '';
    return `<span class="seat-badge" title="Machine: ${esc(seat)}">${esc(seat)}</span>`;
  };

  list.innerHTML = registry.agents.map(agent => {
    const levels = ['public', 'trusted', 'private'];
    const buttons = levels.map(level => {
      const active = agent.trust_level === level ? `active ${level}` : '';
      const label = level.charAt(0).toUpperCase() + level.slice(1);
      return `<button class="trust-btn ${active}" onclick="setTrust('${agent.id}', '${level}')">${label}</button>`;
    }).join('');

    const lastSeen = agent.last_seen ? timeAgo(agent.last_seen) : 'never';
    const reads = agent.reads || 0;
    const logo = getAgentLogo(agent.id, agent.icon);
    const badge = getInstallBadge(agent.install_type);
    // Disambiguation (Nebo, 07.09. v2): Sitzort als BADGE neben Local/Remote —
    // nicht im Titel (kein KI-Langbindestrich). Das Local/Remote-Badge zeigt
    // die Art, das Seat-Badge den Ort im Klartext ("Mac Mini").
    const seat = (agent.host_label || '').trim();
    // Nebo toggle: only pure-MCP agents get the On/Off switch — plugin
    // agents are wired into the host core and must not be clickable off.
    const isPureMcp = (agent.install_type || '') === 'mcp';
    const toggle = isPureMcp
      ? `<label class="ios-toggle" title="Nexus connection for ${esc(agent.name)}">
           <input type="checkbox" checked onchange="toggleAgent('${agent.id}', this)">
           <span class="ios-slider"></span>
         </label>`
      : '';

    return `
      <div class="agent-card">
        <div class="agent-header">
          ${logo}
          <span class="agent-name">${esc(agent.name)}</span>
          <span class="agent-install-badge">${badge}</span>
          ${hostBadge(agent)}
          ${seatBadge(agent, seat)}
          <span class="agent-status"></span>
          ${toggle}
        </div>
        <div class="agent-meta">Last seen: ${lastSeen} | ${reads} reads</div>
        <div class="trust-buttons">${buttons}</div>
      </div>
    `;
  }).join('');

  // Available but not connected agents
  if (detection && detection.detected_agents) {
    const available = detection.detected_agents.filter(a => !connectedIds.has(a.id));
    const availSection = document.getElementById('available-section');
    const availList = document.getElementById('available-list');

    if (available.length > 0) {
      availSection.style.display = 'block';
      availList.innerHTML = available.map(agent => {
        const logo = getAgentLogo(agent.id, agent.icon);
        const pluginLabel = agent.plugin_available ? 'Plugin+MCP' : 'MCP';
        return `
          <div class="available-card">
            ${logo}
            <span class="agent-name">${agent.name}</span>
            <span class="agent-install-badge mcp">${pluginLabel}</span>
            <button class="connect-btn" onclick="connectAgent('${agent.id}', event)">Connect</button>
          </div>
        `;
      }).join('');
    } else {
      availSection.style.display = 'none';
    }
  }
}

async function setTrust(agentId, level) {
  const result = await fetchAPI(`/api/agents/${agentId}/trust?level=${level}`, { method: 'POST' });
  if (result && !result.error) {
    showToast(`${agentId}: trust level → ${level}`, 'success');
    loadAgents();
  }
}

async function connectAgent(agentId, ev) {
  if (ev) ev.stopPropagation();
  const result = await fetchAPI(`/api/agents/${agentId}/connect`, { method: 'POST' });
  if (result && !result.error) {
    const msg = result.action === 'already-connected'
      ? `${agentId}: already connected`
      : `${agentId}: connected — Nexus registered in its config`;
    showToast(msg, 'success');
    loadAgents(); // agent moves to Connected once nexus_installed flips true
  } else {
    showToast(`${agentId}: connect failed — ${result?.error || 'unknown error'}`, 'error');
  }
}

async function toggleAgent(agentId, checkbox) {
  // Revert the visual state immediately; loadAgents() repaints after the
  // backend answers (or after the cancel), so the slider never lies.
  if (checkbox.checked) {
    // OFF → ON: connect right away (starts at Trusted, per Nebo semantics).
    const result = await fetchAPI(`/api/agents/${agentId}/connect`, { method: 'POST' });
    if (result && !result.error) {
      showToast(`${agentId}: connected — starts at Trusted`, 'success');
      loadAgents();
    } else {
      showToast(`${agentId}: connect failed — ${result?.error || 'unknown error'}`, 'error');
      checkbox.checked = false;
    }
    return;
  }
  // ON → OFF: confirm, then disconnect.
  const ok = confirm(`${agentId}: really disconnect from Nexus Memory?\nThe agent loses memory access at its next start.`);
  if (!ok) { checkbox.checked = true; return; }
  const result = await fetchAPI(`/api/agents/${agentId}/disconnect`, { method: 'POST' });
  if (result && !result.error) {
    showToast(`${agentId}: disconnected — moves back to Available`, 'success');
    loadAgents();
  } else {
    showToast(`${agentId}: disconnect failed — ${result?.error || 'unknown error'}`, 'error');
    checkbox.checked = true;
  }
}

// ── Memory Stats ─────────────────────────────────────────────

async function loadMemoryStats() {
  const stats = await fetchAPI('/api/memories/stats');
  if (!stats) return;

  // Drei Zeilen in fester Reihenfolge (Nebo-Regel: Public oben, dann Trusted, dann Private) — immer alle zeigen
  const lv = stats.by_access_level || {};
  const rows = [
    ['Public', lv.public || 0],
    ['Trusted', lv.trusted || 0],
    ['Private', lv.private || 0],
  ];
  const el = document.getElementById('stat-access');
  if (el) {
    el.innerHTML = rows.map(([k, v]) =>
      `<div class="access-row">${k}: ${v.toLocaleString('de-DE')}</div>`
    ).join('');
    // Auto-Fit: längere Zahlen → kleinere Schrift (zeilen bleiben immer lesbar, kein Umbruch)
    const maxLen = Math.max(...rows.map(([k, v]) => `${k}: ${v.toLocaleString('de-DE')}`.length));
    let as = 15;
    if (maxLen > 20) as = 13;
    if (maxLen > 26) as = 11;
    el.style.fontSize = as + 'px';
  }
}

// ── Graph: handled by app.js (from /graph page) ─────────────

// ── Actions ──────────────────────────────────────────────────

async function triggerBackup() {
  showToast('Creating backup...');
  const result = await fetchAPI('/api/backup', { method: 'POST' });
  if (result && result.status === 'ok') {
    showToast('Backup created', 'success');
  } else {
    showToast('Backup failed', 'error');
  }
}

async function checkHealth() {
  const health = await fetchAPI('/api/health');
  if (!health) return;

  const details = Object.entries(health)
    .filter(([k]) => k !== 'overall')
    .map(([k, v]) => `${k}: ${v.status}`)
    .join(' | ');
  showToast(`Health: ${health.overall} - ${details}`, health.overall === 'healthy' ? 'success' : 'error');
}

async function detectAgents() {
  const result = await fetchAPI('/api/detect');
  if (!result) return;

  const detected = result.detected_agents || [];
  const names = detected.map(a => `${a.icon} ${a.name}`).join(', ');
  showToast(`Detected: ${names}`);
  loadAgents(); // Refresh available list
}

async function refreshAll() {
  showToast('Refreshing...');
  await Promise.all([loadStatus(), loadAgents(), loadMemoryStats(), loadRecallOrdering()]);
  showToast('Refreshed', 'success');
}

// ── Utils ────────────────────────────────────────────────────

// Ein Vektorraum heisst "<provider>__<organisation>_<modell>__<dims>", z.B.
// "sentence-transformers__qwen_qwen3-embedding-0_6b__1024". Der Modell-Teil
// wiederholt die Organisation ("qwen" + "qwen3-...") und traegt die Groesse mit
// Unterstrichen. Anzeige: "Qwen3-Embedding-0.6B". Das Wort "Embedding" bleibt
// stehen — ohne es laese man den Chat-Modellnamen Qwen3-0.6B.
function prettyEmbedName(raw) {
  let s = String(raw == null ? '' : raw).trim();
  if (!s || s.toLowerCase() === 'unknown') return 'unknown';
  // Groesse lesbar: "…-embedding-0_6b" bzw. "…embedding_0_6b" -> "-0.6B"
  s = s.replace(/[_-](\d+)[_-](\d+)([bm])\b/gi, '-$1.$2$3');
  s = s.replace(/(\d)\.(\d+)([bm])\b/gi, (m, major, minor, unit) => `${major}.${minor}${unit.toUpperCase()}`);
  // Organisations-Praefix nur weg, wenn der Modellname ihn selbst wiederholt
  // ("qwen" + "qwen3-..." -> "qwen3-..."). Sonst stuende "Qwen" zweimal drin.
  const parts = s.split('_');
  if (parts.length > 1 && parts[1].toLowerCase().startsWith(parts[0].toLowerCase())) {
    s = parts.slice(1).join('_');
  }
  // Wortweise gross: "qwen3-embedding-0.6B" -> "Qwen3-Embedding-0.6B"
  return s.split('-').map(w => /^(bge|llm|api)$/i.test(w)
    ? w.toUpperCase()
    : w.charAt(0).toUpperCase() + w.slice(1)).join('-');
}

// Fuel-Modellname lesbar machen. ":cloud" ist ein Transport-Suffix, kein Teil
// des Modellnamens (es stand vorher im Namen und taeuschte einen Anbieter vor,
// den es nicht gab). Unbekannte Namen bleiben unveraendert — lieber roh als
// geraten.
function prettyFuelModel(raw) {
  return String(raw == null ? '' : raw).trim()
    .replace(':cloud', '')
    .replace('glm-5.3-flash', 'GLM-5.3-Flash')
    .replace('gpt-5.6-luna', 'GPT-5.6-Luna')
    .replace('bge-m3', 'BGE-M3')
    .replace('qwen3-embedding', 'Qwen3-Embedding');
}

// Einzeilig halten: der Name darf nie umbrechen (sonst steht ein einzelnes "b"
// in Zeile 2). Statt Umbruch wird die Schrift kleiner, bis der Text passt.
function fitOneLine(el, maxPx = 19, minPx = 11) {
  if (!el) return;
  let size = maxPx;
  el.style.fontSize = size + 'px';
  while (size > minPx && el.scrollWidth > el.clientWidth) {
    size -= 1;
    el.style.fontSize = size + 'px';
  }
}

function timeAgo(isoStr) {
  const date = new Date(isoStr);
  const diff = Date.now() - date.getTime();
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return 'just now';
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  return `${days}d ago`;
}

// ── Init ─────────────────────────────────────────────────────

async function init() {
  await Promise.all([loadStatus(), loadAgents(), loadMemoryStats(), loadRecallOrdering()]);
}

init();
// Auto-refresh every 30s
setInterval(() => { loadStatus(); loadAgents(); loadRecallOrdering(); }, 30000);

// Breitere/schmalere Kachel (Fenster, Tablet, Handy): Namen neu einpassen.
window.addEventListener('resize', () => {
  fitOneLine(document.getElementById('stat-provider'));
  // Auch der Fuel-Name muss nach einem Resize wieder einzeilig werden —
  // sonst bricht genau bei schmalem Fenster ein Zeichen in Zeile 2 um.
  fitOneLine(document.getElementById('stat-fuel-model'), 20, 11);
  fitOneLine(document.getElementById('stat-recall-model'), 13, 11);
});