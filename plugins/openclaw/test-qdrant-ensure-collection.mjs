/**
 * Regressionstest: ensureCollection darf nie automatisch löschen (A3)
 *
 * Bei einem Dimension-Mismatch hat ensureCollection() früher per
 * `DELETE /collections/{name}` die Sammlung gelöscht und neu erstellt
 * (Datenverlust). Jetzt muss es werfen, außer allowRecreate=true UND ein
 * Backup-Pfad sind explizit angegeben.
 */
import assert from "node:assert"
import { QdrantClient } from "./lib/qdrant-client.ts"

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

/** fetch-Mock: Sammlung existiert mit `currentDim`, sammelt alle Methoden+URLs. */
function mockFetch(currentDim, calls) {
  globalThis.fetch = async (url, opts = {}) => {
    const method = opts.method ?? "GET"
    const u = String(url)
    calls.push({ url: u, method, body: opts.body ? String(opts.body) : null })
    // Fund W37 (medium): Mock respektiert die URL — GET auf die konkrete Collection
    // liefert die Dimension, ALLES andere (PUT /collections/…, DELETE, …) antwortet
    // generisch. Ein Regression, das die falsche Collection adressiert, fällt auf.
    if (method === "GET" && /\/collections\/[^/]+$/.test(u)) {
      return {
        ok: true,
        status: 200,
        json: async () => ({ result: { config: { params: { vectors: { size: currentDim } } } } }),
        text: async () => "",
      }
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

await t("explizites allowRecreate + Backup-Pfad → DELETE erlaubt", async () => {
  const calls = []
  mockFetch(1024, calls)
  const client = new QdrantClient("http://localhost:6333", "nexus", 1024)
  await client.ensureCollection(2048, true, "/tmp/nexus-backup.snapshot")
  // Fund W37 (medium): nicht nur irgendein DELETE — die Recreate-Kette muss
  // DELETE auf DIESE Collection + nachfolgende PUT (recreate) zeigen.
  const del = calls.find((c) => c.method === "DELETE" && /\/collections\/nexus$/.test(c.url))
  assert.ok(del, "mit allowRecreate+Backup muss DELETE auf /collections/nexus kommen")
  const put = calls.find((c) => c.method === "PUT" && /\/collections\/nexus$/.test(c.url))
  assert.ok(put, "nach dem DELETE muss die Collection neu erstellt werden (PUT)")
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

// Fund W37 (medium): fetch-Restore auch bei Crash + exitCode statt process.exit.
process.on("exit", () => { globalThis.fetch = realFetch })
globalThis.fetch = realFetch
process.exitCode = failed ? 1 : 0
