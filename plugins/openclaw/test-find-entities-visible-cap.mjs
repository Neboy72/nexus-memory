/**
 * Regressionstest: `limit` gilt für SICHTBARE Entities (24.10.2026)
 *
 * Befund der Prüfung: `registerFindEntitiesTool` scrollte mit Gesamtdeckel
 * `limit` und filterte `access_level` erst DANACH (`points.filter(isVisible)`).
 * Ein `public`-Agent bekam dadurch weniger als `limit` Treffer, obwohl genug
 * sichtbare Entities existierten — die Liste war still zu kurz.
 *
 * Gepinnt wird hier:
 *  a) 30 Punkte, 10 davon public; limit=10, Stufe public → 10 sichtbare
 *  b) der Deckel bleibt ein Deckel: limit=5 → 5, nicht alle 10
 *  c) Stufe private → alle 30
 *  d) unbekannte Stufe → "No entities found." und KEIN Qdrant-Aufruf
 *
 * Kein echter Qdrant: der Client ist ein Stub, der den Filter wie Qdrant
 * anwendet (must-Klauseln, limit als Gesamtdeckel).
 */
import assert from "node:assert"
import { register } from "node:module"

register("./_typebox-test-loader.mjs", import.meta.url)
const { registerFindEntitiesTool } = await import("./tools/graph_traverse.ts")

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

const point = (id, accessLevel) => ({
  id,
  payload: {
    category: "entity",
    entity_type: accessLevel === "public" ? "software" : "service",
    entity_name: id,
    content: `${id} content`,
    access_level: accessLevel,
    edges: [],
  },
})

// Die 20 privaten Punkte stehen ZUERST: ohne Stufen-Filter verbraucht der
// Deckel sie, und die 10 public-Punkte fallen aus der Liste — genau der Fehler.
const POINTS = [
  ...Array.from({ length: 20 }, (_, i) => point(`ent-private-${i}`, "private")),
  ...Array.from({ length: 10 }, (_, i) => point(`ent-public-${i}`, "public")),
]

function matchesClause(payload, clause) {
  const value = payload[clause.key]
  const match = clause.match ?? {}
  if ("value" in match) return value === match.value
  if ("any" in match) return Array.isArray(match.any) && match.any.includes(value)
  return true
}

function matchesFilter(pt, filter) {
  if (!filter) return true
  return (filter.must ?? []).every((clause) => matchesClause(pt.payload ?? {}, clause))
}

function makeClient(points = POINTS) {
  const calls = { scrollFiltered: 0, filters: [] }
  return {
    calls,
    scrollFiltered: async (filter, limit, maxTotal) => {
      calls.scrollFiltered++
      calls.filters.push(filter)
      const total = maxTotal ?? limit
      return points.filter((pt) => matchesFilter(pt, filter)).slice(0, total)
    },
  }
}

function entityTool(cfg, client) {
  let tool = null
  registerFindEntitiesTool(
    { registerTool: (tt) => { tool = tt } },
    client,
    cfg,
  )
  assert.ok(tool, "Werkzeug wurde registriert")
  return tool
}

const textOf = (res) => res.content.map((c) => c.text).join("\n")

// ── a) limit zählt sichtbare Treffer ───────────────────────────────────────

await t("find_entities (public, limit=10): 10 sichtbare Treffer, nicht 0", async () => {
  const tool = entityTool({ accessLevel: "public" }, makeClient())
  const res = await tool.execute("t1", { limit: 10 })
  assert.ok(res.details, `kein details-Block — Antwort war: ${textOf(res)}`)
  const entities = res.details.entities
  assert.strictEqual(entities.length, 10, `erwartet 10 sichtbare, bekam ${entities.length}`)
  assert.ok(
    entities.every((e) => e.id.startsWith("ent-public-")),
    "nur sichtbare (public) Entities dürfen erscheinen",
  )
})

// ── b) der Deckel bleibt ein Deckel ────────────────────────────────────────

await t("find_entities (public, limit=5): genau 5, nicht alle 10", async () => {
  const tool = entityTool({ accessLevel: "public" }, makeClient())
  const res = await tool.execute("t2", { limit: 5 })
  assert.strictEqual(res.details.entities.length, 5, "limit muss weiterhin begrenzen")
})

// ── c) private sieht alles ─────────────────────────────────────────────────

await t("find_entities (private): alle 30 Entities", async () => {
  const tool = entityTool({ accessLevel: "private" }, makeClient())
  const res = await tool.execute("t3", {})
  assert.strictEqual(res.details.entities.length, 30)
})

// ── d) unbekannte Stufe: nichts sehen, nichts fragen ───────────────────────

await t("find_entities (unbekannte Stufe): 'No entities found.' ohne Qdrant-Aufruf", async () => {
  const client = makeClient()
  const tool = entityTool({ accessLevel: "unbekannt" }, client)
  const res = await tool.execute("t4", { limit: 10 })
  assert.strictEqual(textOf(res), "No entities found.")
  assert.strictEqual(res.details, undefined, "kein details-Block (kein Entity-Leck)")
  assert.strictEqual(client.calls.scrollFiltered, 0, "Qdrant darf für eine unbekannte Stufe nicht gefragt werden")
})

process.exit(failed ? 1 : 0)
