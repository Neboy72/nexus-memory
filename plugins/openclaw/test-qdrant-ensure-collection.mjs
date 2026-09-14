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
const t = (name, fn) =>
  Promise.resolve()
    .then(fn)
    .then(() => console.log("PASS ", name))
    .catch((e) => {
      failed++
      console.log("FAIL ", name, "—", e.message)
    })

const realFetch = globalThis.fetch

/** fetch-Mock: Sammlung existiert mit `currentDim`, sammelt alle Methoden. */
function mockFetch(currentDim, calls) {
  globalThis.fetch = async (url, opts = {}) => {
    const method = opts.method ?? "GET"
    calls.push({ url: String(url), method })
    if (method === "GET") {
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
  assert.ok(
    !calls.some((c) => c.method === "DELETE"),
    "es darf KEIN DELETE gesendet werden",
  )
})

await t("explizites allowRecreate + Backup-Pfad → DELETE erlaubt", async () => {
  const calls = []
  mockFetch(1024, calls)
  const client = new QdrantClient("http://localhost:6333", "nexus", 1024)
  await client.ensureCollection(2048, true, "/tmp/nexus-backup.snapshot")
  assert.ok(
    calls.some((c) => c.method === "DELETE"),
    "mit allowRecreate+Backup muss neu erstellt werden",
  )
})

await t("allowRecreate ohne Backup-Pfad → wirft, kein DELETE", async () => {
  const calls = []
  mockFetch(1024, calls)
  const client = new QdrantClient("http://localhost:6333", "nexus", 1024)
  await assert.rejects(() => client.ensureCollection(2048, true))
  assert.ok(!calls.some((c) => c.method === "DELETE"), "ohne Backup kein DELETE")
})

await t("passende Dimension → nichts tun", async () => {
  const calls = []
  mockFetch(1024, calls)
  const client = new QdrantClient("http://localhost:6333", "nexus", 1024)
  await client.ensureCollection(1024)
  assert.ok(!calls.some((c) => c.method === "DELETE"), "kein DELETE bei passender Dimension")
})

globalThis.fetch = realFetch
process.exit(failed ? 1 : 0)
