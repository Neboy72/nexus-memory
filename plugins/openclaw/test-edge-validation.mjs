/**
 * Regressionstest: Edge-Validierung (H125, Welle 13)
 *
 * Edges liegen als untypisierte JSON-Objekte im `edges`-Payload eines
 * Entity-Punkts. Vorher wanderte `edge.target_fact_id as string` ungeprüft in
 * visited/BFS-Queue/results — eine einzige malformed Edge (target undefined)
 * korrumpierte den ganzen Traversal und lieferte `fact_id: undefined` an
 * Caller. normalizeEdge() ist jetzt das gemeinsame Gate beider Graph-Tools.
 */
import assert from "node:assert"
import { register } from "node:module"

try {
  register("./_typebox-test-loader.mjs", import.meta.url)
} catch (e) {
  console.log("FAIL ", "Loader-Registrierung", "— _typebox-test-loader.mjs unresolvable:", e?.message, "\n", e?.stack)
  process.exitCode = 1
  process.exit(process.exitCode)
}
let normalizeEdge, isActiveEdge, registerGraphTraverseTool, registerGetRelatedTool
try {
  ;({ normalizeEdge, isActiveEdge } = await import("./lib/edge.ts"))
  ;({ registerGraphTraverseTool, registerGetRelatedTool } = await import("./tools/graph_traverse.ts"))
} catch (e) {
  console.log("FAIL ", "Modul-Import", "— edge.ts/graph_traverse.ts unresolvable (node TS-Flag aktiv?):", e?.message, "\n", e?.stack)
  process.exitCode = 1
  process.exit(process.exitCode)
}

let failed = 0
const t = (name, fn) =>
  Promise.resolve()
    .then(fn)
    .then(() => console.log("PASS ", name))
    .catch((e) => {
      failed++
      const msg = e instanceof Error ? e.message : String(e)
      console.log("FAIL ", name, "—", msg, "\n", e?.stack ?? "(kein stack — non-Error throw)")
    })

// ── normalizeEdge direkt ────────────────────────────────────────────────────

await t("normalizeEdge: gutformatierte Edge wird durchgelassen", () => {
  assert.deepStrictEqual(
    normalizeEdge({ target_fact_id: "b", relation: "manages", edge_id: "e1" }),
    { targetId: "b", relation: "manages", edgeId: "e1" },
  )
})

await t("normalizeEdge: fehlende/kaputte target_fact_id → null", () => {
  assert.strictEqual(normalizeEdge({ relation: "manages" }), null)
  assert.strictEqual(normalizeEdge({ target_fact_id: undefined, relation: "manages" }), null)
  assert.strictEqual(normalizeEdge({ target_fact_id: 42, relation: "manages" }), null)
  assert.strictEqual(normalizeEdge({ target_fact_id: "", relation: "manages" }), null)
  assert.strictEqual(normalizeEdge({ target_fact_id: null, relation: "manages" }), null)
})

await t("normalizeEdge: fehlende/kaputte relation → null", () => {
  assert.strictEqual(normalizeEdge({ target_fact_id: "b" }), null)
  assert.strictEqual(normalizeEdge({ target_fact_id: "b", relation: 7 }), null)
  assert.strictEqual(normalizeEdge({ target_fact_id: "b", relation: "" }), null)
})

await t("normalizeEdge: edge_id fehlt/null → \"\", niemals undefined (\"\"-Fallback gepinnt)", () => {
  const e = normalizeEdge({ target_fact_id: "b", relation: "manages" })
  assert.strictEqual(e.edgeId, "")
  assert.notStrictEqual(e.edgeId, undefined)
  const e2 = normalizeEdge({ target_fact_id: "b", relation: "manages", edge_id: 5 })
  assert.strictEqual(e2.edgeId, "")
  const e3 = normalizeEdge({ target_fact_id: "b", relation: "manages", edge_id: null })
  assert.strictEqual(e3.edgeId, "", "edge_id:null muss auf \"\" fallen (nicht durchgelassen)")
  assert.strictEqual(typeof e3.edgeId, "string")
})

await t("normalizeEdge: Nicht-Objekte → null", () => {
  assert.strictEqual(normalizeEdge(null), null)
  assert.strictEqual(normalizeEdge(undefined), null)
  assert.strictEqual(normalizeEdge("edge"), null)
})

await t("isActiveEdge: fehlender status = aktiv, inaktive werden erkannt", () => {
  assert.strictEqual(isActiveEdge({}), true)
  assert.strictEqual(isActiveEdge({ status: "active" }), true)
  assert.strictEqual(isActiveEdge({ status: "deleted" }), false)
})

// ── Tool-Ebene: malformed Edges werden geskippt ─────────────────────────────

const MALFORMED_EDGE = { relation: "manages", edge_id: "e-bad" }
const NULL_TARGET_EDGE = { target_fact_id: null, relation: "manages" }
const BLANK_TARGET_EDGE = { target_fact_id: "", relation: "manages" }

function makeClient(graph, incoming = []) {
  // W38 (medium): echte findIncomingEdges(factId, relation) nimmt einen relation-Filter —
  // der Stub respektiert ihn, damit der incoming-Pfad-Filter nicht stumm bleibt.
  return {
    scrollPoint: async (id) => graph[id] ?? null,
    findIncomingEdges: async (_factId, relation) =>
      relation ? incoming.filter((e) => e.relation === relation) : incoming,
  }
}

function registerToolFor(fn, client) {
  let tool
  let registered = 0
  const api = { registerTool: (tt) => { tool = tt; registered++ } }
  fn(api, client, {})
  if (registered === 0 || !tool) {
    throw new Error(`Registrierung fehlgeschlagen: registerTool wurde ${registered}× gerufen (Export umbenannt? Signatur geändert?)`)
  }
  return tool
}

await t("graph_traverse: malformed Edges werden geskippt, gute durchgelassen", async () => {
  const client = makeClient({
    start: {
      id: "start",
      payload: {
        edges: [
          { target_fact_id: "a", relation: "manages", edge_id: "e1", status: "active" },
          MALFORMED_EDGE,
          NULL_TARGET_EDGE,
          BLANK_TARGET_EDGE,
          null,
        ],
      },
    },
    a: { id: "a", payload: { edges: [] } },
  })
  const tool = registerToolFor(registerGraphTraverseTool, client)
  const res = await tool.execute("id", { fact_id: "start" })
  // W38 (low): Kopplung an UI-Wortlaut gelöst — die Regression ist die Edge-Validierung,
  // nicht der Text. Gezählt wird über details.results.
  assert.ok(Array.isArray(res.details?.results), "details.results fehlt")
  const results = res.details.results
  assert.strictEqual(results.length, 1)
  assert.strictEqual(results[0].fact_id, "a")
  assert.ok(
    results.every((r) => typeof r.fact_id === "string" && r.fact_id !== ""),
    "kein undefined/leeres fact_id darf in results landen",
  )
})

await t("graph_traverse: edges undefined / non-array → keine Results, kein Crash", async () => {
  for (const bad of [undefined, { not: "an array" }, "edges", 7]) {
    const client = makeClient({ start: { id: "start", payload: { edges: bad } } })
    const tool = registerToolFor(registerGraphTraverseTool, client)
    const res = await tool.execute("id", { fact_id: "start" })
    const results = res.details?.results ?? []
    assert.strictEqual(results.length, 0, `edges=${JSON.stringify(bad)} muss 0 Results liefern`)
  }
})

await t("get_related: Contract {fact_id, relation, edge_id, direction}, malformed geskippt", async () => {
  const client = makeClient(
    {
      start: {
        id: "start",
        payload: {
          edges: [
            { target_fact_id: "a", relation: "manages", status: "active" },
            { target_fact_id: "b", relation: "runs_on", edge_id: "e2", status: "active" },
            MALFORMED_EDGE,
            NULL_TARGET_EDGE,
          ],
        },
      },
    },
    [{ source_id: "src-1", relation: "owns", edge_id: "e3" }],
  )
  const tool = registerToolFor(registerGetRelatedTool, client)
  const res = await tool.execute("id", { fact_id: "start" })
  const results = res.details.results

  // 2 outgoing (validiert) + 1 incoming
  assert.strictEqual(results.length, 3)
  for (const r of results) {
    assert.deepStrictEqual(Object.keys(r).sort(), ["direction", "edge_id", "fact_id", "relation"])
    assert.strictEqual(typeof r.fact_id, "string")
    assert.notStrictEqual(r.fact_id, "")
    assert.strictEqual(typeof r.relation, "string")
    assert.notStrictEqual(r.relation, "")
    assert.notStrictEqual(r.edge_id, undefined, "edge_id darf nie undefined sein")
  }
  const outgoing = results.filter((r) => r.direction === "outgoing")
  assert.strictEqual(outgoing.length, 2)
  assert.ok(outgoing.every((r) => r.edge_id === "" || typeof r.edge_id === "string"))
  const incoming = results.find((r) => r.direction === "incoming")
  assert.deepStrictEqual(incoming, {
    fact_id: "src-1",
    relation: "owns",
    edge_id: "e3",
    direction: "incoming",
  })
})

process.exitCode = failed ? 1 : 0
