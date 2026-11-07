/**
 * Regressionstest: Werkzeuge respektieren den Gruppenkontext (ClawHub-Audit 1.22.0)
 *
 * Befund: `hooks/recall.ts` und `hooks/capture.ts` kappten Gruppen-Turns
 * korrekt, die WERKZEUGE (`nexus_search`, `nexus_forget`, Graph-Werkzeuge)
 * lasen aber stur `cfg.accessLevel`. Ein Agent in einem Gruppenchat konnte
 * damit private Erinnerungen abrufen und löschen.
 *
 * Ein Werkzeug bekommt vom Host keinen Kontext (`AnyAgentTool.execute(
 * toolCallId, params, signal?, onUpdate?)` — auch `OpenClawPluginToolContext`
 * trägt keine groupId), deshalb setzt `before_prompt_build` ein Turn-Merkmal
 * (lib/turn-context.ts), das die Werkzeuge lesen.
 *
 * Gepinnt wird hier:
 *  a) Gruppen-Merkmal  → nexus_search fragt nur `public` ab (echter Qdrant-Body)
 *  b) kein Merkmal     → `public` (fail-closed)
 *  c) Merkmal > 120 s  → `public` (fail-closed, auch für einen DM-Turn)
 *  d) Direktchat       → `cfg.accessLevel` wie bisher
 *  e) nexus_forget     → Gruppen-Merkmal kappt Suche UND Direkt-Löschung
 *  f) Graph-Werkzeuge  → Gruppen-Merkmal kappt find_entities und graph_traverse
 *  g) der Recall-Hook  → setzt das Merkmal (auch in nicht-interaktiven Turns)
 *
 * Kein Qdrant, kein Netz: fetch ist gemockt, der Graph-Client ein Stub.
 */
import assert from "node:assert"
import { register } from "node:module"
import { mkdtempSync, rmSync } from "node:fs"
import { tmpdir } from "node:os"
import { join } from "node:path"

register("./_typebox-test-loader.mjs", import.meta.url)

// Sandbox VOR jedem Modul-Import: die Capture-Queue wird nie angefasst und die
// Agent-Registry (~/.nexus-memory/agents.json) bleibt unberührt.
const SANDBOX = mkdtempSync(join(tmpdir(), "nexus-turn-context-"))
process.env.NEXUS_CAPTURE_QUEUE_FILE = join(SANDBOX, "capture-retry-queue.jsonl")
process.env.NEXUS_AGENT_SELFTEST = "1"

const { setTurnContext, resetTurnContext, getTurnContext, toolAccessLevel, TURN_MARKER_TTL_MS } =
  await import("./lib/turn-context.ts")
const { registerSearchTool } = await import("./tools/search.ts")
const { registerForgetTool } = await import("./tools/forget.ts")
const { registerFindEntitiesTool, registerGraphTraverseTool } = await import(
  "./tools/graph_traverse.ts"
)
const { buildRecallHandler } = await import("./hooks/recall.ts")
const { QdrantClient } = await import("./lib/qdrant-client.ts")

let failed = 0
const t = (name, fn) =>
  Promise.resolve()
    .then(fn)
    .then(() => console.log("PASS ", name))
    .catch((e) => {
      failed++
      console.log("FAIL ", name, "—", e?.message ?? String(e))
    })

// ── fetch-Mock: Qdrant zeichnet auf, was WIRKLICH gesendet wird ─────────────

const searches = [] // access_level-Filter je /points/search-Aufruf
const realFetch = globalThis.fetch

globalThis.fetch = async (url, opts = {}) => {
  const u = typeof url === "string" ? url : String(url)
  if (u.includes("/points/search")) {
    let levels = "no-filter"
    try {
      const body = JSON.parse(opts.body ?? "{}")
      const any = body?.filter?.must?.[0]?.match?.any
      if (any !== undefined) levels = Array.isArray(any) ? any.join("|") : String(any)
      else if (body?.filter) levels = "filter-without-any"
    } catch {
      levels = "parse-error"
    }
    searches.push(levels)
    return { ok: true, status: 200, json: async () => ({ result: [] }), text: async () => "" }
  }
  if (u.includes("/collections/")) {
    // resolveLayout()/ensureCollection(): anonyme Vektoren, keine Punkte
    return { ok: true, status: 200, json: async () => ({ result: {} }), text: async () => "" }
  }
  throw new Error(`unerwarteter fetch im Test: ${u}`)
}

// ── Werkzeug-Bau ───────────────────────────────────────────────────────────

const realClient = () => new QdrantClient("http://localhost:6333", "nexus-turn-test", 8)

function searchTool(cfg = { accessLevel: "private" }) {
  let tool = null
  registerSearchTool(
    { registerTool: (tt) => { tool = tt } },
    { embed: async () => [0.1, 0.2, 0.3] },
    realClient(),
    cfg,
  )
  assert.ok(tool, "nexus_search muss registriert sein")
  return tool
}

/** Eine Suche ausführen und den access_level-Filter zurückgeben. */
async function searchLevel(prepare) {
  searches.length = 0
  resetTurnContext()
  prepare()
  const res = await searchTool().execute("t", { query: "irgendetwas" })
  assert.ok(!res.isError, `Suche fehlgeschlagen: ${JSON.stringify(res?.content)}`)
  assert.strictEqual(searches.length, 1, `genau eine Suche erwartet, waren ${searches.length}`)
  return searches[0]
}

const textOf = (res) => (res.content ?? []).map((c) => c.text).join("\n")

// ── a-d) nexus_search ──────────────────────────────────────────────────────

await t("a) Gruppen-Merkmal → nexus_search fragt nur 'public' ab", async () => {
  const levels = await searchLevel(() => setTurnContext(true, "group-session-1"))
  assert.strictEqual(levels, "public", `Gruppen-Suche muss auf public gekappt sein, war: ${levels}`)
})

await t("b) kein Merkmal → 'public' (fail-closed)", async () => {
  const levels = await searchLevel(() => {})
  assert.strictEqual(levels, "public", `ohne Merkmal muss public gelten, war: ${levels}`)
})

await t("c) Merkmal älter als 120 s → 'public' (fail-closed)", async () => {
  const levels = await searchLevel(() =>
    // DM-Merkmal, aber abgelaufen: ohne TTL würde hier cfg (=private) greifen.
    setTurnContext(false, "dm-session", Date.now() - TURN_MARKER_TTL_MS),
  )
  assert.strictEqual(levels, "public", `abgelaufenes Merkmal muss public ergeben, war: ${levels}`)
})

await t("d) Direktchat-Merkmal → cfg.accessLevel wie bisher (private = kein Filter)", async () => {
  const levels = await searchLevel(() => setTurnContext(false, "dm-session-2"))
  assert.strictEqual(levels, "no-filter", `DM mit cfg=private sieht alles, war: ${levels}`)
})

await t("d2) Direktchat-Merkmal + cfg=trusted → public|trusted", async () => {
  searches.length = 0
  resetTurnContext()
  setTurnContext(false)
  await searchTool({ accessLevel: "trusted" }).execute("t", { query: "irgendetwas" })
  assert.strictEqual(searches[0], "public|trusted", "trusted muss public+trusted sehen")
})

// ── e) nexus_forget ────────────────────────────────────────────────────────

function forgetTool({ client, accessLevel = "private" } = {}) {
  let tool = null
  registerForgetTool(
    { registerTool: (tt) => { tool = tt } },
    { embed: async () => [0.1, 0.2] },
    client,
    { accessLevel, forgetMinScore: 0.5 },
  )
  assert.ok(tool, "nexus_forget muss registriert sein")
  return tool
}

const forgettingClient = () => {
  const seen = { deleted: [], searchByVector: [] }
  return {
    seen,
    scrollPointStrict: async (id) => ({
      id,
      payload: { text: "privater Inhalt", access_level: "private" },
    }),
    searchByVector: async (_vec, _limit, accessLevel) => {
      seen.searchByVector.push(accessLevel)
      return []
    },
    delete: async (id) => { seen.deleted.push(id) },
  }
}

await t("e) Gruppen-Merkmal → forget-by-query sucht nur 'public'", async () => {
  resetTurnContext()
  setTurnContext(true)
  const client = forgettingClient()
  await forgetTool({ client }).execute("t", { query: "privater Inhalt" })
  assert.deepStrictEqual(client.seen.searchByVector, ["public"], "Gruppen-Suche muss public sein")
})

await t("e2) Gruppen-Merkmal → Direkt-Löschung einer privaten Erinnerung wird verweigert", async () => {
  resetTurnContext()
  setTurnContext(true)
  const client = forgettingClient()
  const res = await forgetTool({ client }).execute("t", { memoryId: "mem-private" })
  assert.strictEqual(res.isError, true, "Verweigerung muss isError tragen")
  assert.match(textOf(res), /refused/i)
  assert.deepStrictEqual(client.seen.deleted, [], "in der Gruppe darf nichts gelöscht werden")
})

await t("e3) Direktchat-Merkmal → dieselbe Löschung ist erlaubt", async () => {
  resetTurnContext()
  setTurnContext(false)
  const client = forgettingClient()
  const res = await forgetTool({ client }).execute("t", { memoryId: "mem-private" })
  assert.strictEqual(res.isError, undefined)
  assert.deepStrictEqual(client.seen.deleted, ["mem-private"], "im DM muss gelöscht werden")
})

await t("e4) kein Merkmal → Direkt-Löschung fail-closed verweigert", async () => {
  resetTurnContext()
  const client = forgettingClient()
  const res = await forgetTool({ client }).execute("t", { memoryId: "mem-private" })
  assert.strictEqual(res.isError, true)
  assert.deepStrictEqual(client.seen.deleted, [], "ohne Merkmal darf nichts gelöscht werden")
})

// ── f) Graph-Werkzeuge ─────────────────────────────────────────────────────

const ENTITY_POINTS = [
  { id: "ent-public", payload: { category: "entity", entity_name: "Public", access_level: "public" } },
  { id: "ent-private", payload: { category: "entity", entity_name: "Private", access_level: "private" } },
]

function entityTool(cfg = { accessLevel: "private" }) {
  const filters = []
  let tool = null
  registerFindEntitiesTool(
    { registerTool: (tt) => { tool = tt } },
    {
      scrollFiltered: async (filter) => {
        filters.push(filter)
        return ENTITY_POINTS
      },
    },
    cfg,
  )
  assert.ok(tool, "nexus_find_entities muss registriert sein")
  return { tool, filters }
}

await t("f) Gruppen-Merkmal → find_entities filtert auf 'public'", async () => {
  resetTurnContext()
  setTurnContext(true)
  const { tool, filters } = entityTool()
  const res = await tool.execute("t", {})
  const clause = filters[0].must.find((c) => c.key === "access_level")
  assert.ok(clause, "access_level-Klausel muss im Qdrant-Filter stehen")
  assert.deepStrictEqual(clause.match.any, ["public"], "Gruppe sieht nur public")
  const ids = res.details.entities.map((e) => e.id)
  assert.ok(!ids.includes("ent-private"), "private Entity darf in der Gruppe nicht erscheinen")
})

await t("f2) kein Merkmal → find_entities filtert fail-closed auf 'public'", async () => {
  resetTurnContext()
  const { tool, filters } = entityTool()
  await tool.execute("t", {})
  const clause = filters[0].must.find((c) => c.key === "access_level")
  assert.deepStrictEqual(clause?.match?.any, ["public"], "ohne Merkmal nur public")
})

await t("f3) Gruppen-Merkmal → graph_traverse auf privaten Startpunkt: 'not found'", async () => {
  resetTurnContext()
  setTurnContext(true)
  let tool = null
  registerGraphTraverseTool(
    { registerTool: (tt) => { tool = tt } },
    {
      scrollPointStrict: async (id) => ({
        id,
        payload: { entity_name: "Secret", access_level: "private", edges: [] },
      }),
    },
    { accessLevel: "private" },
  )
  const res = await tool.execute("t", { fact_id: "fact-secret" })
  assert.strictEqual(textOf(res), "Fact fact-secret not found")
})

await t("f4) Direktchat-Merkmal → derselbe Startpunkt ist erreichbar", async () => {
  resetTurnContext()
  setTurnContext(false)
  let tool = null
  registerGraphTraverseTool(
    { registerTool: (tt) => { tool = tt } },
    {
      scrollPointStrict: async (id) => ({
        id,
        payload: { entity_name: "Secret", access_level: "private", edges: [] },
      }),
    },
    { accessLevel: "private" },
  )
  const res = await tool.execute("t", { fact_id: "fact-secret" })
  assert.notStrictEqual(textOf(res), "Fact fact-secret not found")
})

// ── g) der Hook setzt das Merkmal ──────────────────────────────────────────

const recallCfg = { accessLevel: "private", maxRecallResults: 5, scope: "" }

await t("g) before_prompt_build (Gruppe) setzt isGroup=true", async () => {
  resetTurnContext()
  const handler = buildRecallHandler(
    { embed: async () => [0.1] },
    { search: async () => [] },
    recallCfg,
  )
  await handler(
    { prompt: "was weiss ich ueber das projekt" },
    { trigger: "user", groupId: "-1001234", sessionKey: "grp-1" },
  )
  const marker = getTurnContext()
  assert.ok(marker, "Merkmal muss gesetzt sein")
  assert.strictEqual(marker.isGroup, true)
  assert.strictEqual(marker.sessionKey, "grp-1")
})

await t("g2) before_prompt_build (DM, leerer groupId) → isGroup=true (fail-closed)", async () => {
  resetTurnContext()
  const handler = buildRecallHandler(
    { embed: async () => [0.1] },
    { search: async () => [] },
    recallCfg,
  )
  await handler({ prompt: "was weiss ich ueber das projekt" }, { trigger: "user", groupId: "" })
  assert.strictEqual(getTurnContext()?.isGroup, true, "leerer groupId ist eine Gruppe, kein DM")
})

await t("g3) before_prompt_build (DM) → isGroup=false, cfg gilt", async () => {
  resetTurnContext()
  const handler = buildRecallHandler(
    { embed: async () => [0.1] },
    { search: async () => [] },
    recallCfg,
  )
  await handler(
    { prompt: "was weiss ich ueber das projekt" },
    { trigger: "user", groupId: null },
  )
  assert.strictEqual(getTurnContext()?.isGroup, false)
  assert.strictEqual(toolAccessLevel("private"), "private")
})

await t("g4) nicht-interaktiver Turn frischt das Merkmal ebenfalls auf", async () => {
  // Ein Cron-/Heartbeat-Turn hat KEINE Gruppe: bliebe das alte Gruppen-Merkmal
  // stehen, wären dessen Werkzeuge unnötig auf public gekappt — und umgekehrt
  // könnte ein altes DM-Merkmal in einen Gruppenturn hineinragen.
  resetTurnContext()
  setTurnContext(true, "alte-gruppe", Date.now() - 1_000)
  const handler = buildRecallHandler(
    { embed: async () => [0.1] },
    { search: async () => [] },
    recallCfg,
  )
  await handler({ prompt: "cron tick" }, { trigger: "heartbeat", groupId: null })
  assert.strictEqual(getTurnContext()?.isGroup, false, "der Turn muss das Merkmal auffrischen")
})

globalThis.fetch = realFetch
rmSync(SANDBOX, { recursive: true, force: true })

process.exitCode = failed ? 1 : 0
