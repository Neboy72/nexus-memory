/**
 * Regressionstest: Cron-Form-Gate (Astra-R6 P0, 08.09.2026)
 * Beweist am ECHTEN gebauten Handler:
 * 1. Cron-Session + gültiges Formular → durchgelassen
 * 2. Cron-Session mit Thinking-Leak im Text → BLOCKED
 * 3. Cron-Session, freier Text ohne Titel → BLOCKED
 * 4. DM-Session (interaktiv) → unangetastet, auch ohne Formular
 * 5. Heartbeat-Session + gültiges Formular → durchgelassen
 * 6. isUnattendedSession: Key-Erkennung korrekt
 */
import assert from "node:assert"

const mod = await import("/Users/miosha/nexus-memory/plugins/openclaw/dist/index.js")
// Einzeldatei nicht gebundelt (esbuild bundled nur index.ts) — Gate-Funktionen via internem Export prüfen:
// dist/index.js ist ein Bundle; die Gate-Helfer sind dort eingekapselt. Für Unit-Tests verwenden wir
// tsx-freie Variante: wir importieren das TS direkt via node --experimental-strip-types NICHT — stattdessen
// prüfen wir die Verhaltensweise über den registrierten Handler (E2E, echtes Bundle).

let failed = 0
const t = (name, fn) => fn().then(() => console.log("PASS ", name)).catch((e) => { failed++; console.log("FAIL ", name, "—", e.message) })

// Session-Erkennung wird über Handler-Verhalten bewiesen (DM-Test vs Cron-Tests)

// --- Handler-Tests: registriere das Plugin und greife auf message_sending zu
const handlers = {}
const mockApi = {
  on(e, h) { handlers[e] = h },
  registerTool() {}, registerProvider() {}, registerService() {},
  logger: { info: () => {}, warn: () => {}, error: () => {}, debug: () => {} },
  pluginConfig: {
    qdrantUrl: "http://localhost:6333",
    collection: "nexus-test-gate",
    autoRecall: false,
    autoCapture: false,
    accessLevel: "private",
    embedding: { provider: "voyage", apiKey: "test" },
  },
}

// fetch-Mock für Qdrant + Voyage (keine Netzwerk-Kosten)
const realFetch = globalThis.fetch
globalThis.fetch = async (url, opts) => {
  const u = typeof url === "string" ? url : String(url)
  if (u.includes("voyageai.com")) {
    return { ok: true, status: 200, json: async () => ({ data: [{ embedding: new Array(1024).fill(0.1) }] }) }
  }
  if (u.includes("localhost:6333")) {
    return { ok: true, status: 200, json: async () => ({ result: [] }) }
  }
  return realFetch(url, opts)
}

await mod.default.register(mockApi)
const sendingHandler = handlers["message_sending"]
assert.ok(sendingHandler, "message_sending Handler muss registriert sein")

const CRON_KEY = "agent:main:cron:98d4e5bb-7971-4348-805b-2a38b640878f:run:9b65cf0a"
const DM_KEY = "agent:main:telegram:default:direct:5763330319"

const goodForm = [
  "🚀 OpenClaw Release",
  "Neue Version v2026.9.3 ist draußen.",
  "Relevant: Memory-Plugin-Schema geändert.",
  "",
  "Miosha 🦊",
].join("\n")

await t("cron + gültiges Formular → durchgelassen", async () => {
  const res = await sendingHandler({ to: "telegram:5763330319", content: goodForm }, { sessionKey: CRON_KEY })
  assert.ok(!res || !res.cancel, "gültiges Formular darf NICHT geblockt werden")
})

await t("cron + Thinking-Leak → BLOCKED", async () => {
  const leakText = "🚀 OpenClaw Release\nLet me work through this task step by step. First I need to fetch the feed.\n\nMiosha 🦊"
  const res = await sendingHandler({ to: "telegram:5763330319", content: leakText }, { sessionKey: CRON_KEY })
  assert.ok(res && res.cancel === true, "Leak-Formular MUSS geblockt werden")
})

await t("cron + freier Text ohne Titel → BLOCKED", async () => {
  const free = "Hey Nebo, ich habe heute mal ein paar Gedanken zusammengefasst, die dir vielleicht nützlich erscheinen könnten, weil ich viel recherchiert habe heute Nacht."
  const res = await sendingHandler({ to: "telegram:5763330319", content: free }, { sessionKey: CRON_KEY })
  assert.ok(res && res.cancel === true, "freier Text MUSS geblockt werden")
})

await t("DM interaktiv → unangetastet", async () => {
  const free = "Hey Nebo, ganz spontan: Ich habe eine Idee für das Dashboard!"
  const res = await sendingHandler({ to: "telegram:5763330319", content: free }, { sessionKey: DM_KEY })
  assert.ok(!res || !res.cancel, "interaktive DM darf NIE geblockt werden")
})

await t("heartbeat + gültiges Formular → durchgelassen", async () => {
  const res = await sendingHandler({ to: "telegram:5763330319", content: goodForm }, { sessionKey: "agent:main:main:heartbeat" })
  assert.ok(!res || !res.cancel, "gültiges Formular im Heartbeat darf nicht geblockt werden")
})

await t("cron + zu langer Text (>900) → BLOCKED", async () => {
  const long = "🚀 OpenClaw Release\n" + "X".repeat(950)
  const res = await sendingHandler({ to: "telegram:5763330319", content: long }, { sessionKey: CRON_KEY })
  assert.ok(res && res.cancel === true, "übergroße Nachricht MUSS geblockt werden")
})

process.exit(failed ? 1 : 0)