/**
 * Regressionstest: Schutzregeln werden VOLLSTAENDIG geladen (Befund 2)
 *
 * ClawHub-Gutachten vom 14.11.2026, Befund 2 (`lib/qdrant-client.ts:823`):
 * ein Fehler auf einer spaeteren Seite — oder die erreichte Seitengrenze —
 * galt als Erfolg, und eine gefaehrliche Aktion wurde durchgelassen, weil die
 * sperrende Regel auf einer nie geholten Seite lag. Jetzt gilt: der
 * sicherheitskritische Lesevorgang ist vollstaendig oder ein Fehler
 * (`scrollFilteredStrict`), und eine unvollstaendige Regelmenge sperrt.
 *
 * Getestet am echten Tool gegen einen fetch-Stub, damit der ganze Weg
 * (Qdrant-REST → QdrantClient → loadProtectionRules → Verdict) laeuft.
 */
import assert from "node:assert"
import { register } from "node:module"

import { QdrantClient } from "./lib/qdrant-client.ts"
import { bindToolSession, setTurnContext } from "./lib/turn-context.ts"

register("./_typebox-test-loader.mjs", import.meta.url)
const { registerGuardrailCheckTool } = await import("./tools/guardrail_check.ts")

let failed = 0
const t = (name, fn) =>
  Promise.resolve()
    .then(fn)
    .then(() => console.log("PASS ", name))
    .catch((e) => {
      failed++
      console.log("FAIL ", name, "—", e?.stack ?? String(e))
    })

const realFetch = globalThis.fetch
process.on("beforeExit", () => { globalThis.fetch = realFetch })

// Owner-Sitzung: die Regeln sind `private`, ihre Metadaten sollen hier
// sichtbar sein (die Sichtbarkeitsfilterung selbst deckt
// test-guardrail-disclosure.mjs ab).
const SESSION = "protection-rule-load"
setTurnContext(false, SESSION, Date.now(), false, false, true)
bindToolSession("call-rule-load", SESSION)

/** Regel-Memory, wie sie in Qdrant liegt. */
const rulePoint = (id, path) => ({
  id,
  payload: { content: `niemals löschen: ${path}`, category: "rule", access_level: "private" },
})

/**
 * Qdrant-Scroll-Stub: blaettert durch `points` (limit aus dem Request-Body) und
 * laesst `failOnPage` (1-basiert) mit HTTP 503 scheitern.
 */
function mockScroll(points, { failOnPage = 0 } = {}) {
  const bodies = []
  globalThis.fetch = async (_url, opts = {}) => {
    const body = JSON.parse(opts.body)
    bodies.push(body)
    const page = bodies.length
    if (failOnPage && page === failOnPage) {
      return { ok: false, status: 503, json: async () => ({}), text: async () => "down" }
    }
    const start = Number(body.offset ?? 0)
    const slice = points.slice(start, start + Number(body.limit ?? 100))
    const next = start + slice.length < points.length ? start + slice.length : null
    return {
      ok: true,
      status: 200,
      json: async () => ({ result: { points: slice, next_page_offset: next } }),
      text: async () => "",
    }
  }
  return bodies
}

function makeTool() {
  let tool
  const api = { registerTool: (tt) => { tool = tt } }
  registerGuardrailCheckTool(
    api,
    new QdrantClient("http://localhost:6333", "nexus", 8),
    { collection: "nexus", accessLevel: "private" },
  )
  if (!tool) throw new Error("registerGuardrailCheckTool hat KEIN Tool registriert")
  return tool
}

const ask = async (tool, command) => {
  const res = await tool.execute("call-rule-load", { command })
  const text = res?.content?.[0]?.text
  if (typeof text !== "string") throw new Error(`unerwartetes Tool-Output: ${JSON.stringify(res)}`)
  return { out: JSON.parse(text), text }
}

// ── Sammlung ueber 1.000 Regeln: die letzte Seite entscheidet ───────────────

await t("1.200 Regeln: eine Regel auf der ZWEITEN Seite blockt (kein Teilsample)", async () => {
  const points = []
  for (let i = 0; i < 1199; i++) points.push(rulePoint(`r-${i}`, `/srv/unrelated-${i}`))
  points.push(rulePoint("r-last", "/srv/last-only"))
  const bodies = mockScroll(points)

  const { out } = await ask(makeTool(), "rm -rf /srv/last-only")
  assert.strictEqual(out.verdict, "block", `Regel der letzten Seite muss greifen: ${JSON.stringify(out)}`)
  assert.strictEqual(out.matched_rules[0].source_memory_id, "r-last")
  assert.ok(bodies.length >= 2, `die zweite Seite muss geholt werden (Seiten: ${bodies.length})`)
  assert.ok(Number(bodies[0].limit) >= 1000, `Seitengroesse darf die Regelmenge nicht deckeln: ${bodies[0].limit}`)
})

await t("1.200 Regeln, Ziel ohne Regel → allow (Vollstaendigkeit sperrt nicht unnoetig)", async () => {
  const points = []
  for (let i = 0; i < 1200; i++) points.push(rulePoint(`r-${i}`, `/srv/unrelated-${i}`))
  mockScroll(points)
  const { out } = await ask(makeTool(), "rm -rf /srv/nothing-protected")
  assert.strictEqual(out.verdict, "allow", JSON.stringify(out))
})

// ── Fehler auf einer spaeteren Seite: sperren, nicht durchlassen ────────────

await t("Fehler auf Seite 2 → BLOCK (fail-closed), trotz bereits gelesener Regeln", async () => {
  // Seite 1 muss voll sein (RULE_PAGE_SIZE = 1000), damit ein next_page_offset
  // gesetzt wird und `failOnPage: 2` ueberhaupt eine zweite Seite trifft.
  const points = []
  for (let i = 0; i < 1000; i++) points.push(rulePoint(`r-${i}`, `/srv/unrelated-${i}`))
  points.push(rulePoint("r-other", "/srv/other"))
  mockScroll(points, { failOnPage: 2 })
  const { out } = await ask(makeTool(), "rm -rf /srv/anything")
  assert.strictEqual(out.verdict, "block", `Teilmenge darf nie allow sein: ${JSON.stringify(out)}`)
  assert.match(out.reason ?? "", /fail-closed/i)
})

await t("Fehler auf Seite 2 mit passender Regel auf Seite 1 → BLOCK mit Regelbezug", async () => {
  const points = [rulePoint("r-0", "/srv/anything"), rulePoint("r-1", "/srv/other")]
  mockScroll(points, { failOnPage: 2 })
  const { out } = await ask(makeTool(), "rm -rf /srv/anything")
  assert.strictEqual(out.verdict, "block")
  // Die Seite-1-Regel ist sichtbar (private Sitzung) — der Block ist konkret.
  assert.strictEqual(out.matched_rules?.[0]?.source_memory_id, "r-0")
})

// ── Seitengrenze: abgeschnitten ≠ vollstaendig ──────────────────────────────

await t("ewiger Offset (Seitengrenze erreicht) → BLOCK, nicht 'keine Regeln'", async () => {
  let calls = 0
  globalThis.fetch = async () => {
    calls++
    return {
      ok: true,
      status: 200,
      json: async () => ({
        result: { points: [rulePoint("r-0", "/srv/unrelated")], next_page_offset: "endless" },
      }),
      text: async () => "",
    }
  }
  const { out } = await ask(makeTool(), "rm -rf /srv/anything")
  assert.strictEqual(out.verdict, "block", `abgeschnittener Lauf darf nie allow sein: ${JSON.stringify(out)}`)
  assert.match(out.reason ?? "", /fail-closed/i)
  assert.ok(calls > 1, "der strikte Lauf blaettert weiter als eine Seite")
})

// ── Pfad-Schutz fuer die Klassiker bleibt (kein length>2-Filter) ────────────

await t("Schutz fuer `/` blockt jeden Zielpfad darunter", async () => {
  mockScroll([rulePoint("r-root", "/")])
  const { out } = await ask(makeTool(), "rm -rf /srv/deep/dir")
  assert.strictEqual(out.verdict, "block", JSON.stringify(out))
  assert.strictEqual(out.matched_rules[0].protected_path, "/")
})

await t("Schutz fuer `/a` blockt `/a` (kurzer Pfad wird nicht verworfen)", async () => {
  mockScroll([rulePoint("r-a", "/a")])
  const { out } = await ask(makeTool(), "rm -rf /a")
  assert.strictEqual(out.verdict, "block", JSON.stringify(out))
})

globalThis.fetch = realFetch
process.exitCode = failed ? 1 : 0
