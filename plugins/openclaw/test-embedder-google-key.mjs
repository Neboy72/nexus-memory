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
// Fund W37 (low): stack + timeout statt nur e.message.
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
// Fund W37 (medium): Dimension NICHT hardcoded — aus der Struktur der Quelle
// abgeleitet (PROVIDER_DEFAULTS.google.dimensions Zeile), mit Fallback 768.
const _src = fs.readFileSync(new URL("./lib/embedder.ts", import.meta.url), "utf8")
const _m = _src.match(/google:\s*\{[^}]*dimensions:\s*(\d+)/)
const DIMS = _m ? Number(_m[1]) : 768
assert.ok(_m, "google-defaults-Zeile nicht gefunden — Struktur geaendert, Test anpassen")
const okBody = { embedding: { values: new Array(DIMS).fill(0.1) } }
// Fund W37 (low): Konstruktion zentral — jede Test-Aenderung an einer Stelle gilt überall.
function makeGoogleEmbedder() {
  return new Embedder("google", undefined, API_KEY, undefined, undefined)
}

await t("primärer Request trägt x-goog-api-key und keine key-Query", async () => {
  const calls = mockFetch(() => ({
    ok: true,
    status: 200,
    json: async () => okBody,
    text: async () => "",
  }))
  const emb = makeGoogleEmbedder()
  const vec = await emb.embed("hallo")
  assert.strictEqual(vec.length, DIMS)
  assert.strictEqual(vec[0], 0.1)
  assert.strictEqual(calls.length, 1)
  // Fund W37 (low): Headers-Instanz ODER plain object — beides korrekt lesen.
  const h = calls[0].opts.headers
  const got = typeof h?.get === "function" ? h.get("x-goog-api-key") : h?.["x-goog-api-key"]
  assert.strictEqual(got, API_KEY)
  assert.ok(!calls[0].url.includes("key="), "Key darf nicht in der URL stehen")
})

await t("400 → KEIN Retry (genau ein fetch)", async () => {
  const calls = mockFetch(() => ({
    ok: false,
    status: 400,
    json: async () => ({}),
    text: async () => "bad request",
  }))
  const emb = makeGoogleEmbedder()
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
  const emb = makeGoogleEmbedder()
  await assert.rejects(() => emb.embed("hallo"), /Google embedding failed: 403/)
  assert.strictEqual(calls.length, 1)
})

await t("strukturell: kein retryUrl-Block mehr in der Quelle", () => {
  const src = fs.readFileSync(new URL("./lib/embedder.ts", import.meta.url), "utf8")
  assert.ok(src.includes("x-goog-api-key"), "Header fehlt in der Quelle")
  assert.ok(!src.includes("retryUrl"), "der 400-Retry-Block muss entfernt sein")
})

// Fund W37 (medium): 401 ist ein realistischer invalid-key-Response — gleiches
// Verhalten wie 400/403: sofortiger Fehler, KEIN Retry.
await t("401 → Fehler mit echter Response, kein Retry", async () => {
  const calls = mockFetch(() => ({
    ok: false,
    status: 401,
    json: async () => ({}),
    text: async () => "unauthorized",
  }))
  const emb = makeGoogleEmbedder()
  await assert.rejects(() => emb.embed("hallo"), /Google embedding failed: 401/)
  assert.strictEqual(calls.length, 1, "401 darf nicht retried werden")
})

// Fund W37 (medium): fetch-Restore auch bei Crash (exit-Hook) + exitCode statt
// process.exit (Hard-Exit kappt gepufferte stdout-Diagnosen in CI).
process.on("exit", () => { globalThis.fetch = realFetch })
globalThis.fetch = realFetch
process.exitCode = failed ? 1 : 0
