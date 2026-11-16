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
  'deepseek-harness': '/assets/agent-logos/dsh.svg',
  'dsh': '/assets/agent-logos/dsh.svg',
  // Weitere Marken aus der Datenbank: bereit, sobald die Erkennung den Agenten kennt.
  'aider': '/assets/agent-logos/aider.png',
  'continue': '/assets/agent-logos/continue.png',
  'copilot': '/assets/agent-logos/copilot.png',
  'github-copilot': '/assets/agent-logos/github-copilot.svg',
  'warp': '/assets/agent-logos/warp.png',
  'zed': '/assets/agent-logos/zed.png',
  'amazon-q': '/assets/agent-logos/amazon.svg',
  'tabnine': '/assets/agent-logos/tabnine.png',
  'qodo': '/assets/agent-logos/qodo.png',
  'jetbrains': '/assets/agent-logos/jetbrains.svg',
  'jetbrains-junie': '/assets/agent-logos/jetbrains.svg',
  'n8n': '/assets/agent-logos/n8n.svg',
  'vercel': '/assets/agent-logos/vercel.svg',
  'salesforce': '/assets/agent-logos/salesforce.svg',
  'activepieces': '/assets/agent-logos/activepieces.svg',
  'windmill': '/assets/agent-logos/windmill.svg',
  'microsoft': '/assets/agent-logos/microsoft.svg',
  'lovable': '/assets/agent-logos/lovable.svg',
  'bolt': '/assets/agent-logos/bolt.svg',
  'crewai': '/assets/agent-logos/crewai.svg',
  'dify': '/assets/agent-logos/dify.svg',
  'hubspot': '/assets/agent-logos/hubspot.svg',
  'huggingface': '/assets/agent-logos/huggingface.svg',
  'langchain': '/assets/agent-logos/langchain.svg',
  'replit': '/assets/agent-logos/replit.svg',
};

function getAgentLogo(agentOrId, fallbackIcon) {
  // Look up under the current id, then any earlier id, then the label. An agent that was renamed
  // keeps its logo without anyone having to update this table (see agents.json "formerly").
  const agent = typeof agentOrId === 'string' ? { id: agentOrId } : (agentOrId || {});
  const candidates = [
    agent.id,
    agent.type,
    ...(Array.isArray(agent.formerly) ? agent.formerly : []),
    agent.name,
    String(agent.name || '').toLowerCase(),
  ];
  const key = candidates.find((k) => k && AGENT_LOGOS[k]);
  const logo = key ? AGENT_LOGOS[key] : null;
  if (logo) {
    return `<img src="${logo}" class="agent-logo" alt="${agent.id || ''}" onerror="this.style.display='none'; this.nextElementSibling.style.display='inline-flex';">${agentAvatar(agent, fallbackIcon, 'none')}`;
  }
  // Unknown agent: ask the server once (it fetches and stores the file), else show the avatar.
  const slug = String(agent.id || agent.name || '').toLowerCase().replace(/[^a-z0-9-]/g, '');
  if (slug) {
    return `<img src="/api/agent-logo/${encodeURIComponent(slug)}" class="agent-logo" alt="${agent.id || ''}" onerror="this.style.display='none'; this.nextElementSibling.style.display='inline-flex';">${agentAvatar(agent, fallbackIcon, 'none')}`;
  }
  return agentAvatar(agent, fallbackIcon, 'inline-flex');
}

// Universal avatar: first letter on a colour derived from the name, so every agent has a face
// even without artwork. The same name always gets the same colour.
function agentAvatar(agent, fallbackIcon, display) {
  const name = String((agent && (agent.name || agent.id)) || '?').trim();
  const letter = (name[0] || '?').toUpperCase();
  let h = 0;
  for (let i = 0; i < name.length; i++) h = (h * 31 + name.charCodeAt(i)) % 360;
  return `<span class="agent-avatar" style="display:${display};background:hsl(${h} 55% 32%);color:#F5F0FF" title="${name}">${letter}</span>`;
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
    const provider = embedDisplayName(status.embedding_provider);
    providerEl.textContent = provider.shown;
    // Das Label der Karte nennt das Wort schon. Damit trotzdem nichts verloren
    // geht, steht der volle Name im Tooltip UND als data-full im Markup.
    providerEl.title = provider.full;
    providerEl.dataset.full = provider.full;
  }

  // Fuel-Zeile: eine Zeile, alles Wichtige drin (Modell · verbraucht von Cap)
  const fuelEl = document.getElementById('stat-fuel');
  if (fuelEl && status.fuel) {
    const f = status.fuel;
    // Zeile 1: Modellname (feste 13px), Zeile 2: Kosten (kurz, stabil)
    const nameEl = document.getElementById('stat-fuel-model');
    const costEl = document.getElementById('stat-fuel-cost');
    if (nameEl && costEl) {
      // Die Kachel darf nur nennen, was wirklich offen ist: ohne Station KEINEN
      // Modellnamen (vorher stand hier "glm-5.3-flash:cloud" auch dann, wenn der
      // User weder Ollama noch einen Schlüssel hatte — ein Name, der nicht stimmt).
      if (!f.enabled) {
        nameEl.textContent = 'OFF';
        nameEl.removeAttribute('title');
        delete nameEl.dataset.full;
        fuelEl.title = 'Memory AI sorting is off — memories stay unsorted.';
      } else if (!f.provider) {
        nameEl.textContent = 'no station';
        nameEl.removeAttribute('title');
        delete nameEl.dataset.full;
        fuelEl.title = 'No station available — memories stay unsorted.';
      } else {
        // Nur der Modellname; die Kennung hinter dem letzten ":" bleibt im
        // Tooltip und als data-full erhalten.
        const model = modelName(f.model);
        nameEl.textContent = prettyFuelModel(model.shown);
        nameEl.title = model.full;
        nameEl.dataset.full = model.full;
        // "geprüft" nur, wenn der Name wirklich erreichbar ist; ein blosser
        // Schlüssel ist eine Erwartung, keine Gewissheit.
        const state = f.verified ? 'verified' : 'key present, unverified';
        fuelEl.title = `Station: ${f.provider} (${state}) · Model: ${model.full}`;
      }
      costEl.textContent = f.enabled ? `$${f.spent_usd.toFixed(2)} / $${f.budget_usd.toFixed(2)}` : '—';
      // Beschreibungszeile unter dem Schalter — Betrag aus derselben Quelle wie
      // die Kostenanzeige darüber (NEXUS_FUEL_BUDGET_USD), nicht hartkodiert.
      const hintEl = document.getElementById('stat-fuel-hint');
      if (hintEl) hintEl.textContent = `Sorts old memories with your own AI — hard $${f.budget_usd.toFixed(2)}/month cap.`;
      const toggleEl = document.getElementById('fuel-toggle');
      if (toggleEl) {
        toggleEl.checked = f.enabled;
        toggleEl.disabled = false;
      }
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
    // Fixed 13px from CSS (DESIGN.md: mono data 11.5-13px) — never shrunk.
    // Angezeigt wird nur der Modellname; die Hermes-Kennung bleibt in
    // title/data-full.
    const model = modelName(data.llm_model);
    modelEl.textContent = model.shown;
    modelEl.title = model.full;
    modelEl.dataset.full = model.full;
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

  // Connected (15.11.2026, Nebo): a registry entry only counts once the agent
  // has actually read or written — or when it holds a key of its own. An entry
  // with reads == 0, writes == 0 and no own key was never connected: it stays
  // in agents.json (nothing is deleted, the registry keeps it) but it is not
  // listed. The first read or write makes it appear, immediately.
  // `unidentified` is no agent at all — it is the id an access WITHOUT a
  // sender gets, so it is never shown.
  // A former name (`formerly` of another entry) is the same agent under its
  // old id; listing it would show one agent twice.
  const UNIDENTIFIED_AGENT_ID = 'unidentified';
  const formerNames = new Set(
    registry.agents.flatMap(a => Array.isArray(a.formerly) ? a.formerly : [])
  );
  const isConnectedAgent = agent => {
    const id = String(agent.id || '').trim().toLowerCase();
    if (!id || id === UNIDENTIFIED_AGENT_ID || formerNames.has(id)) return false;
    // A registered agent counts as connected when it has a real target
    // (config dir), a key, or any access at all. Without this, an agent that
    // was just connected would stay invisible until its first read.
    return (
      (agent.reads || 0) > 0 ||
      (agent.writes || 0) > 0 ||
      Boolean(agent.has_own_key) ||
      Boolean(String(agent.config_dir || '').trim())
    );
  };
  const connectedAgents = registry.agents.filter(isConnectedAgent);

  // The tile counts what the list shows — connected, not registered.
  document.getElementById('stat-agents').textContent = connectedAgents.length;

  // Connected agents
  // An agent counts as connected under its current id AND under any earlier one — otherwise a
  // renamed agent shows up twice, once as connected and once as "available" (see "formerly").
  const connectedIds = new Set(
    connectedAgents.flatMap(a => [a.id, ...(Array.isArray(a.formerly) ? a.formerly : [])])
  );
  // Locations already connected: a second installation of the same type in
  // another folder (profile) must still be offered.
  const connectedDirs = new Set(
    connectedAgents.map(a => (a.config_dir || '').trim()).filter(Boolean)
  );
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
  // ID-Badge (15.11.2026): die Kennung neben den anderen Badges, ganz rechts.
  // Dieselbe Klasse wie sie (keine Sonderfarbe) — sie ist eine Auskunft wie
  // "Local" und "Mac Mini", keine Warnung. Der Titel der Karte bleibt das
  // Label; die Kennung steht nur hier und im Tooltip.
  const idBadge = agent => {
    const id = String(agent.id || '').trim();
    if (!id) return '';
    // ``id-badge`` traegt nur die Ausrichtung (margin-left:auto): bricht die
    // Badge-Zeile um, bleibt die Kennung rechts in ihrer Zeile stehen.
    return `<span class="seat-badge id-badge" title="Unique ID of this agent instance">ID: ${esc(id)}</span>`;
  };

  list.innerHTML = connectedAgents.map(agent => {
    const levels = ['public', 'trusted', 'private'];
    const buttons = levels.map(level => {
      const active = agent.trust_level === level ? `active ${level}` : '';
      const label = level.charAt(0).toUpperCase() + level.slice(1);
      return `<button class="trust-btn ${active}" onclick="setTrust('${agent.id}', '${level}')">${label}</button>`;
    }).join('');

    // Rolle (15.11.2026): drei Stufen statt „darf alles". Was fehlt, ist Know —
    // dieselbe Vorgabe, die der Dienst fail-closed anwendet.
    const roles = ['admin', 'work', 'know'];
    const currentRole = roles.includes(agent.role) ? agent.role : 'know';
    const roleButtons = roles.map(level => {
      const active = currentRole === level ? `active ${level}` : '';
      const label = level.charAt(0).toUpperCase() + level.slice(1);
      return `<button class="trust-btn role-btn ${active}" title="${esc(ROLE_HINTS[level])}" onclick="setRole('${agent.id}', '${level}')">${label}</button>`;
    }).join('');

    const lastSeen = agent.last_seen ? timeAgo(agent.last_seen) : 'never';
    const reads = agent.reads || 0;
    const writes = agent.writes || 0;
    // Access counting started with commit 197c3a5 (24.10.2026): reads/writes are smaller
    // than the connection age for the first agents. A fresh install counts from day one.
    const nf = n => Number(n).toLocaleString('en-US');
    // Connected since: the registry stores the first connection date.
    const sinceDate = agent.connected_at
      ? new Date(agent.connected_at).toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' })
      : '';
    const sincePart = sinceDate ? ` · connected ${sinceDate}` : '';
    // Self-enrolment (15.11.2026): when the agent last enrolled and how often.
    const enrolledDate = agent.enrolled_at
      ? new Date(agent.enrolled_at).toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' })
      : '';
    const enrollCount = Number(agent.enroll_count || 0);
    const logo = getAgentLogo(agent, agent.icon);
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
          ${idBadge(agent)}
          <span class="agent-status"></span>
          ${toggle}
        </div>
        <div class="agent-meta">
          <span>Last access: ${lastSeen}</span>
          <span>${nf(reads)} reads</span>
          <span>${nf(writes)} writes</span>
          ${sinceDate ? `<span>connected since ${sinceDate}</span>` : ''}
          ${enrolledDate ? `<span title="self-enrolment via POST /enroll">enrolled ${enrolledDate}${enrollCount ? ` · ${nf(enrollCount)}×` : ''}</span>` : ''}
          <span>${agent.has_own_key ? 'own key' : 'no own key'}</span>
        </div>
        <div class="agent-controls">
          <div class="control-group">
            <span class="control-label">Access</span>
            <div class="trust-buttons">${buttons}</div>
          </div>
          <div class="control-group">
            <span class="control-label">Role</span>
            <div class="trust-buttons">${roleButtons}</div>
          </div>
        </div>
      </div>
    `;
  }).join('') || '<div class="loading">No agents connected</div>';

  // Available but not connected agents
  if (detection && detection.detected_agents) {
    const available = detection.detected_agents.filter(a => {
      if (connectedIds.has(a.id)) return false;
      // A connected agent hides only ITS OWN installation (same config dir),
      // never every installation of the same type.
      const dir = (a.config_dir || '').trim();
      if (dir && connectedDirs.has(dir)) return false;
      return true;
    });
    const availSection = document.getElementById('available-section');
    const availList = document.getElementById('available-list');

    if (available.length > 0) {
      availSection.style.display = 'block';
      availList.innerHTML = available.map(agent => {
        const logo = getAgentLogo(agent, agent.icon);
        const pluginLabel = agent.plugin_available ? 'Plugin+MCP' : 'MCP';
        return `
          <div class="available-card">
            ${logo}
            <span class="agent-name">${esc(agent.name)}</span>
            ${agent.profile ? `<span class="agent-install-badge">${esc(agent.profile)}</span>` : ''}
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

// One sentence per level — the tooltip on the Role field.
const ROLE_HINTS = {
  admin: 'Admin: everything Work may do, plus backups, restore, webhooks, guardrail overrides and cost reports.',
  work: 'Work: read and write, graph queries, and forget/update on other agents\' entries — but no administration.',
  know: 'Know: read and write, and forget/update only on entries this agent wrote itself.',
};

async function setRole(agentId, role) {
  // fetchAPI shows the error toast itself (and returns null), same as setTrust.
  const result = await fetchAPI(`/api/agents/${agentId}/role?role=${role}`, { method: 'POST' });
  if (result && !result.error) {
    showToast(`${agentId}: role → ${role}`, 'success');
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

// ── Service-Protokoll (Bewegungsprotokoll) ───────────────────
// Quelle ist GET /api/service-log: der Server liest dieselbe Tagesdatei wie
// das CLI (nexus_memory.service_log). Hier wird nichts nachgeladen und nichts
// ergaenzt — was nicht im Protokoll steht, steht auch nicht in der Liste.

const SVCLOG_LIMIT_START = 200;   // default of the route
const SVCLOG_LIMIT_MAX = 1000;    // upper bound of the route
const SVCLOG_ACTION_LABELS = {
  write: 'write', update: 'update', forget: 'revoke',
  denied: 'refused', error: 'error', backup: 'backup',
  restore: 'restore', role_change: 'role change',
  key_change: 'key change', guardrail_override: 'guardrail override',
};
// Reasons written before the switch to English. They stay in old log lines, so
// the display maps them instead of letting German words sit between English
// ones. New entries are written in English and pass through unchanged.
const SVCLOG_REASON_LEGACY = {
  'leerer Inhalt': 'empty content',
  'Etikett statt Aussage (kein Satz)': 'label instead of a statement (not a sentence)',
  'konsolidiert': 'consolidated',
  'SICA-Sitzung': 'SICA session',
  'Freigabe erteilt': 'override granted',
  'ungueltige Zugriffsstufe': 'invalid access level',
  'Eintrag nicht gefunden': 'entry not found',
  'Ausgang unklar': 'outcome unclear',
  'kein JSON-Koerper': 'no JSON body',
  'Koerper ist kein Objekt': 'body is not an object',
  'Inhalt ist keine Zeichenkette': 'content is not a string',
  'Kennung ist keine Zeichenkette': 'id is not a string',
};
// Filterliste wie im Auftrag. Weitere Aktionen stehen in der Tabelle mit
// ihrem Namen — sie fehlen dann nur im Menue, nicht in den Zeilen.
const SVCLOG_FILTER_ACTIONS = ['write', 'update', 'forget', 'denied', 'error', 'backup'];

const ServiceLog = {
  limit: SVCLOG_LIMIT_START,
  action: '',
  actor: '',
  actors: [],
  events: [],
  // Wie im Inspector: nur die jeweils letzte Anfrage darf rendern, sonst
  // ueberschreibt eine langsame aeltere Antwort die neuere Ansicht.
  _seq: 0,

  open() {
    const overlay = document.getElementById('serviceLogOverlay');
    if (!overlay) return;
    this.limit = SVCLOG_LIMIT_START;
    this.action = '';
    this.actor = '';
    overlay.classList.add('insp-overlay--open');
    document.body.style.overflow = 'hidden';
    this.load();
  },

  close() {
    const overlay = document.getElementById('serviceLogOverlay');
    if (overlay) overlay.classList.remove('insp-overlay--open');
    document.body.style.overflow = '';
  },

  setFilter(kind, value) {
    if (kind === 'action') this.action = value;
    if (kind === 'actor') this.actor = value;
    this.limit = SVCLOG_LIMIT_START;  // neuer Filter faengt wieder oben an
    this.load();
  },

  older() {
    this.limit = Math.min(this.limit * 2, SVCLOG_LIMIT_MAX);
    this.load();
  },

  async load() {
    const body = document.getElementById('serviceLogBody');
    if (!body) return;
    const token = ++this._seq;
    const params = new URLSearchParams({ limit: String(this.limit) });
    if (this.action) params.set('action', this.action);
    if (this.actor) params.set('actor', this.actor);
    try {
      const res = await fetch(`/api/service-log?${params}`);
      if (!res.ok) throw new Error('HTTP ' + res.status);
      const data = await res.json();
      if (token !== this._seq) return;
      this.events = data.events || [];
      // Absender aus den geladenen Zeilen; bereits bekannte bleiben waehlbar,
      // damit ein gesetzter Filter nicht aus dem Menue verschwindet.
      const seen = new Set(this.actors);
      this.events.forEach(e => { if (e.actor) seen.add(e.actor); });
      this.actors = [...seen].sort((a, b) => a.localeCompare(b));
      this._chainText(data.chain || {});
      body.innerHTML = this._filtersHTML() + this._tableHTML() + this._moreHTML();
    } catch (err) {
      if (token !== this._seq) return;
      body.innerHTML = `<div class="insp-error">Log not readable: ${this._esc(err.message || String(err))}</div>`;
      const hint = document.getElementById('svclogChain');
      if (hint) {
        hint.textContent = 'Chain not verifiable';
        hint.className = 'insp-overlay__hint svclog-chain svclog-chain--warn';
      }
    }
  },

  // Ketten-Status als Text. Kein Tag, keine Datei: "Kein Protokoll für diesen
  // Tag" — die Kette ist dann nicht kaputt, sie ist nur nicht da.
  _chainText(chain) {
    const hint = document.getElementById('svclogChain');
    if (!hint) return;
    const base = 'insp-overlay__hint svclog-chain';
    let text = 'Chain not verifiable';
    let warn = true;
    if (chain.broken_at) {
      text = `Chain broken at line ${chain.broken_at}`;
    } else if (chain.ok) {
      text = `Chain verified, ${chain.lines} ${chain.lines === 1 ? 'entry' : 'entries'}`;
      warn = false;
    } else if (!chain.lines) {
      text = 'No log for this day';
      warn = false;
    }
    hint.textContent = text;
    hint.className = warn ? `${base} svclog-chain--warn` : base;
  },

  _filtersHTML() {
    const actions = SVCLOG_FILTER_ACTIONS.map(value =>
      `<option value="${value}"${this.action === value ? ' selected' : ''}>${SVCLOG_ACTION_LABELS[value]}</option>`
    ).join('');
    const actors = this.actors.map(name =>
      `<option value="${this._esc(name)}"${this.actor === name ? ' selected' : ''}>${this._esc(name)}</option>`
    ).join('');
    return `
      <div class="insp-toolbar">
        <select class="insp-select" onchange="ServiceLog.setFilter('action', this.value)">
          <option value=""${this.action ? '' : ' selected'}>Action: all</option>${actions}
        </select>
        <select class="insp-select" onchange="ServiceLog.setFilter('actor', this.value)">
          <option value=""${this.actor ? '' : ' selected'}>Actor: all</option>${actors}
        </select>
      </div>`;
  },

  // A reason is metadata, not content: it rides along as the topic's tooltip,
  // so the column layout stays as it is while old German wording is still
  // translated instead of standing between English rows.
  _reason(raw) {
    const text = String(raw == null ? '' : raw).trim();
    if (!text) return '';
    if (SVCLOG_REASON_LEGACY[text]) return SVCLOG_REASON_LEGACY[text];
    // Prefix forms written by older callers.
    const backup = text.match(/^Sicherung\s+(.*)$/);
    if (backup) return `backup ${backup[1]}`;
    const failed = text.match(/^(\w+)\s+fehlgeschlagen$/);
    if (failed) return `${failed[1]} failed`;
    const gate = text.match(/^Schreibtor nicht ladbar:\s*(.*)$/);
    if (gate) return `write gate not loadable: ${gate[1]}`;
    return text;
  },

  _tableHTML() {
    if (!this.events.length) {
      return '<div class="svclog-empty">No entries logged yet.</div>';
    }
    // Newest first — that is how the route already delivers them.
    const rows = this.events.map(event => {
      const action = SVCLOG_ACTION_LABELS[event.action] || event.action || '—';
      const reason = this._reason(event.reason);
      const topicTitle = reason ? ` title="${this._esc(reason)}"` : '';
      const topic = event.topic
        ? this._esc(event.topic)
        : '<span class="svclog-private">private</span>';
      const result = String(event.result || '');
      const resultCls = result === 'failed' ? ' svclog-result--failed'
        : result === 'refused' ? ' svclog-result--refused'
        : ' svclog-result--ok';
      // Kennung nur gekuerzt; der volle Wert steht im Tooltip.
      const id = event.target_id
        ? ` <code class="svclog-id" title="${this._esc(event.target_id)}">${this._esc(String(event.target_id).slice(0, 8))}</code>`
        : '';
      // Spalte "Actor": das Label von damals, darunter klein die Kennung.
      // Altbestand ohne ``actor_label`` zeigt nur die Kennung — wie bisher.
      const actor = this._esc(event.actor || 'unidentified');
      const actorLabel = String(event.actor_label || '').trim();
      const actorCell = actorLabel && this._esc(actorLabel) !== actor
        ? `<div>${this._esc(actorLabel)}</div><div class="svclog-actor-id">${actor}</div>`
        : actor;
      return `<tr>
        <td class="svclog-time">${this._clock(event.ts)}</td>
        <td>${actorCell}</td>
        <td>${this._esc(action)}</td>
        <td class="svclog-topic"${topicTitle}>${topic}</td>
        <td><span class="svclog-result${resultCls}">${this._esc(result || '—')}</span>${id}</td>
      </tr>`;
    }).join('');
    return `
      <div class="svclog-scroll">
        <table class="svclog-table">
          <thead>
            <tr><th>Time</th><th>Actor</th><th>Action</th><th>Topic</th><th>Result</th></tr>
          </thead>
          <tbody>${rows}</tbody>
        </table>
      </div>`;
  },

  _moreHTML() {
    // Nur anbieten, wenn die Antwort die angefragte Menge ausgeschoepft hat:
    // sonst gibt es nichts Aelteres und der Knopf laedt dieselben Zeilen neu.
    if (this.events.length < this.limit || this.limit >= SVCLOG_LIMIT_MAX) return '';
    return '<div class="insp-actions"><button class="insp-btn" onclick="ServiceLog.older()">Load older</button></div>';
  },

  _clock(ts) {
    const when = new Date(ts);
    if (isNaN(when.getTime())) return '—';
    const pad = (n) => String(n).padStart(2, '0');
    return `${pad(when.getHours())}:${pad(when.getMinutes())}:${pad(when.getSeconds())}`;
  },

  _esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  },
};

function openServiceLog() { ServiceLog.open(); }
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') ServiceLog.close();
});

// ── Utils ────────────────────────────────────────────────────

// Ein Vektorraum heisst "<provider>__<organisation>_<modell>__<dims>", z.B.
// "sentence-transformers__qwen_qwen3-embedding-0_6b__1024". Der Modell-Teil
// wiederholt die Organisation ("qwen" + "qwen3-...") und traegt die Groesse mit
// Unterstrichen. Anzeige: "Name-0.6B" (das Wort "Embedding" entfernt
// embedDisplayName, das Label der Karte sagt es bereits).
function prettyEmbedName(raw) {
  let s = String(raw == null ? '' : raw).trim();
  if (!s || s.toLowerCase() === 'unknown') return 'unknown';
  // Organisations-Praefix nur weg, wenn der Modellname ihn selbst wiederholt
  // ("qwen" + "qwen3-..." -> "qwen3-..."). Sonst stuende "Qwen" zweimal drin.
  const parts = s.split('_');
  if (parts.length > 1 && parts[1].toLowerCase().startsWith(parts[0].toLowerCase())) {
    s = parts.slice(1).join('_');
  }
  // Zusammengezogene Groesse: der Vektor-Name verliert Punkt und Bindestrich
  // ("0.6b" -> "0_6b" -> "0_6b" ohne Trenner: "name30_6b"). Steht vor der "0"
  // eine Ziffer, ist das die Hauptversion und die "0" der Punkt: -> "name3-0.6b".
  s = s.replace(/(\d)0[_-](\d+)([bm])$/i, '$1-0.$2$3');
  // Klassisch getrennte Groesse: "…-embedding-0_6b" bzw. "…embedding_0_6b" -> "-0.6b"
  s = s.replace(/[_-](\d+)[_-](\d+)([bm])\b/gi, '-$1.$2$3');
  // Einheit gross: "0.6b" -> "0.6B"
  s = s.replace(/(\d)\.(\d+)([bm])\b/gi, (m, major, minor, unit) => `${major}.${minor}${unit.toUpperCase()}`);
  // "bge-…" bleibt klein (so schreibt sich die Modellfamilie), sonst wortweise
  // gross: "name3-0.6B" -> "Name3-0.6B".
  const words = s.split('-');
  if (/^bge$/i.test(words[0])) {
    return s.toLowerCase().replace(/(\d)\.(\d+)([bm])\b/gi,
      (m, major, minor, unit) => `${major}.${minor}${unit.toUpperCase()}`);
  }
  return words.map(w => /^(llm|api)$/i.test(w)
    ? w.toUpperCase()
    : w.charAt(0).toUpperCase() + w.slice(1)).join('-');
}

// Das Label der Karte sagt bereits "Embedding Provider" — im Wert ist das Wort
// doppelt und kostet nur Platz. Entfernt wird nur das Wort selbst, nicht die
// Bedeutung: der volle Name bleibt in title/data-full erhalten.
function embedDisplayName(raw) {
  const full = String(raw == null ? '' : raw).trim() || 'unknown';
  const cleaned = prettyEmbedName(full)
    .replace(/embeddings?/gi, '')       // das Wort selbst (auch "Embeddings")
    .replace(/([-_\s])[-_\s]+/g, '$1')  // doppelte Trenner -> einer
    .replace(/^[-_\s]+|[-_\s]+$/g, ''); // Trenner an den Raendern
  // War der Wert nur "embedding", ist der Rest leer: lieber roh als leer.
  return { shown: cleaned || full, full };
}

// Hermes haengt eine Kennung an den Modellnamen ("provider-model:1017-cloud",  # guard-ok: neutrale Platzhalter, kein echter Modellname
// "provider-model:cloud"). Fuer die Anzeige zaehlt nur der Modellname; alles ab  # guard-ok: neutrale Platzhalter, kein echter Modellname
// dem letzten ":" faellt weg und bleibt in title und data-full erhalten. Die
// Regel gilt fuer jeden Modellnamen, nicht fuer eine bestimmte Endung.
function modelName(raw) {
  const full = String(raw == null ? '' : raw).trim() || 'unknown';
  const cut = full.lastIndexOf(':');
  const shown = cut > 0 ? full.slice(0, cut).trim() : full;
  return { shown: shown || full, full };
}

// Fuel-Modellname lesbar machen. Die Kennung hinter dem ":" hat modelName()
// bereits entfernt. Unbekannte Namen bleiben unveraendert — lieber roh als
// geraten.
function prettyFuelModel(raw) {
  return String(raw == null ? '' : raw).trim()
    .replace('glm-5.3-flash', 'GLM-5.3-Flash')
    .replace('gpt-5.6-luna', 'GPT-5.6-Luna')
    .replace('bge-m3', 'BGE-M3')
    .replace('qwen3-embedding', 'Qwen3-Embedding');
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