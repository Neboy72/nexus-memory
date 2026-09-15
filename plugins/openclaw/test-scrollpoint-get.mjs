/**
 * Regressionstest: scrollPoint per Point-Retrieve (H137, Welle 13)
 *
 * Vorher: POST /points/scroll mit `must: [{ key: "id", match: { value: id } }]`.
 * Qdrant matcht Filter gegen das PAYLOAD — die native Point-ID liegt aber
 * ausserhalb des Payloads. Der Filter traf nie, scrollPoint lieferte immer
 * null, und graph_traverse/get_related hielten jeden Fakt für "nicht
 * gefunden". Jetzt: GET /collections/{c}/points/{id}?with_payload=true.
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
const client = () => new QdrantClient("http://localhost:6333", "nexus", 8)

await t("200 + payload → Punkt; URL nutzt /points/{id} und with_payload=true", async () => {
  let seenUrl
  let seenMethod
  globalThis.fetch = async (url, opts = {}) => {
    seenUrl = String(url)
    seenMethod = opts.method
    return {
      ok: true,
      status: 200,
      json: async () => ({ result: { id: "fact-1", payload: { text: "hallo" } } }),
      text: async () => "",
    }
  }
  const point = await client().scrollPoint("fact-1")
  assert.deepStrictEqual(point, { id: "fact-1", payload: { text: "hallo" } })
  assert.match(seenUrl, /\/collections\/nexus\/points\/fact-1/)
  assert.match(seenUrl, /with_payload=true/)
  assert.strictEqual(seenMethod, "GET", "kein Scroll-Body mehr — GET auf den Punkt")
})

await t("404 → null", async () => {
  globalThis.fetch = async () => ({
    ok: false,
    status: 404,
    json: async () => ({}),
    text: async () => "Not Found",
  })
  assert.strictEqual(await client().scrollPoint("fehlt"), null)
})

await t("Netzwerkfehler → null (kein Wurf)", async () => {
  globalThis.fetch = async () => {
    throw new Error("ECONNREFUSED")
  }
  assert.strictEqual(await client().scrollPoint("x"), null)
})

await t("leeres result → null", async () => {
  globalThis.fetch = async () => ({
    ok: true,
    status: 200,
    json: async () => ({ result: null }),
    text: async () => "",
  })
  assert.strictEqual(await client().scrollPoint("x"), null)
})

await t("numerische ID wird als String zurückgegeben", async () => {
  globalThis.fetch = async () => ({
    ok: true,
    status: 200,
    json: async () => ({ result: { id: 7, payload: {} } }),
    text: async () => "",
  })
  const point = await client().scrollPoint("7")
  assert.strictEqual(point.id, "7")
})

globalThis.fetch = realFetch
process.exit(failed ? 1 : 0)
