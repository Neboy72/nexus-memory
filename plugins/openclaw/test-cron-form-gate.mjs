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
import { existsSync } from "node:fs"
import { fileURLToPath } from "node:url"
// H6/H7: Direkt-Import der lokalen Quelle (Node type-stripping, kein Build nötig).
import { buildCronFormGateHandler } from "./hooks/cron-form-gate.ts"

// T1: Pfad relativ zum Test-File (nicht machine-specific hardcoded) → portabel.
const DIST_ENTRY = fileURLToPath(new URL("./dist/index.js", import.meta.url))
if (!existsSync(DIST_ENTRY)) {
  console.error("dist/index.js fehlt — erst `npm run build` im plugins/openclaw-Verzeichnis ausführen.")
  process.exit(1)
}

const mod = await import(DIST_ENTRY)
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
const originalFetch = globalThis.fetch
globalThis.fetch = async (url, opts) => {
  const u = typeof url === "string" ? url : String(url)
  if (u.includes("voyageai.com")) {
    return { ok: true, status: 200, json: async () => ({ data: [{ embedding: new Array(1024).fill(0.1) }] }) }
  }
  if (u.includes("localhost:6333")) {
    return { ok: true, status: 200, json: async () => ({ result: [] }) }
  }
  return originalFetch(url, opts)
}

// T6: der GESAMTE Flow in try/catch/finally — ein Fehler außerhalb von t() darf den
// failed-Counter nicht umgehen und muss deterministisch mit Exit-Code 1 enden.
try {
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

  // ── H6/H7: Kurz-Token-Whitelist + message/text-Payload (lokale Quelle) ──
  const gate = buildCronFormGateHandler()
  const cronCtx = { sessionKey: CRON_KEY }
  const shortSpam = "abc def ghi jkl mno pqr" // 23 Zeichen: kein Token, kein Formular
  assert.strictEqual(shortSpam.length, 23, "Fixture muss 23 Zeichen haben")

  await t("H6: 23-Zeichen-Non-Token → BLOCKED (Lücke geschlossen)", async () => {
    const res = await gate({ to: "telegram:5763330319", content: shortSpam }, cronCtx)
    assert.ok(res && res.cancel === true, "Kurz-Non-Token MUSS geblockt werden")
  })

  await t("H6: NO_REPLY → durchgelassen", async () => {
    const res = await gate({ to: "telegram:5763330319", content: "NO_REPLY" }, cronCtx)
    assert.ok(!res || !res.cancel, "NO_REPLY darf NICHT geblockt werden")
  })

  await t("H6: Whitelist-Tokens ([SILENT]/ok/OK/-/leer) → durchgelassen", async () => {
    for (const tok of ["[SILENT]", "ok", "OK", "-", "   "]) {
      const res = await gate({ to: "telegram:5763330319", content: tok }, cronCtx)
      assert.ok(!res || !res.cancel, `Token «${tok}» darf nicht geblockt werden`)
    }
  })

  await t("H7: Payload in event.message → Gate greift", async () => {
    const res = await gate({ to: "telegram:5763330319", message: shortSpam }, cronCtx)
    assert.ok(res && res.cancel === true, "message-Payload muss das Gate treffen")
  })

  await t("H7: Payload in event.text → Gate greift", async () => {
    const res = await gate({ to: "telegram:5763330319", text: shortSpam }, cronCtx)
    assert.ok(res && res.cancel === true, "text-Payload muss das Gate treffen")
  })

  await t("H7: gültiges Formular über event.message → durchgelassen", async () => {
    const res = await gate({ to: "telegram:5763330319", message: goodForm }, cronCtx)
    assert.ok(!res || !res.cancel, "gültiges Formular darf nicht geblockt werden")
  })

  await t("H7: interactiv (DM) bleibt unangetastet, auch via message", async () => {
    const res = await gate({ to: "telegram:5763330319", message: shortSpam }, { sessionKey: DM_KEY })
    assert.ok(!res || !res.cancel, "interaktive DM darf NIE geblockt werden")
  })
} catch (e) {
  // Fehler außerhalb von t() (z.B. Register- oder Fixture-Fehler) → zählt als FAIL.
  console.error("Unerwarteter Fehler im Test-Flow:", e)
  process.exitCode = 1
  failed++
} finally {
  // T3: Mock IMMER restaurieren (vor dem deterministischen Exit).
  globalThis.fetch = originalFetch
  // T6: EIN deterministischer Exit-Punkt — kein process.exit IM try.
  process.exit(failed === 0 ? 0 : 1)
}
