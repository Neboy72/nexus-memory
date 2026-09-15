/**
 * Regressionstest: Google-Embedding API-Key (H8, Welle 12)
 *
 * Der primäre fetch schickte KEINEN Key-Header — der 400-Retry griff nie
 * (fehlender Key liefert 401/403) und legte den Key als ?key= in die URL.
 * Jetzt: x-goog-api-key-Header, kein Retry mehr.
 */
import assert from "node:assert"
import fs from "node:fs"
import { Embedder } from "./lib/embedder.ts"

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
const API_KEY = "test-key-123"

function mockFetch(handler) {
  const calls = []
  globalThis.fetch = async (url, opts = {}) => {
    calls.push({ url: String(url), opts })
    return handler(String(url), opts)
  }
  return calls
}

// Google-Default-Dimension 768: validateVector (Nr 365, W21) wirft sonst.
const okBody = { embedding: { values: new Array(768).fill(0.1) } }

await t("primärer Request trägt x-goog-api-key und keine key-Query", async () => {
  const calls = mockFetch(() => ({
    ok: true,
    status: 200,
    json: async () => okBody,
    text: async () => "",
  }))
  const emb = new Embedder("google", undefined, API_KEY, undefined, undefined)
  const vec = await emb.embed("hallo")
  assert.strictEqual(vec.length, 768)
  assert.strictEqual(vec[0], 0.1)
  assert.strictEqual(calls.length, 1)
  assert.strictEqual(calls[0].opts.headers["x-goog-api-key"], API_KEY)
  assert.ok(!calls[0].url.includes("key="), "Key darf nicht in der URL stehen")
})

await t("400 → KEIN Retry (genau ein fetch)", async () => {
  const calls = mockFetch(() => ({
    ok: false,
    status: 400,
    json: async () => ({}),
    text: async () => "bad request",
  }))
  const emb = new Embedder("google", undefined, API_KEY, undefined, undefined)
  await assert.rejects(() => emb.embed("hallo"), /Google embedding failed: 400/)
  assert.strictEqual(calls.length, 1, "der alte 400-Retry muss entfernt sein")
})

await t("403 → Fehler mit echter Response, kein Retry", async () => {
  const calls = mockFetch(() => ({
    ok: false,
    status: 403,
    json: async () => ({}),
    text: async () => "forbidden",
  }))
  const emb = new Embedder("google", undefined, API_KEY, undefined, undefined)
  await assert.rejects(() => emb.embed("hallo"), /Google embedding failed: 403/)
  assert.strictEqual(calls.length, 1)
})

await t("strukturell: kein retryUrl-Block mehr in der Quelle", () => {
  const src = fs.readFileSync(new URL("./lib/embedder.ts", import.meta.url), "utf8")
  assert.ok(src.includes("x-goog-api-key"), "Header fehlt in der Quelle")
  assert.ok(!src.includes("retryUrl"), "der 400-Retry-Block muss entfernt sein")
})

globalThis.fetch = realFetch
process.exit(failed ? 1 : 0)
