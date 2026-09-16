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

// HOME VOR dem Import setzen: CACHE_FILE wird beim Modul-Load daraus gebaut.
// Fund W37 (medium): Original-HOME sichern + Temp-Dir später räumen — ohne das
// leakt jeder Lauf ein nexus-upd-* Verzeichnis und das mutierte HOME bleibt
// für alles Weitere im Prozess umgestellt.
const REAL_HOME = process.env.HOME
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

await t("Müll-Tag → latest '', available:false, NICHT gecacht (W37-Fix)", async () => {
  clearCache()
  mockFetch({ tag: "not-a-version" })
  const res = await checkForUpdate()
  assert.strictEqual(res.available, false)
  assert.strictEqual(res.latest, "")
  // Fund W37 (medium, KONTRAKT): vorher wurde latest:'' für 24h gecacht und versteckte
  // echte Releases im TTL-Fenster (403/404 warfen, Müll-Tag nicht). update-check.ts
  // cached jetzt NUR bei nutzbarer Version — konsistent mit dem Header-Vertrag
  // "a garbage tag can never be cached as a version".
  assert.ok(!fs.existsSync(CACHE), "Müll-Tag darf den 24h-Cache nicht besetzen")
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

// Fund W37 (medium): ALLE neuen Fail-Pfade des "any error → no update"-Vertrags.
await t("rejecting fetch (Netzwerk-Fehler) → available:false, kein Cache", async () => {
  clearCache()
  fetchCalls = 0
  globalThis.fetch = async () => { fetchCalls++; throw new Error("ECONNREFUSED") }
  const res = await checkForUpdate()
  assert.strictEqual(res.available, false)
  assert.strictEqual(fetchCalls, 1)
  assert.ok(!fs.existsSync(CACHE), "Netz-Fehler darf NICHT gecacht werden")
})

await t("5xx-Response → available:false, kein Cache", async () => {
  clearCache()
  mockFetch({ ok: false, status: 503 })
  const res = await checkForUpdate()
  assert.strictEqual(res.available, false)
  assert.ok(!fs.existsSync(CACHE), "5xx darf NICHT gecacht werden")
})

await t("abgelaufener Cache (TTL) → neuer fetch", async () => {
  const STALE = Date.now() - 25 * 60 * 60 * 1000 // 25h > 24h TTL
  writeCache({ checkedAt: STALE, latest: "1.0.0", url: "" })
  mockFetch({ tag: "v9.9.9" })
  const res = await checkForUpdate()
  assert.strictEqual(fetchCalls, 1, "abgelaufener Cache MUSS neu fetchen")
  assert.strictEqual(res.available, true)
})

await t("isNewerVersion: non-string Input wirft nicht", () => {
  // Fund W37 (medium): isNewerVersion ruft v.replace(...) direkt — nicht-string
  // Inputs (realistisch für ungeprüfte Cache-Werte) müssen fail-open bleiben.
  for (const bad of [null, undefined, {}, 42]) {
    assert.doesNotThrow(() => isNewerVersion(bad, "1.0.0"))
    assert.strictEqual(isNewerVersion(bad, "1.0.0"), false)
  }
  // "1.2" → [1,2,0] ist dokumentiertes Padding-Verhalten (kein Bug); Muell-Strings
  // und x-Platzhalter müssen fail-open sein.
  for (const odd of ["", "v", "1.2.x", "1.2.beta"]) {
    assert.strictEqual(isNewerVersion(odd, "1.0.0"), false, "odd input '" + odd + "'")
  }
})

// Fund W37 (medium): fetch-Restore IMMER (auch bei Crash), HOME restore + Temp-Cleanup,
// exitCode statt process.exit (Hard-Exit kappt gepufferte stdout-Diagnosen in CI).
process.on("exit", () => { globalThis.fetch = realFetch })
if (REAL_HOME !== undefined) process.env.HOME = REAL_HOME
try { fs.rmSync(HOME, { recursive: true, force: true }) } catch { /* best-effort */ }
process.exitCode = failed ? 1 : 0
