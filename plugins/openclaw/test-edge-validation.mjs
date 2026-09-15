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

register("./_typebox-test-loader.mjs", import.meta.url)
const { normalizeEdge, isActiveEdge } = await import("./lib/edge.ts")
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

await t("normalizeEdge: edge_id darf fehlen → \"\", niemals undefined", () => {
  const e = normalizeEdge({ target_fact_id: "b", relation: "manages" })
  assert.strictEqual(e.edgeId, "")
  assert.notStrictEqual(e.edgeId, undefined)
  const e2 = normalizeEdge({ target_fact_id: "b", relation: "manages", edge_id: 5 })
  assert.strictEqual(e2.edgeId, "")
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
  return {
    scrollPoint: async (id) => graph[id] ?? null,
    findIncomingEdges: async () => incoming,
  }
}

function registerToolFor(fn, client) {
  let tool
  const api = { registerTool: (tt) => { tool = tt } }
  fn(api, client, {})
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
  assert.match(res.content[0].text, /Found 1 connected facts/)
  const results = res.details.results
  assert.strictEqual(results.length, 1)
  assert.strictEqual(results[0].fact_id, "a")
  assert.ok(
    results.every((r) => typeof r.fact_id === "string" && r.fact_id !== ""),
    "kein undefined/leeres fact_id darf in results landen",
  )
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

process.exit(failed ? 1 : 0)
