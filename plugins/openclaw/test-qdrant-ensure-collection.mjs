/**
 * Regressionstest: ensureCollection darf nie automatisch löschen (A3)
 *
 * Bei einem Dimension-Mismatch hat ensureCollection() früher per
 * `DELETE /collections/{name}` die Sammlung gelöscht und neu erstellt
 * (Datenverlust). Jetzt muss es werfen, außer allowRecreate=true UND ein
 * Backup-Pfad sind explizit angegeben.
 */
import assert from "node:assert"
// OCR-6 (test medium, Z308): the backup fixtures lived on fixed shared /tmp
// paths and were never cleaned up — parallel runs collided, leftovers
// polluted later runs, and /tmp does not exist on Windows. Unique temp dir
// per run (os.tmpdir()-based), cleaned up at the end.
import { writeFileSync, mkdtempSync, rmSync } from "node:fs"
import { tmpdir } from "node:os"
import { join } from "node:path"
import { QdrantClient } from "./lib/qdrant-client.ts"

const fixtureDir = mkdtempSync(join(tmpdir(), "nexus-ensure-test-"))
const backupPath = join(fixtureDir, "backup.snapshot")
const backupEmptyPath = join(fixtureDir, "backup-empty.snapshot")
const backupMissingPath = join(fixtureDir, "backup-MISSING.snapshot")

const realFetch = globalThis.fetch

let failed = 0
// Fund W37 (low): stack + timeout statt nur e.message.
const t = async (name, fn) => {
  // OCR-6 (bug medium): the 10s safety timer was never cleared — every test
  // kept a pending setTimeout alive (6 × 10s tail on the run) and, when a
  // test DID hit the timeout, its body kept running against later mocks.
  // Keep the handle, clear it in finally, and abort the body on timeout.
  let timer
  let timedOut = false
  try {
    await Promise.race([
      Promise.resolve().then(fn),
      new Promise((_, rej) => {
        timer = setTimeout(() => {
          timedOut = true
          rej(new Error("timeout: test hing 10s"))
        }, 10000)
      }),
    ])
    console.log("PASS ", name)
  } catch (e) {
    failed++
    console.log("FAIL ", name, "—", (e && e.stack) || String(e))
  } finally {
    clearTimeout(timer)
    // OCR-6 (maintainability low, L283): restore realFetch in EVERY t()'s
    // finally — a crashing test body can no longer leave the mock installed
    // for later tests (cross-suite poisoning).
    globalThis.fetch = realFetch
  }
  if (timedOut) throw new Error("timeout abort") // stops follow-up side effects
}


/** fetch-Mock: Sammlung existiert mit `currentDim`, sammelt alle Methoden+URLs. */
function mockFetch(currentDim, calls, opts = {}) {
  // OCR-6 (test low, L296): the GET matcher was too permissive for the
  // comment's claim — /\/collections\/[^/]+$/ matched EVERY collection name,
  // so a regression addressing the wrong collection still got currentDim and
  // the test stayed green. Match the exact collection (nexus) and answer 404
  // for unknown GETs (a genuinely-missing collection), per L296.
  const collection = opts.collection ?? "nexus"
  const getStatus = opts.getStatus ?? 200
  globalThis.fetch = async (url, o = {}) => {
    const method = o.method ?? "GET"
    const u = String(url)
    calls.push({ url: u, method, body: o.body ? String(o.body) : null })
    if (method === "GET" && u.endsWith(`/collections/${collection}`)) {
      if (getStatus !== 200) {
        return { ok: false, status: getStatus, json: async () => ({}), text: async () => `err ${getStatus}` }
      }
      return {
        ok: true,
        status: 200,
        json: async () => ({ result: { config: { params: { vectors: { size: currentDim } } } } }),
        text: async () => "",
      }
    }
    if (method === "GET") {
      // unknown collection → 404 (was: generic 200 — L296)
      return { ok: false, status: 404, json: async () => ({}), text: async () => "Not Found" }
    }
    return { ok: true, status: 200, json: async () => ({ result: true }), text: async () => "" }
  }
}

await t("Dimension-Mismatch → wirft und löscht NICHT", async () => {
  const calls = []
  mockFetch(1024, calls)
  const client = new QdrantClient("http://localhost:6333", "nexus", 1024)
  await assert.rejects(
    () => client.ensureCollection(2048),
    /dimensions=1024, expected=2048/,
    "Mismatch muss mit klarer Meldung werfen",
  )
  // Fund W37 (medium): DELETE-Verbot praezisiert — KEIN schreibender Call aller Art.
  assert.ok(
    !calls.some((c) => c.method !== "GET"),
    `es darf ausschliesslich der Existenz-GET fliegen: ${JSON.stringify(calls)}`,
  )
})

// OCR-5 (security medium, /tmp/z714): "verified backup" ist jetzt wirklich
// verifiziert — der Pfad muss existieren und nicht-leer sein, BEVOR der
// DELETE läuft. Der Test schreibt einen echten (nicht-leeren) Snapshot.
await t("explizites allowRecreate + ECHTER Backup-Pfad → DELETE erlaubt", async () => {
  const calls = []
  mockFetch(1024, calls)
  writeFileSync(backupPath, "snapshot-data", "utf8")
  const client = new QdrantClient("http://localhost:6333", "nexus", 1024)
  await client.ensureCollection(2048, true, backupPath)
  // Fund W37 (medium): nicht nur irgendein DELETE — die Recreate-Kette muss
  // DELETE auf DIESE Collection + nachfolgende PUT (recreate) zeigen.
  const del = calls.find((c) => c.method === "DELETE" && /\/collections\/nexus$/.test(c.url))
  assert.ok(del, "mit allowRecreate+Backup muss DELETE auf /collections/nexus kommen")
  const put = calls.find((c) => c.method === "PUT" && /\/collections\/nexus$/.test(c.url))
  assert.ok(put, "nach dem DELETE muss die Collection neu erstellt werden (PUT)")
})

await t("allowRecreate + FEHLENDER Backup-Pfad → wirft, kein DELETE (OCR-5)", async () => {
  const calls = []
  mockFetch(1024, calls)
  const client = new QdrantClient("http://localhost:6333", "nexus", 1024)
  await assert.rejects(
    () => client.ensureCollection(2048, true, backupMissingPath),
    /refusing recreate: backupPath.*does not exist/,
  )
  assert.ok(!calls.some((c) => c.method === "DELETE"), "ohne echten Backup darf KEIN DELETE fliegen")
})

await t("allowRecreate + LEERER Backup (0 bytes) → wirft, kein DELETE (OCR-5)", async () => {
  const calls = []
  mockFetch(1024, calls)
  writeFileSync(backupEmptyPath, "", "utf8")
  const client = new QdrantClient("http://localhost:6333", "nexus", 1024)
  await assert.rejects(
    () => client.ensureCollection(2048, true, backupEmptyPath),
    /refusing recreate: backup .* is empty/,
  )
  assert.ok(!calls.some((c) => c.method === "DELETE"), "mit leerem Backup darf kein DELETE fliegen")
})

await t("allowRecreate ohne Backup-Pfad → wirft, kein DELETE", async () => {
  const calls = []
  mockFetch(1024, calls)
  const client = new QdrantClient("http://localhost:6333", "nexus", 1024)
  // Fund W37 (medium): Matcher an den dokumentierten Fehler — nicht "irgendwas warf".
  await assert.rejects(
    () => client.ensureCollection(2048, true),
    /backup/i,
    "ohne Backup-Pfad muss die Backup-Bedingung greifen",
  )
  assert.ok(!calls.some((c) => c.method !== "GET"), "ohne Backup kein schreibender Call")
})

await t("passende Dimension → nichts tun", async () => {
  const calls = []
  mockFetch(1024, calls)
  const client = new QdrantClient("http://localhost:6333", "nexus", 1024)
  await client.ensureCollection(1024)
  assert.ok(calls.length > 0, "vacuum-Guard: es MUSS mindestens der Existenz-GET geflogen sein")
  // Fund W37 (medium): nicht nur DELETE-Abwesenheit — auch kein schreibender Call.
  const writes = calls.filter((c) => c.method !== "GET")
  assert.strictEqual(writes.length, 0, `kein schreibender Call bei passender Dimension: ${JSON.stringify(writes)}`)
})

// OCR-6 (test low, L341): the mock answered every GET with 200, so the
// production-commonest path — collection ABSENT (404) → PUT/create — was
// never exercised, nor the deliberate throw on a 5xx/4xx existence check.
await t("Collection fehlt (404) → genau EIN PUT (create), kein DELETE (OCR-6 L341)", async () => {
  const calls = []
  mockFetch(1024, calls, { getStatus: 404 })
  const client = new QdrantClient("http://localhost:6333", "nexus", 1024)
  await client.ensureCollection(1024)
  const puts = calls.filter((c) => c.method === "PUT" && c.url.endsWith("/collections/nexus"))
  assert.strictEqual(puts.length, 1, `genau ein create-PUT erwartet: ${JSON.stringify(calls)}`)
  assert.ok(!calls.some((c) => c.method === "DELETE"), "kein DELETE beim Erst-Create")
})
await t("Existenz-Check 500 → wirft, kein PUT/DELETE (OCR-6 L341)", async () => {
  const calls = []
  mockFetch(1024, calls, { getStatus: 500 })
  const client = new QdrantClient("http://localhost:6333", "nexus", 1024)
  await assert.rejects(() => client.ensureCollection(1024), /collection check failed: 500/)
  assert.ok(!calls.some((c) => c.method !== "GET"), `nur der GET darf fliegen: ${JSON.stringify(calls)}`)
})

// Fund W37 (medium): fetch-Restore + exitCode statt process.exit.
// OCR-6 (maintainability low, L283): the process.on("exit") handler was dead
// code — the line below restores realFetch synchronously, so the handler
// could only re-assign the same value; and it protected nothing (a crash
// outside t() leaves the mock installed until process end). Removed: each
// t() restores the mock in its finally (see below), so no cross-suite leak.
globalThis.fetch = realFetch
process.exitCode = failed ? 1 : 0

// OCR-6 (test medium, Z308): clean up the fixture directory.
rmSync(fixtureDir, { recursive: true, force: true })
