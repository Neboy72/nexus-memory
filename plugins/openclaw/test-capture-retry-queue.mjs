/**
 * Regressionstest: Capture-Retry-Queue (Astra-R6 P1, 08.09.2026)
 * Drill B aus Astras Abnahme-Test: Qdrant-Ausfall → Captures dürfen NICHT
 * verloren gehen. Beweist am ECHTEN gebauten Plugin (capture-Handler):
 * 1. Storage down → capture wirft NICHT, Eintrag landet in Queue
 * 2. Storage wieder up → nächster Capture draint die Queue (restored)
 * 3. Queue-Datei leer danach
 */
import assert from "node:assert"
import { readFileSync, writeFileSync, unlinkSync, existsSync } from "node:fs"
import { homedir } from "node:os"
import { join } from "node:path"
import { fileURLToPath } from "node:url"
// H3/H4: Direkt-Import der lokalen Quelle (Node type-stripping, kein Build nötig).
import { enqueueCapture, readQueue, writeQueue, drainQueue, queueSize } from "./hooks/capture-retry-queue.ts"
import { buildCaptureHandler } from "./hooks/capture.ts"

// T1: Pfad relativ zum Test-File (nicht machine-specific hardcoded) → portabel.
const DIST_ENTRY = fileURLToPath(new URL("./dist/index.js", import.meta.url))
if (!existsSync(DIST_ENTRY)) {
  console.error("dist/index.js fehlt — erst `npm run build` im plugins/openclaw-Verzeichnis ausführen.")
  process.exit(1)
}

// T4: EXAKT derselbe Pfad wie die Produktion (homedir()-Ableitung statt hardcoded User).
// Die Produktion liest kein ENV → kein TEST-QUEUE-ENV: der Test nutzt den echten Pfad
// und räumt vor+nach dem Lauf auf (Cleanup before AND after).
const QUEUE = join(homedir(), ".openclaw", "workspace", "data", "capture-retry-queue.jsonl")
try { unlinkSync(QUEUE) } catch {}

// Qdrant-Zustand: down bis "up" gesetzt wird
let qdrantUp = false
const originalFetch = globalThis.fetch
globalThis.fetch = async (url, opts) => {
  const u = typeof url === "string" ? url : String(url)
  if (u.includes("voyageai.com")) {
    return { ok: true, status: 200, json: async () => ({ data: [{ embedding: new Array(1024).fill(0.1) }] }) }
  }
  if (u.includes("localhost:6333")) {
    if (!qdrantUp) return { ok: false, status: 503, text: async () => "simulated outage", json: async () => ({}) }
    return { ok: true, status: 200, json: async () => ({ result: {} }) }
  }
  return originalFetch(url, opts)
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
const mod = await import(DIST_ENTRY)
await mod.default.register(mockApi)
const captureHandler = handlers["agent_end"][0]
assert.ok(captureHandler, "agent_end (capture) muss registriert sein")

let failed = 0
const t = (name, fn) => fn().then(() => console.log("PASS ", name)).catch((e) => { failed++; console.log("FAIL ", name, "—", e.message) })

try {
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

  // ── H4: readQueue darf EINE korrupte Zeile nicht als "Queue leer" werten ──
  await t("H4: korrupte Zeile zwischen guten → nur gute gelesen (kein Datenverlust)", async () => {
    const goodA = { id: "a", text: "erster", payload: { scope: "default" } }
    const goodB = { id: "b", text: "zweiter", payload: { scope: "default" } }
    writeFileSync(
      QUEUE,
      JSON.stringify(goodA) + "\n" + '{"id":"corrupt' + "\n" + JSON.stringify(goodB) + "\n",
      "utf8",
    )
    const entries = readQueue()
    assert.strictEqual(entries.length, 2, `2 gute Einträge erwartet, bekam ${entries.length}`)
    assert.deepStrictEqual(entries.map((e) => e.id), ["a", "b"])
  })

  await t("H4: korrupte Zeile fällt beim nächsten writeQueue raus", async () => {
    writeQueue(readQueue())
    assert.ok(!readFileSync(QUEUE, "utf8").includes("corrupt"), "korrupte Zeile muss weg sein")
    assert.strictEqual(readQueue().length, 2, "gute Einträge bleiben erhalten")
  })

  // ── H3: enqueue ist append-only (single-writer), drain holt beide ──
  await t("H3: zwei enqueue → drain stellt beide wieder her, Queue danach leer", async () => {
    writeQueue([]) // saubere Queue
    enqueueCapture({ id: "id-1", text: "eins", payload: { scope: "default" } })
    enqueueCapture({ id: "id-2", text: "zwei", payload: { scope: "default" } })
    // Append-only: KEIN Rewrite in enqueue → beide Einträge müssen erhalten sein.
    assert.deepStrictEqual(readQueue().map((e) => e.id), ["id-1", "id-2"])

    const restored = []
    const n = await drainQueue(
      async (id) => { restored.push(id) },
      async () => [0.1],
    )
    assert.strictEqual(n, 2, `drain muss 2 wiederherstellen, war ${n}`)
    assert.deepStrictEqual(restored.sort(), ["id-1", "id-2"])
    assert.strictEqual(queueSize(), 0, "Queue muss nach drain leer sein (single-writer)")
  })

  // ── H5: Requeue darf ID + inferred Scope nicht verlieren ──
  await t("H5: Requeue nutzt dieselbe ID UND den inferierten Scope", async () => {
    writeQueue([])
    const seenIds = []
    const embedder = { embed: async () => [1, 0, 0] }
    const qdrant = { upsert: async (id) => { seenIds.push(id); throw new Error("storage down") } }
    const centroidCache = { get: async () => ({ work: [1, 0, 0] }) } // klarer Match → scope "work"
    const handler = buildCaptureHandler(embedder, qdrant, { accessLevel: "private" }, centroidCache)
    await handler(
      { success: true, messages: [{ role: "user", content: "Wichtige Erinnerung: Requeue-ID-und-Scope-Test" }] },
      { trigger: "user", messageProvider: "telegram", groupId: null },
    )
    const q = readQueue()
    assert.strictEqual(q.length, 1, "genau ein Queue-Eintrag erwartet")
    assert.strictEqual(q[0].id, seenIds[0], "Requeue muss dieselbe ID nutzen (kein Doppel-Speicher)")
    assert.strictEqual(q[0].payload.scope, "work", `inferierter Scope erwartet, bekam ${q[0].payload.scope}`)
  })
} finally {
  // T3: Mock IMMER restaurieren (läuft vor dem finalen process.exit).
  globalThis.fetch = originalFetch
  // T4: Cleanup NACH dem Lauf (before AND after) — keine Queue-Reste für den nächsten Test.
  try { unlinkSync(QUEUE) } catch {}
}

process.exit(failed ? 1 : 0)
