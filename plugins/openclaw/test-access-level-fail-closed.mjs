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
const { registerStoreTool } = await import("./tools/store.ts")
const { QdrantClient } = await import("./lib/qdrant-client.ts")

let failed = 0
const t = (name, fn) =>
  Promise.resolve()
    .then(fn)
    .then(() => console.log("PASS ", name))
    .catch((e) => {
      failed++
      console.log("FAIL ", name, "—", e.message)
    })

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

function captureSearchBody(accessLevel) {
  let body
  globalThis.fetch = async (_url, opts = {}) => {
    body = JSON.parse(opts.body)
    return { ok: true, status: 200, json: async () => ({ result: [] }), text: async () => "" }
  }
  const client = new QdrantClient("http://localhost:6333", "nexus", 8)
  return client.search([0.1], 5, accessLevel).then(() => body)
}

await t("search: public → Filter any ['public']", async () => {
  const body = await captureSearchBody("public")
  assert.deepStrictEqual(body.filter.must[0].match.any, ["public"])
})

await t("search: unbekannter Level → kein Qdrant-Call (fail-closed, W31-16)", async () => {
  const body = await captureSearchBody("geheim")
  // W31-16: empty levels return EARLY — no fetch, no filter semantics bet.
  assert.strictEqual(body, undefined, "unbekannter Level darf NICHTS sehen")
})

globalThis.fetch = realFetch

// ── (c) store.ts: unbekannte Enum-Werte werden abgelehnt ─────────────────────

function makeStoreTool(cfg = { accessLevel: "public" }) {
  let tool
  const touched = { embed: 0, upsert: 0 }
  const api = { registerTool: (tt) => { tool = tt } }
  registerStoreTool(
    api,
    { embed: async () => { touched.embed++; return [0.1, 0.2] } },
    { upsert: async () => { touched.upsert++ } },
    cfg,
  )
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
})

await t("store: fehlender access_level → cfg-Fallback (kein Fehler)", async () => {
  const { tool, touched } = makeStoreTool({ accessLevel: "trusted" })
  const res = await tool.execute("id", { text: "x" })
  assert.match(res.content[0].text, /Stored/i)
  assert.strictEqual(touched.upsert, 1)
})

process.exit(failed ? 1 : 0)
