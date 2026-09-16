/**
 * Regressionstest: access-level fail-closed (H6, Welle 12)
 *
 * Drei Stellen degradierten unbekannte access_level stillschweigend zu
 * "public" (fail-OPEN): recall.ts (indexOf -1 > agentIdx nie true),
 * qdrant-client.ts visibleAccessLevels (`?? 0`) und store.ts (Runtime-Cast
 * ohne Validierung). Alle drei müssen jetzt fail-CLOSED sein.
 */
import assert from "node:assert"
import fs from "node:fs"
import { register } from "node:module"

register("./_typebox-test-loader.mjs", import.meta.url)
// Fund W36 (low): Import-Fehler klar melden statt unhandled rejection (CI-Log tot).
let registerStoreTool, QdrantClient
try {
  ;({ registerStoreTool } = await import("./tools/store.ts"))
  ;({ QdrantClient } = await import("./lib/qdrant-client.ts"))
} catch (e) {
  console.error("Modul-Import fehlgeschlagen (Loader/Source kaputt?):", e.stack || e)
  process.exitCode = 1
  // Kein weiterer Test kann laufen — trotzdem geordnetes Ende.
  throw e
}

let failed = 0
const t = async (name, fn) => {
  try {
    await Promise.race([
      Promise.resolve().then(fn),
      new Promise((_, rej) => setTimeout(() => rej(new Error("timeout: test hing 10s")), 10000)),
    ])
    console.log("PASS ", name)
  } catch (e) {
    failed++
    console.log("FAIL ", name, "—", (e && e.stack) || String(e))
  }
}

const realFetch = globalThis.fetch

// ── (a) recall.ts: unbekannter tpAccess wird geskippt ────────────────────────

await t("recall.ts skippt unbekannten tpAccess (memIdx === -1) statt public", () => {
  const src = fs.readFileSync(new URL("./hooks/recall.ts", import.meta.url), "utf8")
  assert.match(src, /memIdx === -1/, "Skip-Bedingung `memIdx === -1` fehlt")
  assert.ok(
    !/tpPayload\.access_level as string\) \|\| "public"/.test(src),
    'das `|| "public"` an der tpAccess-Stelle muss entfernt sein',
  )
})

// ── (b) qdrant-client.ts: unknown level → nichts sichtbar ────────────────────

async function captureSearchBody(accessLevel) {
  let body
  const realFetch = globalThis.fetch
  // Fund W36 (medium): Patch+Restore IMMER paarweise (try/finally) — ein Reject in
  // client.search() liess sonst den Mock haengen und faerbte auf Folgetests ab.
  globalThis.fetch = async (_url, opts = {}) => {
    body = JSON.parse(opts.body)
    return { ok: true, status: 200, json: async () => ({ result: [] }), text: async () => "" }
  }
  try {
    const client = new QdrantClient("http://localhost:6333", "nexus", 8)
    await client.search([0.1], 5, accessLevel)
    return body
  } finally {
    globalThis.fetch = realFetch
  }
}

await t("search: public → Filter any ['public']", async () => {
  const body = await captureSearchBody("public")
  // Fund W36 (medium): Shape-guard statt TypeError beim Payload-Drift.
  const any = body?.filter?.must?.[0]?.match?.any
  assert.ok(Array.isArray(any), "Filter-Shape unerwartet: " + JSON.stringify(body))
  assert.deepStrictEqual(any, ["public"])
})

await t("search: unbekannter Level → kein Qdrant-Call (fail-closed, W31-16)", async () => {
  const body = await captureSearchBody("geheim")
  // W31-16: empty levels return EARLY — no fetch, no filter semantics bet.
  assert.strictEqual(body, undefined, "unbekannter Level darf NICHTS sehen")
})

// Fund W36 (medium): behavioural-Ergänzung zu den white-box-Greps in (a): der ECHTE
// Filter baut nur aus gültigen Levels — ein tpAccess-Fall-through zu 'public' würde
// hier als zusätzliche 'public'-Stufe im any-Array sichtbar.
await t("search: trusted → Filter any [public, trusted], KEIN Fall-through-Level", async () => {
  const body = await captureSearchBody("trusted")
  const any = body?.filter?.must?.[0]?.match?.any
  assert.ok(Array.isArray(any), "Filter-Shape unerwartet: " + JSON.stringify(body))
  assert.deepStrictEqual([...any].sort(), ["public", "trusted"])
  // Fund W36 (medium): behavioural-Ergänzung zu den white-box-Greps in (a) — der
  // ECHTE Filter besteht nur aus Order-konsistenten Levels; ein tpAccess-Fall-through
  // zu 'public' bei 'geheim' wäre bereits durch den early-return gedeckt, ein
  // stummer public-Zusatz bei bekannten Levels schlägt hier an.
  assert.ok(any.every((v) => ["public", "trusted", "private"].includes(v)), "unbekannter Level im Filter: " + JSON.stringify(any))
})

globalThis.fetch = realFetch

// ── (c) store.ts: unbekannte Enum-Werte werden abgelehnt ─────────────────────

function makeStoreTool(cfg = { accessLevel: "public" }) {
  let tool
  const touched = { embed: 0, upsert: 0 }
  const api = { registerTool: (tt) => { tool = tt } }
  // Fund W36 (low): tool-Fang validieren — wenn registerStoreTool die Registrierung
  // aendert, failt der Test klipp statt mit opaque TypeError an jeder Stelle.
  registerStoreTool(
    api,
    { embed: async () => { touched.embed++; return [0.1, 0.2] } },
    { upsert: async () => { touched.upsert++ } },
    cfg,
  )
  assert.ok(tool && typeof tool.execute === "function", "registerStoreTool hat keinen Tool registriert (API-Vertrag geaendert?)")
  return { tool, touched }
}

await t("store: access_level='geheim' → Fehler, kein embed/upsert", async () => {
  const { tool, touched } = makeStoreTool()
  const res = await tool.execute("id", { text: "x", access_level: "geheim" })
  const text = res.content[0].text
  assert.match(text, /invalid access_level/i, text)
  assert.match(text, /public/, "erlaubte Werte müssen genannt werden")
  assert.strictEqual(touched.embed, 0, "embedder darf nicht berührt werden")
  assert.strictEqual(touched.upsert, 0, "qdrant darf nicht berührt werden")
})

await t("store: category='bogus' → Fehler, kein embed/upsert", async () => {
  const { tool, touched } = makeStoreTool()
  const res = await tool.execute("id", { text: "x", category: "bogus" })
  const text = res.content[0].text
  assert.match(text, /invalid category/i, text)
  assert.strictEqual(touched.embed, 0)
  assert.strictEqual(touched.upsert, 0)
})

await t("store: gültige Werte → upsert", async () => {
  const { tool, touched } = makeStoreTool()
  const res = await tool.execute("id", { text: "x", category: "fact", access_level: "trusted" })
  assert.match(res.content[0].text, /Stored/i)
  assert.strictEqual(touched.upsert, 1)
  // Fund W36 (low): embed genau 1x (kein Skip, kein Doppel-Call).
  assert.strictEqual(touched.embed, 1, "embed muss genau 1x laufen — got: " + touched.embed)
})

await t("store: fehlender access_level → cfg-Fallback (kein Fehler)", async () => {
  const { tool, touched } = makeStoreTool({ accessLevel: "trusted" })
  const res = await tool.execute("id", { text: "x" })
  assert.match(res.content[0].text, /Stored/i)
  assert.strictEqual(touched.upsert, 1)
  // Fund W36 (medium): Regression-Wache — Fallback muss WIRKLICH cfg.accessLevel
  // sein (hier 'trusted'), nicht stillschweigend 'public'. store.ts echoet die
  // effektiven Werte im Result-Text.
  assert.match(res.content[0].text, /trusted/i, "effektiver access_level muss im Result stehen: " + res.content[0].text)
})

// Fund W36 (low): exitCode statt process.exit — Hard-Exit kappt gepufferte
// stdout-Ausgabe (PASS/FAIL-Diagnosen!) bei gepipestem stdout. Natuerliches Ende flushes.
process.exitCode = failed ? 1 : 0
