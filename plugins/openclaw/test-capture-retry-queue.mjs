/**
 * Regressionstest: Capture-Retry-Queue (Astra-R6 P1, 08.09.2026)
 * Drill B aus Astras Abnahme-Test: Qdrant-Ausfall → Captures dürfen NICHT
 * verloren gehen. Beweist am ECHTEN gebauten Plugin (capture-Handler):
 * 1. Storage down → capture wirft NICHT, Eintrag landet in Queue
 * 2. Storage wieder up → nächster Capture draint die Queue (restored)
 * 3. Queue-Datei leer danach
 */
import assert from "node:assert"
import { readFileSync, writeFileSync, unlinkSync } from "node:fs"

const QUEUE = "/Users/miosha/.openclaw/workspace/data/capture-retry-queue.jsonl"
try { unlinkSync(QUEUE) } catch {}

// Qdrant-Zustand: down bis "up" gesetzt wird
let qdrantUp = false
const realFetch = globalThis.fetch
globalThis.fetch = async (url, opts) => {
  const u = typeof url === "string" ? url : String(url)
  if (u.includes("voyageai.com")) {
    return { ok: true, status: 200, json: async () => ({ data: [{ embedding: new Array(1024).fill(0.1) }] }) }
  }
  if (u.includes("localhost:6333")) {
    if (!qdrantUp) return { ok: false, status: 503, text: async () => "simulated outage", json: async () => ({}) }
    return { ok: true, status: 200, json: async () => ({ result: {} }) }
  }
  return realFetch(url, opts)
}

const handlers = {}
const mockApi = {
  on(e, h) { if (!handlers[e]) handlers[e] = []; handlers[e].push(h) },
  registerTool() {}, registerProvider() {}, registerService() {},
  logger: { info: () => {}, warn: () => {}, error: () => {}, debug: () => {} },
  pluginConfig: {
    qdrantUrl: "http://localhost:6333",
    collection: "nexus-test-gate",
    autoRecall: false,
    autoCapture: true,
    accessLevel: "private",
    embedding: { provider: "voyage", apiKey: "test" },
  },
}
const mod = await import("/Users/miosha/nexus-memory/plugins/openclaw/dist/index.js")
await mod.default.register(mockApi)
const captureHandler = handlers["agent_end"][0]
assert.ok(captureHandler, "agent_end (capture) muss registriert sein")

let failed = 0
const t = (name, fn) => fn().then(() => console.log("PASS ", name)).catch((e) => { failed++; console.log("FAIL ", name, "—", e.message) })

await t("Storage down → capture landet in Queue, kein Verlust", async () => {
  await captureHandler(
    { success: true, messages: [{ role: "user", content: "Wichtige Erinnerung während des Ausfalls: Testeintrag Drain-B" }] },
    { trigger: "user", messageProvider: "telegram", groupId: null },
  )
  const q = readFileSync(QUEUE, "utf8")
  assert.ok(q.includes("Testeintrag Drain-B"), "Text muss in der Queue stehen")
})

await t("Storage up → nächster Capture draint Queue", async () => {
  qdrantUp = true
  await captureHandler(
    { success: true, messages: [{ role: "user", content: "Frischer Capture nach Wiederherstellung Drain-B" }] },
    { trigger: "user", messageProvider: "telegram", groupId: null },
  )
  // Kurz warten (drain läuft im Capture)
  await new Promise((r) => setTimeout(r, 500))
  const q = readFileSync(QUEUE, "utf8").trim()
  assert.strictEqual(q, "", "Queue muss nach Drain leer sein")
})

process.exit(failed ? 1 : 0)