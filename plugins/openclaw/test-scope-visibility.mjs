/**
 * Probe: Sichtbarkeit nach Verbund in den Lesewegen des OpenClaw-Plugins
 * (15.11.2026).
 *
 * Entschieden: ein Punkt ist sichtbar, wenn
 *   scope == "default"        (gemeinsam — jeder Agent liest das),
 *   scope == <eigener Verbund> (die eigenen Sitzungsprotokolle),
 *   oder das Feld `scope` FEHLT (Altbestand, darf nicht verschwinden).
 * Die Zugriffsstufe bleibt eine eigene Achse: sie wird ueber `must` neben den
 * Verbund gelegt, nie ersetzt.
 *
 * Vier Proben je Leseweg, kein echter Qdrant:
 *   a) Punkt in "default"            → wird gefunden
 *   b) Punkt in "hermes" (fremd)     → wird NICHT gefunden
 *   c) Punkt ohne `scope`-Feld       → wird gefunden
 *   d) Punkt im eigenen Verbund      → wird gefunden
 *
 * Geprueft werden die DREI Wege, die die Regel bisher umgingen:
 *   1. `search()` — der Filter, der wirklich an Qdrant geht
 *   2. Graph-Nachbarn (`graphBoost` im Recall-Hook)
 *   3. Entity-Aufloesung (`nexus_find_entities`)
 */
import assert from "node:assert"
import { register } from "node:module"

register("./_typebox-test-loader.mjs", import.meta.url)

const {
  QdrantClient,
  scopeBundle,
  scopeShould,
  scopeVisible,
  SHARED_SCOPE,
} = await import("./lib/qdrant-client.ts")
const { buildRecallHandler } = await import("./hooks/recall.ts")
const { registerFindEntitiesTool } = await import("./tools/graph_traverse.ts")
const { setTurnContext, resetTurnContext, bindToolSession } = await import("./lib/turn-context.ts")

let failed = 0
const t = (name, fn) =>
  Promise.resolve()
    .then(fn)
    .then(() => console.log("PASS ", name))
    .catch((e) => {
      failed++
      console.log("FAIL ", name, "—", (e && e.message) || String(e))
    })

const OWN = "openclaw" // Verbund dieses Plugins (Vorgabe)
const FOREIGN = "hermes"

// ── Fixtures: je ein Punkt pro Sichtbarkeitsfall ────────────────────────────

const POINTS = [
  { id: "p-default", payload: { scope: SHARED_SCOPE, category: "fact", access_level: "public" } },
  { id: "p-fremd", payload: { scope: FOREIGN, category: "session", access_level: "public" } },
  { id: "p-alt", payload: { category: "fact", access_level: "public" } }, // kein scope-Feld
  { id: "p-eigen", payload: { scope: OWN, category: "session", access_level: "public" } },
]
const ERWARTET = ["p-alt", "p-default", "p-eigen"]

// ── Ein winziger Qdrant-Filter-Auswerter ────────────────────────────────────
// Prueft GENAU den Filter, der ueber die Leitung geht (must = UND,
// should = mindestens eine, is_empty = Feld fehlt/leer).

function matchesClause(payload, clause) {
  if (clause.is_empty) {
    const v = payload[clause.is_empty.key]
    return v === undefined || v === null || v === ""
  }
  if (clause.key && clause.match) {
    const v = payload[clause.key]
    if ("value" in clause.match) return v === clause.match.value
    if ("any" in clause.match) return Array.isArray(clause.match.any) && clause.match.any.includes(v)
  }
  throw new Error("unbekannte Filter-Klausel: " + JSON.stringify(clause))
}

function matchesFilter(payload, filter) {
  if (!filter) return true
  const must = filter.must ?? []
  const should = filter.should ?? []
  const mustOk = must.every((c) => matchesClause(payload, c))
  const shouldOk = should.length === 0 || should.some((c) => matchesClause(payload, c))
  return mustOk && shouldOk
}

const idsOf = (points) => points.map((p) => p.id).sort()

// ── 0) Die gemeinsame Filterform ───────────────────────────────────────────

await t("scopeShould(bundle) traegt die drei Bedingungen als should", () => {
  const should = scopeShould(OWN)
  assert.strictEqual(should.length, 3, "drei Bedingungen erwartet: " + JSON.stringify(should))
  assert.deepStrictEqual(should[0], { key: "scope", match: { value: SHARED_SCOPE } })
  assert.deepStrictEqual(should[1], { key: "scope", match: { value: OWN } })
  assert.deepStrictEqual(should[2], { is_empty: { key: "scope" } })
})

await t("scopeVisible: die vier Faelle", () => {
  assert.strictEqual(scopeVisible({ scope: SHARED_SCOPE }, OWN), true, "gemeinsam")
  assert.strictEqual(scopeVisible({ scope: FOREIGN }, OWN), false, "fremder Verbund")
  assert.strictEqual(scopeVisible({}, OWN), true, "kein Feld = Altbestand")
  assert.strictEqual(scopeVisible({ scope: OWN }, OWN), true, "eigener Verbund")
})

await t("scopeBundle: Config-Bereich gewinnt, sonst 'openclaw'", () => {
  assert.strictEqual(scopeBundle({ scope: "voice" }), "voice")
  assert.strictEqual(scopeBundle({}), OWN)
  assert.strictEqual(scopeBundle(undefined), OWN)
  assert.strictEqual(scopeBundle({ scope: "NICHT GUELTIG" }), OWN)
})

// ── 1) search(): der Filter, der wirklich rausgeht ─────────────────────────

const realFetch = globalThis.fetch

async function captureSearchFilter(accessLevel, cfgScope) {
  let body
  globalThis.fetch = async (url, opts = {}) => {
    const u = typeof url === "string" ? url : url.url
    if (u.endsWith("/collections/nexus")) {
      return {
        ok: true,
        status: 200,
        json: async () => ({
          result: { config: { params: { vectors: { size: 8, distance: "Cosine" } } } },
        }),
        text: async () => "",
      }
    }
    body = JSON.parse(opts.body ?? "{}")
    return { ok: true, status: 200, json: async () => ({ result: [] }), text: async () => "" }
  }
  try {
    const bundle = scopeBundle({ scope: cfgScope })
    const client = new QdrantClient("http://localhost:6333", "nexus", 8, bundle)
    await client.search([0.1], 5, accessLevel)
    return body
  } finally {
    globalThis.fetch = realFetch
  }
}

await t("search(): die vier Sichtbarkeitsfaelle (Stufe public)", async () => {
  const body = await captureSearchFilter("public")
  assert.ok(body?.filter, "search muss einen Filter mitsenden: " + JSON.stringify(body))
  assert.deepStrictEqual(idsOf(POINTS.filter((p) => matchesFilter(p.payload, body.filter))), ERWARTET)
})

await t("search(): Stufe und Verbund sind zwei Achsen (must + should)", async () => {
  const body = await captureSearchFilter("public")
  // Stufe: eigener must-Eintrag — der Verbund ersetzt ihn nicht.
  assert.ok(
    (body.filter.must ?? []).some((c) => c.key === "access_level"),
    "Zugriffsstufe muss in must stehen: " + JSON.stringify(body.filter),
  )
  // Verbund: eigene should-Liste, NICHT in derselben Liste wie die Stufe.
  assert.ok(Array.isArray(body.filter.should) && body.filter.should.length === 3,
    "Verbund muss als eigene should-Liste stehen: " + JSON.stringify(body.filter))
  assert.ok(
    !(body.filter.should ?? []).some((c) => c.key === "access_level"),
    "Stufe darf nicht in der Verbund-should-Liste stehen (das waere ein ODER)",
  )
})

await t("search(): fremdes Protokoll bleibt auch bei Stufe private draussen", async () => {
  const body = await captureSearchFilter("private")
  assert.ok(body?.filter, "auch ohne Stufenfilter muss der Verbund-Filter stehen")
  assert.ok(
    !idsOf(POINTS.filter((p) => matchesFilter(p.payload, body.filter))).includes("p-fremd"),
    "fremder Verbund darf nie durchkommen",
  )
})

await t("search(): eigener Config-Bereich statt 'openclaw'", async () => {
  const body = await captureSearchFilter("public", "voice")
  const punkt = { id: "p-voice", payload: { scope: "voice", access_level: "public" } }
  assert.ok(matchesFilter(punkt.payload, body.filter), "der konfigurierte eigene Bereich ist sichtbar")
  assert.ok(!matchesFilter(POINTS[1].payload, body.filter), "fremd bleibt fremd")
})

// ── 2) Graph-Nachbarn: der Recall-Hook ─────────────────────────────────────

const CFG = {
  qdrantUrl: "http://localhost:6333",
  collection: "nexus",
  accessLevel: "public",
  maxRecallResults: 8,
  scope: "",
  privateRooms: [],
  ownerUserIds: ["owner-1"],
  autoRecall: true,
}

function fakeClient(neighbours) {
  return {
    async search() {
      return [
        {
          id: "root",
          text: "Wurzel-Fakt",
          score: 0.9,
          category: "fact",
          access_level: "public",
          source: "conversation",
          created_at: "",
          scope: SHARED_SCOPE,
        },
      ]
    },
    async scrollPoint(id) {
      if (id === "root") {
        return {
          id,
          payload: {
            scope: SHARED_SCOPE,
            access_level: "public",
            edges: neighbours.map((n) => ({
              target_fact_id: n.id,
              relation: "uses",
              status: "active",
            })),
          },
        }
      }
      const n = neighbours.find((x) => x.id === id)
      return n ? { id, payload: n.payload } : null
    },
  }
}

async function recallContext(neighbours, cfgScope = "") {
  resetTurnContext()
  const handler = buildRecallHandler(
    { embed: async () => [0.1, 0.2] },
    fakeClient(neighbours),
    { ...CFG, scope: cfgScope },
  )
  const out = await handler({ prompt: "worum geht es beim verteiler" }, {})
  return out?.prependContext ?? ""
}

const NEIGHBOURS = [
  { id: "n-default", payload: { content: "NACHBAR-GEMEINSAM", scope: SHARED_SCOPE, access_level: "public" } },
  { id: "n-fremd", payload: { content: "NACHBAR-FREMD-HERMES", scope: FOREIGN, access_level: "public" } },
  { id: "n-alt", payload: { content: "NACHBAR-ALTBESTAND", access_level: "public" } },
  { id: "n-eigen", payload: { content: "NACHBAR-EIGEN", scope: OWN, access_level: "public" } },
]

await t("graphBoost: die vier Sichtbarkeitsfaelle", async () => {
  const ctx = await recallContext(NEIGHBOURS)
  assert.ok(ctx, "der Block darf nicht leer sein")
  assert.match(ctx, /NACHBAR-GEMEINSAM/, "Punkt in 'default' muss erscheinen")
  assert.ok(!ctx.includes("NACHBAR-FREMD-HERMES"), "fremdes Protokoll darf NICHT erscheinen")
  assert.match(ctx, /NACHBAR-ALTBESTAND/, "Punkt ohne scope-Feld muss erscheinen")
  assert.match(ctx, /NACHBAR-EIGEN/, "eigener Verbund muss erscheinen")
})

await t("graphBoost: eigener Config-Bereich statt 'openclaw'", async () => {
  const ctx = await recallContext(
    [
      { id: "n-voice", payload: { content: "NACHBAR-VOICE", scope: "voice", access_level: "public" } },
      { id: "n-fremd", payload: { content: "NACHBAR-FREMD-HERMES", scope: FOREIGN, access_level: "public" } },
    ],
    "voice",
  )
  assert.match(ctx, /NACHBAR-VOICE/, "der konfigurierte eigene Bereich muss erscheinen")
  assert.ok(!ctx.includes("NACHBAR-FREMD-HERMES"), "fremd bleibt fremd")
})

// ── 3) Entity-Aufloesung ───────────────────────────────────────────────────

function entityTool(points, cfg = { accessLevel: "public" }) {
  resetTurnContext()
  const SESSION = "scope-probe"
  setTurnContext(false, SESSION)
  for (const id of ["e1", ...Array.from({ length: 20 }, (_, i) => `e${i + 2}`)]) {
    bindToolSession(id, SESSION)
  }
  // Der Stub wendet den ECHTEN Filter an (must = UND, should = ODER) — was
  // Qdrant nicht sehen wuerde, kommt auch hier nicht zurueck.
  const client = {
    async scrollFiltered(filter, _limit, _max) {
      return points.filter((p) => matchesFilter(p.payload, filter))
    },
  }
  let tool = null
  registerFindEntitiesTool({ registerTool: (tt) => { tool = tt } }, client, cfg)
  assert.ok(tool, "find_entities wurde nicht registriert")
  return tool
}

const ENTITIES = [
  { id: "e-default", payload: { category: "entity", entity_name: "Gemeinsam", access_level: "public", scope: SHARED_SCOPE } },
  { id: "e-fremd", payload: { category: "entity", entity_name: "FremdHermes", access_level: "public", scope: FOREIGN } },
  { id: "e-alt", payload: { category: "entity", entity_name: "Altbestand", access_level: "public" } },
  { id: "e-eigen", payload: { category: "entity", entity_name: "Eigen", access_level: "public", scope: OWN } },
]

await t("find_entities: die vier Sichtbarkeitsfaelle", async () => {
  const res = await entityTool(ENTITIES).execute("e1", {})
  const ids = (res.details?.entities ?? []).map((e) => e.id).sort()
  assert.deepStrictEqual(ids, ["e-alt", "e-default", "e-eigen"])
})

await t("find_entities: fremde Entity bleibt auch konfiguriert-fremd draussen", async () => {
  const res = await entityTool(ENTITIES, { accessLevel: "public", scope: "voice" }).execute("e2", {})
  const ids = (res.details?.entities ?? []).map((e) => e.id)
  assert.ok(!ids.includes("e-fremd"), "fremder Verbund darf nicht erscheinen")
  assert.ok(ids.includes("e-default"), "gemeinsamer Bereich bleibt sichtbar")
  assert.ok(ids.includes("e-alt"), "Altbestand bleibt sichtbar")
})

// ── Ergebnis ───────────────────────────────────────────────────────────────

globalThis.fetch = realFetch
console.log(failed ? `\n${failed} Probe(n) fehlgeschlagen` : "\nalle Proben bestanden")
process.exitCode = failed ? 1 : 0
