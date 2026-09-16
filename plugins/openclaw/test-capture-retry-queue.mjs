/**
 * Regressionstest: Capture-Retry-Queue (Astra-R6 P1, 08.09.2026)
 * Drill B aus Astras Abnahme-Test: Qdrant-Ausfall → Captures dürfen NICHT
 * verloren gehen. Beweist am ECHTEN gebauten Plugin (capture-Handler):
 * 1. Storage down → capture wirft NICHT, Eintrag landet in Queue
 * 2. Storage wieder up → nächster Capture draint die Queue (restored)
 * 3. Queue-Datei leer danach
 */
import assert from "node:assert"
import { readFileSync, writeFileSync, unlinkSync, existsSync, mkdtempSync, rmSync } from "node:fs"
import { tmpdir } from "node:os"
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

// T4/W27: Sandbox-Queue — der Test setzt NEXUS_CAPTURE_QUEUE_FILE, bevor
// irgendeine Queue-Funktion läuft (queueFile() löst zur Call-Zeit auf).
// Die PRODUKTIONS-Queue (~/.openclaw/workspace/data/capture-retry-queue.jsonl)
// wird nie mehr angefasst: unlinkSync auf dem echten Pfad konnte bei einem
// Lauf ohne Sandbox echte, noch nicht restaurierte Captures löschen.
const QDRANT_BASE = "localhost:6333" // Single-Source für Mock + pluginConfig (W39/F4)
const SANDBOX_DIR = mkdtempSync(join(tmpdir(), "nexus-queue-test-"))
process.env.NEXUS_CAPTURE_QUEUE_FILE = join(SANDBOX_DIR, "capture-retry-queue.jsonl")
const QUEUE = process.env.NEXUS_CAPTURE_QUEUE_FILE
try { unlinkSync(QUEUE) } catch {}

// Qdrant-Zustand: down bis "up" gesetzt wird
let qdrantUp = false
const originalFetch = globalThis.fetch
globalThis.fetch = async (url, opts) => {
  const u = typeof url === "string" ? url : String(url)
  if (u.includes("voyageai.com")) {
    // Nr 379: parseEmbeddingResponse liest resp.text() zuerst — Mock erfuellt Contract
    return { ok: true, status: 200, text: async () => JSON.stringify({ data: [{ embedding: new Array(1024).fill(0.1) }] }), json: async () => ({ data: [{ embedding: new Array(1024).fill(0.1) }] }) }
  }
  if (u.includes(QDRANT_BASE)) {
    if (!qdrantUp) return { ok: false, status: 503, text: async () => "simulated outage", json: async () => ({}) }
    return { ok: true, status: 200, json: async () => ({ result: {} }) }
  }
  if (u.includes("api.github.com")) {
    // W39/F1: checkForUpdate() fire-and-forget — deterministisch beantworten
    return { ok: true, status: 200, json: async () => ({ tag_name: "v0.0.0-noop", html_url: "http://localhost/noop" }), text: async () => "{}" }
  }
  return originalFetch(url, opts)
}

const handlers = {}
const mockApi = {
  on(e, h) { if (!handlers[e]) handlers[e] = []; handlers[e].push(h) },
  registerTool() {}, registerProvider() {}, registerService() {},
  registerMemoryCapability() {}, // Nr 364: register() fail-loud wenn nichts registriert wird
  logger: { info: () => {}, warn: () => {}, error: () => {}, debug: () => {} },
  pluginConfig: {
    qdrantUrl: `http://${QDRANT_BASE}`,
    collection: "nexus-test-gate",
    autoRecall: false,
    autoCapture: true,
    accessLevel: "private",
    embedding: { provider: "voyage", apiKey: "test" },
  },
}
// W39/F1+F3: Setup (import + register) läuft im try-Block — der finally-Restore
// gilt ab da, nicht erst nach den Tests. F1: register() feuert checkForUpdate()
// fire-and-forget gegen api.github.com — der Mock interceptiert es (kein echter
// Netz-Call, keine Race gegen den realen fetch-Restore).
let mod, captureHandler
let failed = 0
const t = (name, fn) => Promise.resolve().then(fn).then(() => console.log("PASS ", name)).catch((e) => { failed++; console.log("FAIL ", name, "—", e?.stack ?? String(e)) })

try {
mod = await import(DIST_ENTRY)
await mod.default.register(mockApi)
captureHandler = handlers["agent_end"]?.[0]
assert.ok(captureHandler, "agent_end (capture) muss registriert sein")
} catch (e) {
  console.log("SETUP-FAIL:", e?.stack ?? String(e))
  globalThis.fetch = originalFetch
  process.exitCode = 1
  process.exit(1)
}

// H166: statt festem 500ms-Sleep (unter Last zu kurz = false FAIL) pollen wir
// die Queue-Datei bis sie leer ist — Timeout 3s, dann beschreibender FAIL.
async function waitForQueueEmpty(timeoutMs = 3000, intervalMs = 50) {
  const deadline = Date.now() + timeoutMs
  for (;;) {
    let q = ""
    try { q = readFileSync(QUEUE, "utf8").trim() } catch { q = "" } // ENOENT = leer
    if (q === "") return
    if (Date.now() >= deadline) {
      const shortened = q.length > 200 ? q.slice(0, 200) + "…" : q
      throw new Error(`Queue nach ${timeoutMs}ms nicht leer: ${shortened}`)
    }
    await new Promise((r) => setTimeout(r, intervalMs))
  }
}

try {
  await t("Storage down → capture landet in Queue, kein Verlust", async () => {
    await captureHandler(
      { success: true, messages: [{ role: "user", content: "Wichtige Erinnerung während des Ausfalls: Testeintrag Drain-B" }] },
      { trigger: "user", messageProvider: "telegram", groupId: null },
    )
    let q
    try { q = readFileSync(QUEUE, "utf8") } catch (e) {
      // W39/F5: enqueueCapture kann bei internem Fehler still sein — die ENOENT hier
      // ist dann BEWEIS (Queue nie angelegt), nicht ein Verwirr-Fehler.
      if (e.code === "ENOENT") throw new Error("Queue-Datei wurde nie angelegt — enqueueCapture hat nicht geschrieben (Code-Verweis: hooks/capture-retry-queue.ts enqueue)")
      throw e
    }
    assert.ok(q.includes("Testeintrag Drain-B"), "Text muss in der Queue stehen")
  })

  await t("Storage up → nächster Capture draint Queue", async () => {
    qdrantUp = true
    await captureHandler(
      { success: true, messages: [{ role: "user", content: "Frischer Capture nach Wiederherstellung Drain-B" }] },
      { trigger: "user", messageProvider: "telegram", groupId: null },
    )
    // Drain läuft im Capture — auf leere Queue warten (H166: Poll statt fester Sleep)
    await waitForQueueEmpty()
    let q = ""
    try { q = readFileSync(QUEUE, "utf8").trim() } catch (e) {
      if (e.code === "ENOENT") q = "" // W39/F7: drain darf Datei löschen ODER leeren
      else throw e // W39/F6: EACCES/EISDIR ist KEIN 'Queue leer' → false-PASS-Guard
    }
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
  // T4/W27: Cleanup NACH dem Lauf — Sandbox-Queue + -Dir weg (Produktions-Queue unberührt).
  try { unlinkSync(QUEUE) } catch {}
  try { rmSync(SANDBOX_DIR, { recursive: true }) } catch {}
}

process.exitCode = failed ? 1 : 0
