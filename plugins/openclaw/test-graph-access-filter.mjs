/**
 * Regressionstest: Sichtbarkeitsstufe in den vier Graph-Werkzeugen (24.10.2026)
 *
 * Befund aus dem ClawHub-Security-Audit von 1.21.14: `nexus_graph_traverse`,
 * `nexus_find_entities`, `nexus_get_subgraph` und `nexus_get_related` gaben
 * Qdrant-Punkte aus, ohne `access_level` zu prüfen. Ein Agent der Stufe
 * `public` konnte damit `private` Entities lesen (belegt: "Selbst-Reset" =
 * private, "AdGuard" = public) und über Kanten sogar Punkte erreichen, die
 * hinter einem privaten Knoten liegen.
 *
 * Gepinnt wird hier:
 *  a) `public` + find_entities → private Entity fehlt
 *  b) `private` + find_entities → dieselbe Entity ist da
 *  c) `public` + get_related/get_subgraph → privater Nachbar fehlt, public da;
 *     hinter einem privaten Knoten wird NICHT weitergetraversiert
 *  d) `public` + graph_traverse auf einen privaten Startpunkt → Antwort exakt
 *     wie bei einem nicht existierenden Punkt (kein Existenz-Hinweis)
 *  e) fehlt `access_level`, gilt der Punkt als `private` (fail-closed,
 *     Konvention `?? "private"` aus lib/qdrant-client.ts)
 *
 * Kein echter Qdrant: der Client ist ein Stub, die Punkte liegen im Speicher.
 */
import assert from "node:assert"
import { register } from "node:module"

register("./_typebox-test-loader.mjs", import.meta.url)
const {
  registerGraphTraverseTool,
  registerFindEntitiesTool,
  registerGetSubgraphTool,
  registerGetRelatedTool,
} = await import("./tools/graph_traverse.ts")

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

const activeEdge = (target, relation = "uses", edgeId = "") => ({
  target_fact_id: target,
  relation,
  status: "active",
  edge_id: edgeId,
})

const POINTS = {
  // Entity-Bestand (a/b/e)
  "ent-public": {
    category: "entity",
    entity_type: "software",
    entity_name: "AdGuard",
    content: "AdGuard Home",
    access_level: "public",
    edges: [],
  },
  "ent-private": {
    category: "entity",
    entity_type: "service",
    entity_name: "Selbst-Reset",
    content: "privater Dienst",
    access_level: "private",
    edges: [],
  },
  "ent-nolevel": {
    category: "entity",
    entity_type: "device",
    entity_name: "Ohne Stufe",
    content: "kein access_level im Payload",
    edges: [],
  },

  // Graph (c/d): öffentlicher Start mit einem public, einem private und einem
  // stufenlosen Nachbarn; hinter dem privaten Nachbarn hängt ein public Blatt.
  "fact-root": {
    category: "entity",
    entity_name: "Root",
    access_level: "public",
    edges: [
      activeEdge("fact-public-neighbor", "uses", "e1"),
      activeEdge("fact-private-neighbor", "uses", "e2"),
      activeEdge("fact-nolevel-neighbor", "uses", "e3"),
    ],
  },
  "fact-public-neighbor": {
    category: "entity",
    entity_name: "PublicNeighbor",
    access_level: "public",
    edges: [],
  },
  "fact-private-neighbor": {
    category: "entity",
    entity_name: "PrivateNeighbor",
    access_level: "private",
    // Das Blatt ist public — erreichbar nur ÜBER den privaten Knoten. Es darf
    // bei einem public-Agenten trotzdem nicht auftauchen (kein Durchlaufen).
    edges: [activeEdge("fact-leaf", "uses", "e4")],
  },
  "fact-nolevel-neighbor": {
    category: "entity",
    entity_name: "NoLevelNeighbor",
    content: "kein access_level im Payload",
    edges: [],
  },
  "fact-leaf": {
    category: "entity",
    entity_name: "Leaf",
    access_level: "public",
    edges: [],
  },

  // Privater Startpunkt (d)
  "fact-secret-root": {
    category: "entity",
    entity_name: "SecretRoot",
    access_level: "private",
    edges: [activeEdge("fact-public-neighbor", "uses", "e5")],
  },
}

const INCOMING = [
  { source_id: "fact-public-source", relation: "uses", edge_id: "i1" },
  { source_id: "fact-private-source", relation: "uses", edge_id: "i2" },
]

const ALL_POINTS = {
  ...POINTS,
  "fact-public-source": {
    category: "entity",
    entity_name: "PublicSource",
    access_level: "public",
    edges: [],
  },
  "fact-private-source": {
    category: "entity",
    entity_name: "PrivateSource",
    access_level: "private",
    edges: [],
  },
}

function makeClient(points = ALL_POINTS, incoming = INCOMING) {
  return {
    // A working store answers a known id and 404s on an unknown one — the
    // strict variant the tools actually call. Both are the same answer here.
    scrollPoint: async (id) => (points[id] ? { id, payload: points[id] } : null),
    scrollPointStrict: async (id) => (points[id] ? { id, payload: points[id] } : null),
    scrollFiltered: async () => Object.entries(points).map(([id, payload]) => ({ id, payload })),
    findIncomingEdges: async () => incoming,
  }
}

function graphTool(registerFn, cfg, client = makeClient()) {
  let tool = null
  registerFn(
    { registerTool: (tt) => { tool = tt } },
    client,
    cfg,
  )
  assert.ok(tool, "Werkzeug wurde registriert")
  return tool
}

const textOf = (res) => res.content.map((c) => c.text).join("\n")

// ── a) public sieht das private Entity nicht ────────────────────────────────

await t("find_entities (public): private Entity erscheint NICHT", async () => {
  const tool = graphTool(registerFindEntitiesTool, { accessLevel: "public" })
  const res = await tool.execute("t1", {})
  const ids = res.details.entities.map((e) => e.id)
  assert.ok(ids.includes("ent-public"), "public Entity muss sichtbar sein")
  assert.ok(!ids.includes("ent-private"), "private Entity darf NICHT erscheinen")
})

// ── b) private sieht dasselbe Entity ────────────────────────────────────────

await t("find_entities (private): dasselbe Entity erscheint", async () => {
  const tool = graphTool(registerFindEntitiesTool, { accessLevel: "private" })
  const res = await tool.execute("t2", {})
  const ids = res.details.entities.map((e) => e.id)
  assert.ok(ids.includes("ent-private"), "private Agent muss das Entity sehen")
  assert.ok(ids.includes("ent-public"), "public Entity bleibt sichtbar")
})

// ── c) Nachbarn: private fehlt, public ist da ───────────────────────────────

await t("get_related (public): privater Nachbar fehlt, public Nachbar ist da", async () => {
  const tool = graphTool(registerGetRelatedTool, { accessLevel: "public" })
  const res = await tool.execute("t3", { fact_id: "fact-root" })
  const ids = res.details.results.map((r) => r.fact_id)
  assert.ok(ids.includes("fact-public-neighbor"), "public Nachbar muss erscheinen")
  assert.ok(!ids.includes("fact-private-neighbor"), "privater Nachbar darf NICHT erscheinen")
})

await t("get_related (public): auch eingehende Kanten werden gefiltert", async () => {
  const tool = graphTool(registerGetRelatedTool, { accessLevel: "public" })
  const res = await tool.execute("t4", { fact_id: "fact-root" })
  const ids = res.details.results.map((r) => r.fact_id)
  assert.ok(ids.includes("fact-public-source"), "public Quelle muss erscheinen")
  assert.ok(!ids.includes("fact-private-source"), "private Quelle darf NICHT erscheinen")
})

await t("get_subgraph (public): privater Nachbar und dahinterliegendes Blatt fehlen", async () => {
  const tool = graphTool(registerGetSubgraphTool, { accessLevel: "public" })
  const res = await tool.execute("t5", { fact_id: "fact-root", max_depth: 3 })
  const ids = res.details.nodes.map((n) => n.id)
  assert.ok(ids.includes("fact-root"), "Startknoten muss erscheinen")
  assert.ok(ids.includes("fact-public-neighbor"), "public Nachbar muss erscheinen")
  assert.ok(!ids.includes("fact-private-neighbor"), "privater Nachbar darf NICHT erscheinen")
  assert.ok(
    !ids.includes("fact-leaf"),
    "hinter einem privaten Knoten darf nicht weitergetraversiert werden",
  )
  const edgeTargets = res.details.edges.map((e) => e.target)
  assert.ok(
    !edgeTargets.includes("fact-private-neighbor"),
    "keine Kante zu einem unsichtbaren Knoten",
  )
})

await t("graph_traverse (public): private Nachbarn und Pfade dahinter fehlen", async () => {
  const tool = graphTool(registerGraphTraverseTool, { accessLevel: "public" })
  const res = await tool.execute("t6", { fact_id: "fact-root", max_depth: 3 })
  const ids = res.details.results.map((r) => r.fact_id)
  assert.ok(ids.includes("fact-public-neighbor"), "public Nachbar muss erscheinen")
  assert.ok(!ids.includes("fact-private-neighbor"), "privater Nachbar darf NICHT erscheinen")
  assert.ok(!ids.includes("fact-leaf"), "kein Pfad durch einen privaten Knoten")
})

await t("get_subgraph (private): derselbe private Nachbar ist sichtbar", async () => {
  const tool = graphTool(registerGetSubgraphTool, { accessLevel: "private" })
  const res = await tool.execute("t7", { fact_id: "fact-root", max_depth: 3 })
  const ids = res.details.nodes.map((n) => n.id)
  assert.ok(ids.includes("fact-private-neighbor"), "private Agent muss den Nachbarn sehen")
  assert.ok(ids.includes("fact-leaf"), "private Agent darf dahinter weiterlaufen")
})

// ── d) privater Startpunkt = nicht gefunden ────────────────────────────────

await t("graph_traverse (public): privater Startpunkt antwortet wie 'nicht gefunden'", async () => {
  const tool = graphTool(registerGraphTraverseTool, { accessLevel: "public" })
  const secret = textOf(await tool.execute("t8", { fact_id: "fact-secret-root" }))
  const absent = textOf(await tool.execute("t9", { fact_id: "gibt-es-nicht" }))
  assert.strictEqual(secret, "Fact fact-secret-root not found")
  assert.strictEqual(absent, "Fact gibt-es-nicht not found")
  assert.ok(
    !/forbidden|denied|access|private|exist/i.test(secret),
    `Antwort darf keinen Existenz-Hinweis enthalten: ${secret}`,
  )
})

await t("get_related (public): privater Startpunkt antwortet wie 'nicht gefunden'", async () => {
  const tool = graphTool(registerGetRelatedTool, { accessLevel: "public" })
  const secret = textOf(await tool.execute("t10", { fact_id: "fact-secret-root" }))
  assert.strictEqual(secret, "Fact fact-secret-root not found")
})

await t("get_subgraph (public): privater Startpunkt liefert keinen Teilgraphen", async () => {
  const tool = graphTool(registerGetSubgraphTool, { accessLevel: "public" })
  const res = await tool.execute("t11", { fact_id: "fact-secret-root", max_depth: 2 })
  assert.strictEqual(textOf(res), "No subgraph found around this fact.")
  assert.strictEqual(res.details, undefined, "kein details-Block (kein Knoten-/Kantenleck)")
})

// ── e) fehlendes access_level = private (fail-closed) ─────────────────────

await t("Punkt ohne access_level: für public unsichtbar (fail-closed)", async () => {
  const tool = graphTool(registerFindEntitiesTool, { accessLevel: "public" })
  const res = await tool.execute("t12", {})
  const ids = res.details.entities.map((e) => e.id)
  assert.ok(!ids.includes("ent-nolevel"), "Punkt ohne access_level gilt als private")
})

await t("Punkt ohne access_level: für private sichtbar (Konvention ?? 'private')", async () => {
  const tool = graphTool(registerFindEntitiesTool, { accessLevel: "private" })
  const res = await tool.execute("t13", {})
  const ids = res.details.entities.map((e) => e.id)
  assert.ok(ids.includes("ent-nolevel"), "fehlendes access_level ist 'private', nicht unsichtbar")
})

await t("Nachbar ohne access_level: public sieht ihn nicht, private schon", async () => {
  const publicTool = graphTool(registerGetRelatedTool, { accessLevel: "public" })
  const publicIds = (await publicTool.execute("t14", { fact_id: "fact-root" })).details.results.map(
    (r) => r.fact_id,
  )
  assert.ok(!publicIds.includes("fact-nolevel-neighbor"), "public darf ihn nicht sehen")

  const privateTool = graphTool(registerGetRelatedTool, { accessLevel: "private" })
  const privateIds = (
    await privateTool.execute("t15", { fact_id: "fact-root" })
  ).details.results.map((r) => r.fact_id)
  assert.ok(privateIds.includes("fact-nolevel-neighbor"), "private muss ihn sehen")
})

await t("Traverse-Start ohne access_level: public 'not found', private findet ihn", async () => {
  const publicTool = graphTool(registerGraphTraverseTool, { accessLevel: "public" })
  const absent = textOf(await publicTool.execute("t16", { fact_id: "fact-nolevel-neighbor" }))
  assert.strictEqual(absent, "Fact fact-nolevel-neighbor not found")

  const privateTool = graphTool(registerGraphTraverseTool, { accessLevel: "private" })
  const present = textOf(await privateTool.execute("t17", { fact_id: "fact-nolevel-neighbor" }))
  assert.notStrictEqual(present, "Fact fact-nolevel-neighbor not found")
})

process.exit(failed ? 1 : 0)
