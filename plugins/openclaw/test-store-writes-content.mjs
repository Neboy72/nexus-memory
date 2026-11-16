/**
 * Regressionstest: der Schreibpfad setzt `content` (15.11.2026).
 *
 * Befund: 628 von 52.631 Einträgen trugen ihren Text NUR unter `text`
 * (296 OpenClaw-Protokolle, 301 Wiki-Einträge, 29 `openclaw_tool`,
 * 2 `insight-agent`). Die Schreiber hier bauten ihr Payload mit `text:` —
 * jeder Leser, der `content` liest, sah den Eintrag als leer an.
 *
 * Geprüft wird:
 *   1. das Store-Werkzeug (der Weg, über den auch der Wiki-Ingest schreibt)
 *      setzt `content` UND behält `text` (Abwärtskompatibilität),
 *   2. der Capture-Hook setzt `content` genauso,
 *   3. `pointText()` liest einen Punkt, der nur `text` trägt.
 */
import assert from "node:assert"
import { tmpdir } from "node:os"
import { join } from "node:path"
import { register } from "node:module"

// Queue-Datei des Capture-Hooks umlenken — der Test darf die echte
// Warteschlange nicht anfassen (W27-Override, wird zur Laufzeit gelesen).
process.env.NEXUS_CAPTURE_QUEUE_FILE = join(
  tmpdir(),
  `nexus-test-store-writes-content-${process.pid}.jsonl`,
)

register("./_typebox-test-loader.mjs", import.meta.url)
const { registerStoreTool } = await import("./tools/store.ts")
const { buildCaptureHandler } = await import("./hooks/capture.ts")
const { pointText } = await import("./lib/qdrant-client.ts")

let failed = 0
const t = (name, fn) =>
  Promise.resolve()
    .then(fn)
    .then(() => console.log("PASS ", name))
    .catch((e) => {
      failed++
      console.log("FAIL ", name, "—", e?.stack ?? String(e))
    })

const TEXT = "Der Verteiler steht im Flur."

// --- Store-Werkzeug ---------------------------------------------------------

function makeStoreTool(cfg = { accessLevel: "public" }) {
  let tool
  const touched = { upsert: 0, payload: null }
  const api = { registerTool: (tt) => { tool = tt } }
  registerStoreTool(
    api,
    { embed: async () => [0.1, 0.2] },
    {
      upsert: async (_id, _vec, payload) => {
        touched.upsert++
        touched.payload = payload
        return undefined
      },
    },
    cfg,
  )
  return { tool, touched }
}

function lastPayload(touched) {
  assert.ok(
    touched.upsert >= 1 && touched.payload,
    `upsert wurde nie aufgerufen (upsert=${touched.upsert}) — kein payload vorhanden`,
  )
  return touched.payload
}

await t("store: Payload trägt content (und text für Altleser)", async () => {
  const { tool, touched } = makeStoreTool()
  await tool.execute("id", { text: TEXT })
  const payload = lastPayload(touched)
  assert.strictEqual(payload.content, TEXT, "content ist das führende Feld")
  assert.strictEqual(payload.text, TEXT, "text bleibt für Abwärtskompatibilität")
})

await t("store: Wiki-Weg (explizite Quelle) trägt content", async () => {
  const { tool, touched } = makeStoreTool()
  await tool.execute("id", { text: TEXT, source: "wiki" })
  const payload = lastPayload(touched)
  assert.strictEqual(payload.source, "wiki")
  assert.strictEqual(payload.content, TEXT, "auch die Wiki-Quelle schreibt content")
})

// --- Capture-Hook -----------------------------------------------------------

function makeCapture(cfg = { accessLevel: "public" }) {
  let captured = null
  const handler = buildCaptureHandler(
    { embed: async () => [0.1, 0.2] },
    {
      upsert: async (_id, _vec, payload) => {
        captured = payload
        return undefined
      },
    },
    cfg,
  )
  return { handler, payload: () => captured }
}

await t("capture: Payload trägt content (und text)", async () => {
  const { handler, payload } = makeCapture()
  await handler(
    {
      success: true,
      messages: [{ role: "user", content: TEXT }],
    },
    { trigger: "user", messageProvider: "openai", groupId: null },
  )
  const written = payload()
  assert.ok(written, "der Capture-Hook hat nichts geschrieben")
  assert.ok(
    typeof written.content === "string" && written.content.includes(TEXT),
    `content fehlt oder ist leer: ${JSON.stringify(written.content)}`,
  )
  assert.strictEqual(
    written.content,
    written.text,
    "content und text müssen denselben Text tragen",
  )
})

// --- Lesen: der Rückfall, den die Schreiber nötig machten --------------------

await t("pointText(): Punkt mit nur text wird gelesen", () => {
  assert.strictEqual(pointText({ text: TEXT }), TEXT)
  assert.strictEqual(pointText({ content: TEXT }), TEXT)
  assert.strictEqual(pointText({ content: "führend", text: "veraltet" }), "führend")
  assert.strictEqual(pointText({}), "")
  assert.strictEqual(pointText(null), "")
})

process.exitCode = failed ? 1 : 0
