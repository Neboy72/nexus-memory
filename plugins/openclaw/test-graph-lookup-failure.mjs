/**
 * Regressionstest: Ausfall ist nicht „unsichtbar" (24.10.2026)
 *
 * Befund der Prüfung: `QdrantClient.scrollPoint()` liefert für BEIDES `null` —
 * Punkt fehlt (404) und Anfrage gescheitert (500/403/Netz). `scrollPointStrict()`
 * unterscheidet das (404 → null, sonst Fehler). `get_related` und die BFS luden
 * mit `scrollPoint`; bei einem kurzen Qdrant-Ausfall verschwanden Kanten still
 * und der Nutzer las „No related facts found.", obwohl er nur eine Störung hat.
 *
 * Gepinnt wird hier:
 *  a) Nachbar-Lookup scheitert → get_related meldet den Fehler (nicht „No
 *     related facts found.")
 *  b) BFS-Ziel scheitert → graph_traverse meldet den Fehler
 *  c) Wurzel-Lookup scheitert → graph_traverse meldet den Fehler (nicht
 *     „Fact … not found")
 *  d) echter 404 bleibt „auslassen" — Verhalten unverändert
 *
 * Kein echter Qdrant: der Client ist ein Stub. `scrollPoint` verhält sich wie
 * das alte, fail-offene Original (null bei Störung) — damit ist der Test auf
 * dem Stand VOR dem Fix rot.
 */
import assert from "node:assert"
import { register } from "node:module"

register("./_typebox-test-loader.mjs", import.meta.url)
const { registerGraphTraverseTool, registerGetRelatedTool } = await import(
  "./tools/graph_traverse.ts"
)

let failed = 0
const t = (name, fn) =>
  Promise.resolve()
    .then(fn)
    .then(() => console.log("PASS ", name))
    .catch((e) => {
      failed++
      console.log("FAIL ", name, "—", e.message)
    })

// ── Fixtures ────────────────────────────────────────────────────────────────

const edge = (target, relation = "uses") => ({
  target_fact_id: target,
  relation,
  status: "active",
  edge_id: `e-${target}`,
})

const GRAPH = {
  root: {
    category: "entity",
    entity_name: "Root",
    access_level: "private",
    edges: [edge("neighbour-broken"), edge("neighbour-ok")],
  },
  "neighbour-ok": {
    category: "entity",
    entity_name: "Ok",
    access_level: "private",
    edges: [],
  },
  "neighbour-broken": {
    category: "entity",
    entity_name: "Broken",
    access_level: "private",
    edges: [],
  },
}

/**
 * `strictFails`: IDs, deren Lookup eine Störung meldet (500/Netz).
 * `lenientNulls`: dieselben IDs für den alten `scrollPoint` — der Stub bildet
 * das fail-offene Original nach (Störung → null), damit der Rot-Beweis trägt.
 */
function makeClient({ graph = GRAPH, strictFails = [], lenientNulls = [], incoming = [] } = {}) {
  const calls = { scrollPointStrict: [], scrollPoint: [] }
  return {
    calls,
    scrollPointStrict: async (id) => {
      calls.scrollPointStrict.push(id)
      if (strictFails.includes(id)) throw new Error("Qdrant point lookup failed: 500")
      return graph[id] ? { id, payload: graph[id] } : null
    },
    scrollPoint: async (id) => {
      calls.scrollPoint.push(id)
      if (lenientNulls.includes(id)) return null
      return graph[id] ? { id, payload: graph[id] } : null
    },
    findIncomingEdges: async (_factId, relation) =>
      relation ? incoming.filter((e) => e.relation === relation) : incoming,
  }
}

function toolFor(registerFn, client) {
  let tool = null
  registerFn(
    { registerTool: (tt) => { tool = tt } },
    client,
    { accessLevel: "private" },
  )
  assert.ok(tool, "Werkzeug wurde registriert")
  return tool
}

const textOf = (res) => res.content.map((c) => c.text).join("\n")

const assertFailure = (text, what) => {
  assert.notStrictEqual(
    text,
    "No related facts found.",
    `${what}: Ausfall darf nicht als „No related facts found." erscheinen`,
  )
  assert.notStrictEqual(
    text,
    "No connected facts found.",
    `${what}: Ausfall darf nicht als „No connected facts found." erscheinen`,
  )
  assert.match(text, /failed/i, `${what}: Antwort muss den Fehler benennen, war: ${text}`)
}

// ── a) get_related: Nachbar-Lookup scheitert ───────────────────────────────

await t("get_related: scheiternder Nachbar-Lookup meldet Fehler", async () => {
  const client = makeClient({
    strictFails: ["neighbour-broken"],
    lenientNulls: ["neighbour-broken"],
  })
  const tool = toolFor(registerGetRelatedTool, client)
  const res = await tool.execute("t1", { fact_id: "root" })
  const text = textOf(res)
  assertFailure(text, "get_related")
  assert.match(text, /Get related failed/)
  assert.ok(
    client.calls.scrollPointStrict.includes("neighbour-broken"),
    "der Nachbar muss über den strict-Lookup geladen werden",
  )
})

// ── b) graph_traverse: BFS-Ziel scheitert ──────────────────────────────────

await t("graph_traverse: scheiterndes BFS-Ziel meldet Fehler", async () => {
  const client = makeClient({
    strictFails: ["neighbour-broken"],
    lenientNulls: ["neighbour-broken"],
  })
  const tool = toolFor(registerGraphTraverseTool, client)
  const res = await tool.execute("t2", { fact_id: "root", max_depth: 2 })
  const text = textOf(res)
  assertFailure(text, "graph_traverse")
  assert.match(text, /Graph traverse failed/)
})

// ── c) graph_traverse: Wurzel-Lookup scheitert ─────────────────────────────

await t("graph_traverse: scheiternder Wurzel-Lookup meldet Fehler, nicht 'not found'", async () => {
  const client = makeClient({ strictFails: ["root"], lenientNulls: ["root"] })
  const tool = toolFor(registerGraphTraverseTool, client)
  const text = textOf(await tool.execute("t3", { fact_id: "root", max_depth: 2 }))
  assert.notStrictEqual(
    text,
    "Fact root not found",
    "eine Störung darf nicht als fehlender Punkt erscheinen",
  )
  assert.match(text, /Graph traverse failed/)
})

// ── d) echter 404 bleibt „auslassen" ───────────────────────────────────────

await t("get_related: fehlender Nachbar (404) bleibt still ausgelassen", async () => {
  const graph = {
    root: { ...GRAPH.root, edges: [edge("neighbour-ok"), edge("gibt-es-nicht")] },
    "neighbour-ok": GRAPH["neighbour-ok"],
  }
  // "gibt-es-nicht" steht in keiner Fehlerliste: strict liefert null wie bei
  // einem echten 404.
  const client = makeClient({ graph })
  const tool = toolFor(registerGetRelatedTool, client)
  const res = await tool.execute("t4", { fact_id: "root" })
  const ids = (res.details?.results ?? []).map((r) => r.fact_id)
  assert.deepStrictEqual(ids, ["neighbour-ok"], "nur der vorhandene Nachbar erscheint")
})

process.exit(failed ? 1 : 0)
