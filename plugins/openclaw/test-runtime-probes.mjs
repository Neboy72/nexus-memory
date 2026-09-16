/**
 * Regressionstest: Runtime-Probes + Backend-Auflösung (H122, Welle 13)
 *
 * Vorher waren status() (hartkodierte files/chunks=0), probeEmbedding (immer
 * {ok:true}) und probeVectorAvailability (immer true) ehrliche Stubs, und
 * resolveMemoryBackendConfig() ignorierte jeden cfg-Parameter. Jetzt zählen
 * die Probes real (Embedder-Ping, Qdrant points_count) und die Kommentare
 * lügen nicht mehr.
 */
import assert from "node:assert"
import { QdrantClient } from "./lib/qdrant-client.ts"
import { buildMemoryRuntime } from "./runtime.ts"

let failed = 0
const t = (name, fn) =>
  Promise.resolve()
    .then(fn)
    .then(() => console.log("PASS ", name))
    .catch((e) => {
      failed++
      console.log("FAIL ", name, "—", e.message)
    })
    // Restore the real fetch after EVERY test: a throwing stub (e.g. the
    // "Qdrant down" case) must not leak into later tests, and a failure must
    // not leave the patched stub in place for the process tail.
    .finally(() => {
      globalThis.fetch = realFetch
    })

const realFetch = globalThis.fetch

function stubCount(count, ok = true, seen = []) {
  globalThis.fetch = async (url, opts = {}) => {
    seen.push({ url: String(url), method: opts.method })
    return {
      ok,
      status: ok ? 200 : 500,
      json: async () => ({ result: { points_count: count } }),
      text: async () => "",
    }
  }
  return seen
}

const okEmbedder = { embed: async () => [0.1, 0.2] }
const client = () => new QdrantClient("http://localhost:6333", "nexus", 8)

await t("status() meldet echte Punktezahl (count-Endpoint)", async () => {
  const seen = stubCount(7)
  const rt = buildMemoryRuntime(client(), okEmbedder)
  const { manager } = await rt.getMemorySearchManager({ cfg: {}, agentId: "a" })
  await manager.refreshStatus()
  const s = manager.status()
  assert.strictEqual(s.chunks, 7, "chunks = points_count")
  assert.strictEqual(s.files, 7, "files spiegelt chunks (kein separates Mapping)")
  assert.strictEqual(s.custom.count_observed, true)
  assert.match(String(s.custom.count_source), /points_count/)
  assert.match(seen[0].url, /\/collections\/nexus/)
  assert.strictEqual(seen[0].method, "GET")
})

await t("status() ohne erreichbares Qdrant → 0 + count_observed=false", async () => {
  globalThis.fetch = async () => { throw new Error("down") }
  const rt = buildMemoryRuntime(client(), okEmbedder)
  const { manager } = await rt.getMemorySearchManager({ cfg: {}, agentId: "a" })
  await manager.refreshStatus()
  const s = manager.status()
  assert.strictEqual(s.chunks, 0)
  assert.strictEqual(s.custom.count_observed, false)
})

await t("probeEmbeddingAvailability: funktionierender Embedder → ok", async () => {
  const rt = buildMemoryRuntime(client(), okEmbedder)
  const { manager } = await rt.getMemorySearchManager({ cfg: {}, agentId: "a" })
  assert.deepStrictEqual(await manager.probeEmbeddingAvailability(), { ok: true })
})

await t("probeEmbeddingAvailability: embed() wirft → ok:false + error", async () => {
  const rt = buildMemoryRuntime(client(), {
    embed: async () => { throw new Error("voyage 401") },
  })
  const { manager } = await rt.getMemorySearchManager({ cfg: {}, agentId: "a" })
  const probe = await manager.probeEmbeddingAvailability()
  assert.strictEqual(probe.ok, false)
  assert.match(probe.error, /voyage 401/)
})

await t("probeVectorAvailability delegiert an den count-Check", async () => {
  stubCount(3)
  const rt = buildMemoryRuntime(client(), okEmbedder)
  const { manager } = await rt.getMemorySearchManager({ cfg: {}, agentId: "a" })
  assert.strictEqual(await manager.probeVectorAvailability(), true)

  globalThis.fetch = async () => ({ ok: false, status: 503, json: async () => ({}), text: async () => "" })
  assert.strictEqual(await manager.probeVectorAvailability(), false)
})

await t("sync()/close() existieren und sind no-op-Promises", async () => {
  const rt = buildMemoryRuntime(client(), okEmbedder)
  const { manager } = await rt.getMemorySearchManager({ cfg: {}, agentId: "a" })
  assert.strictEqual(typeof manager.sync, "function")
  assert.strictEqual(typeof manager.close, "function")
  assert.strictEqual(await manager.sync(), undefined)
  assert.strictEqual(await manager.close(), undefined)
})

await t("resolveMemoryBackendConfig liefert 'builtin' (einziger Backend)", () => {
  const rt = buildMemoryRuntime(client(), okEmbedder)
  assert.deepStrictEqual(
    rt.resolveMemoryBackendConfig({ cfg: { backend: "qmd" }, agentId: "a" }),
    { backend: "builtin" },
  )
})

globalThis.fetch = realFetch
process.exit(failed ? 1 : 0)
