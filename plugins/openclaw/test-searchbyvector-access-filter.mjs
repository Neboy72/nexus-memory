/**
 * Regressionstest: searchByVector access-level Filter (H11, Welle 12)
 *
 * searchByVector() hatte KEINEN access-level Filter — forget-by-query konnte
 * Punkte treffen (und löschen), die der Agent-Level nicht sehen darf. Jetzt
 * derselbe Filter wie search(); forget.ts reicht _cfg.accessLevel durch.
 */
import assert from "node:assert"
import { register } from "node:module"
import { QdrantClient } from "./lib/qdrant-client.ts"

register("./_typebox-test-loader.mjs", import.meta.url)
const { registerForgetTool } = await import("./tools/forget.ts")

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

function captureBody(accessLevel) {
  let body
  globalThis.fetch = async (_url, opts = {}) => {
    body = JSON.parse(opts.body)
    return { ok: true, status: 200, json: async () => ({ result: [] }), text: async () => "" }
  }
  const client = new QdrantClient("http://localhost:6333", "nexus", 8)
  return client.searchByVector([0.1], 5, accessLevel).then(() => body)
}

await t("accessLevel 'public' → Filter any ['public']", async () => {
  const body = await captureBody("public")
  assert.strictEqual(body.filter.must[0].key, "access_level")
  assert.deepStrictEqual(body.filter.must[0].match.any, ["public"])
})

await t("accessLevel 'trusted' → Filter any ['public','trusted']", async () => {
  const body = await captureBody("trusted")
  assert.deepStrictEqual(body.filter.must[0].match.any, ["public", "trusted"])
})

await t("accessLevel 'private' → KEIN Filter (sieht alles)", async () => {
  const body = await captureBody("private")
  assert.strictEqual(body.filter, undefined)
})

await t("unbekannter accessLevel → kein Qdrant-Call (fail-closed, W31-16)", async () => {
  const body = await captureBody("geheim")
  // W31-16: empty levels return EARLY — no fetch, no filter semantics bet.
  assert.strictEqual(body, undefined, "unbekannter Level darf NICHTS sehen")
})

globalThis.fetch = realFetch

// ── forget-Tool reicht den accessLevel aus der Config durch ─────────────────

await t("forget-by-query reicht _cfg.accessLevel an searchByVector", async () => {
  let tool
  const seen = {}
  const api = { registerTool: (tt) => { tool = tt } }
  registerForgetTool(
    api,
    { embed: async () => [0.1, 0.2] },
    {
      searchByVector: async (_vec, _limit, accessLevel) => {
        seen.accessLevel = accessLevel
        return []
      },
      delete: async () => {},
    },
    { accessLevel: "public" },
  )
  await tool.execute("id", { query: "irgendwas" })
  assert.strictEqual(seen.accessLevel, "public", "accessLevel muss durchgereicht werden")
})

process.exit(failed ? 1 : 0)
