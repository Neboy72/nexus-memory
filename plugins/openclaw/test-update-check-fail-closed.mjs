/**
 * Regressionstest: update-check fail-open-Vertrag (H12, Welle 12)
 *
 * fetchLatest prüfte res.ok nie (403/404 → latest:"" für 24h gecacht);
 * readCache prüfte nur die TTL, nicht die Shape; isNewerVersion stand
 * außerhalb des try/catch. Der dokumentierte Vertrag "any error → no update"
 * muss halten, und ein Müll-Cache darf nie eine Exception auslösen.
 */
import assert from "node:assert"
import fs from "node:fs"
import os from "node:os"
import path from "node:path"

let failed = 0
const t = (name, fn) =>
  Promise.resolve()
    .then(fn)
    .then(() => console.log("PASS ", name))
    .catch((e) => {
      failed++
      console.log("FAIL ", name, "—", e.message)
    })

// HOME VOR dem Import setzen: CACHE_FILE wird beim Modul-Load daraus gebaut.
const HOME = fs.mkdtempSync(path.join(os.tmpdir(), "nexus-upd-"))
process.env.HOME = HOME
const CACHE = path.join(HOME, ".nexus-memory", "update-check-cache.json")

const { checkForUpdate, isNewerVersion } = await import("./lib/update-check.ts")

const realFetch = globalThis.fetch
let fetchCalls = 0

function mockFetch({ ok = true, status = 200, tag = "v9.9.9", url = "" } = {}) {
  fetchCalls = 0
  globalThis.fetch = async () => {
    fetchCalls++
    return {
      ok,
      status,
      json: async () => ({ tag_name: tag, html_url: url }),
      text: async () => "",
    }
  }
}

function writeCache(obj) {
  fs.mkdirSync(path.dirname(CACHE), { recursive: true })
  fs.writeFileSync(CACHE, typeof obj === "string" ? obj : JSON.stringify(obj))
}

function clearCache() {
  try { fs.unlinkSync(CACHE) } catch { /* absent */ }
}

await t("403 → available:false, kein Wurf, Cache bleibt unbeschrieben", async () => {
  clearCache()
  mockFetch({ ok: false, status: 403 })
  const res = await checkForUpdate()
  assert.strictEqual(res.available, false)
  assert.strictEqual(fetchCalls, 1)
  assert.ok(!fs.existsSync(CACHE), "ein 403 darf NICHT gecacht werden")
})

await t("unparsbarer Cache → Fallback auf fetch", async () => {
  writeCache("{ das ist kein json")
  mockFetch({ tag: "v9.9.9" })
  const res = await checkForUpdate()
  assert.strictEqual(fetchCalls, 1, "Müll-Cache → fetch")
  assert.strictEqual(res.available, true)
})

await t("Cache mit falscher Shape (latest=Objekt) → Fallback auf fetch", async () => {
  clearCache()
  writeCache({ checkedAt: Date.now(), latest: { evil: 1 }, url: "x" })
  mockFetch({ tag: "v9.9.9" })
  const res = await checkForUpdate()
  assert.strictEqual(fetchCalls, 1, "Shape-Verstoß → fetch statt isNewerVersion")
  assert.strictEqual(res.available, true)
})

await t("Müll-Tag → latest '', available:false (nicht gecacht als Version)", async () => {
  clearCache()
  mockFetch({ tag: "not-a-version" })
  const res = await checkForUpdate()
  assert.strictEqual(res.available, false)
  assert.strictEqual(res.latest, "")
  assert.strictEqual(JSON.parse(fs.readFileSync(CACHE, "utf8")).latest, "")
})

await t("gültiger frischer Cache → kein fetch", async () => {
  writeCache({ checkedAt: Date.now(), latest: "9.9.9", url: "https://example" })
  mockFetch()
  const res = await checkForUpdate()
  assert.strictEqual(fetchCalls, 0, "frischer Cache → kein Netz-Call")
  assert.strictEqual(res.available, true)
  assert.strictEqual(res.url, "https://example")
})

await t("isNewerVersion wirft bei Müll nicht", () => {
  assert.doesNotThrow(() => isNewerVersion("nonsense", "1.0.0"))
  assert.strictEqual(isNewerVersion("nonsense", "1.0.0"), false)
})

globalThis.fetch = realFetch
process.exit(failed ? 1 : 0)
