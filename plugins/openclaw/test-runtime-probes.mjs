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
import { initLogger } from "./logger.ts"

// Fund F: das Verwerfen von cfg.backend:"qmd" ist dokumentiert (debug-Zeile
// "builtin is the only backend") — hier sichtbar machen und pinnen statt
// das Discard still schlucken.
const debugLines = []
initLogger(
  {
    info: () => {},
    warn: () => {},
    error: () => {},
    debug: (m) => debugLines.push(String(m)),
  },
  true,
)

let failed = 0
const t = (name, fn) =>
  Promise.resolve()
    .then(fn)
    .then(() => console.log("PASS  ", name))
    .catch((e) => {
      failed++
      console.log("FAIL  ", name, "—", e.message)
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
  // Fund E: files===chunks ist Implementierungsdetail (kein separates
  // Mapping) — als KONTRAKT gepinnt würde ein künftiges echtes File-Counting
  // fälschlich brechen. Nur noch plausibilisieren statt pin:
  assert.ok(
    s.files === undefined || (typeof s.files === "number" && s.files >= 0),
    `files muss Zahl >= 0 oder undefined sein, war ${JSON.stringify(s.files)}`,
  )
  assert.strictEqual(s.custom.count_observed, true)
  assert.match(String(s.custom.count_source), /points_count/)
  // Fund C: Request muss NACHGEWIESEN sein, bevor seen[0] gelesen wird —
  // sonst schlägt eine Regression (kein Request) mit TypeError auf, statt
  // mit klarer Meldung.
  assert.ok(seen.length > 0, "status() muss einen count-Request an Qdrant stellen")
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
  // Fund A: error ist nicht garantiert string — bei ok:false ohne error
  // oder nicht-string error soll die Meldung das zeigen, nicht ein
  // TypeError aus assert.match.
  const errText = typeof probe.error === "string" ? probe.error : JSON.stringify(probe.error)
  assert.ok(errText && errText !== "undefined", `probe.error fehlt bei ok:false: ${JSON.stringify(probe)}`)
  assert.match(errText, /voyage 401/)
})

await t("probeVectorAvailability delegiert an den count-Check", async () => {
  // Fund D: die Request-Aufzeichnung wurde verworfen — hiermit beweisen,
  // dass der Probe wirklich den collections-Endpoint trifft.
  const seen = stubCount(3)
  const rt = buildMemoryRuntime(client(), okEmbedder)
  const { manager } = await rt.getMemorySearchManager({ cfg: {}, agentId: "a" })
  assert.strictEqual(await manager.probeVectorAvailability(), true)
  const hits = seen.filter((s) => /\/collections\//.test(s.url))
  assert.ok(hits.length > 0, "probeVectorAvailability muss den count-Endpoint rufen")
  assert.strictEqual(hits[0].method, "GET")

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
  const before = debugLines.length
  assert.deepStrictEqual(
    rt.resolveMemoryBackendConfig({ cfg: { backend: "qmd" }, agentId: "a" }),
    { backend: "builtin" },
  )
  // Fund F: das Discard ist dokumentiert — die debug-Zeile muss es
  // sichtbar machen, nicht still schlucken.
  assert.ok(
    debugLines.slice(before).some((l) => /builtin/.test(l) && /qmd/.test(l)),
    "Rückfall auf builtin muss debug-loggen (qmd nicht implementiert)",
  )
})

// Fund B: process.exitCode statt process.exit — gepufferte Ausgaben
// (CI-Pipes) werden nicht mehr abgeschnitten, der Event-Loop darf leeren.
globalThis.fetch = realFetch
process.exitCode = failed ? 1 : 0