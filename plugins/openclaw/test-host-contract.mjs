/**
 * Kontrakt-Test: message_sending gegen den ECHTEN OpenClaw-Host (30.09.2026).
 *
 * Anlass: Unser thought-filter gab über Monate `{message: …}` zurück. Der Host
 * liest aber AUSSCHLIESSLICH `content` (docs/plugins/hooks/messages.md,
 * PluginHookMessageSendingResult, Empirie gegen dist/hooks-*.mjs) — die
 * Bereinigung UND der Drop liefen deshalb ins Leere. Dieser Test pinnt die
 * Host-Semantik gegen das installierte Bundle und beweist, dass unsere
 * Handler-Ergebnisse in genau diesen Feldern ankommen.
 *
 * Abhängigkeit: OpenClaw-Bundle des Hosts (nicht Teil des Repos). Fehlt es,
 * wird SKIP gemeldet — der Test darf fremde Umgebungen nicht rot treten, die
 * Host-Semantik selbst ist in test-thought-filter.mjs zusätzlich lokal gepinnt.
 */
import assert from "node:assert"
import { createRequire } from "node:module"

const require = createRequire(import.meta.url)
const HOST_ROOT = "/opt/homebrew/lib/node_modules/openclaw"

let createHookRunner
let hooksChunk
try {
  const pkg = require(`${HOST_ROOT}/package.json`)
  assert.ok(pkg?.version, "Host-package.json lesbar")
  // Der Bundle-Dateiname trägt einen Content-Hash → über den Verzeichnis-Scan
  // auflösen statt einen festen Namen zu pinnen.
  const fs = require("node:fs")
  const path = require("node:path")
  const dist = path.join(HOST_ROOT, "dist")
  const candidates = fs
    .readdirSync(dist)
    .filter((f) => f.startsWith("hooks-") && f.endsWith(".mjs"))
  assert.ok(candidates.length > 0, "hooks-*.mjs im Host-dist gefunden")
  // Der Runner-Modul-Export ist im minifizierten Bundle umbenannt — den
  // Export-Namen über die export-Zeile ermitteln statt ihn zu raten.
  let mod = null
  for (const file of candidates) {
    hooksChunk = file
    const url = new URL(`file://${path.join(dist, file)}`).href
    const m = await import(url)
    const fn = Object.values(m).find(
      (v) => typeof v === "function" && v.name === "createHookRunner",
    )
    if (fn) {
      createHookRunner = fn
      mod = m
      break
    }
  }
  assert.ok(mod && createHookRunner, "createHookRunner im Host-Bundle gefunden")
} catch (e) {
  console.log(`SKIP  host-contract: OpenClaw-Bundle nicht verfügbar (${e?.message || e})`)
  console.log("SKIP  (Host-Semantik ist lokal in test-thought-filter.mjs gepinnt)")
  process.exit(0)
}

let failed = 0
const t = async (name, fn) => {
  try {
    await fn()
    console.log("PASS ", name)
  } catch (e) {
    failed++
    console.log("FAIL ", name, "—", e?.message || e)
  }
}

const silent = { logger: { warn() {}, error() {}, debug() {}, info() {} } }

function runnerWith(handlers) {
  const registry = {
    typedHooks: handlers.map((handler, i) => ({
      pluginId: `contract-${i}`,
      hookName: "message_sending",
      handler,
      priority: 0,
    })),
  }
  return createHookRunner(registry, silent)
}

const EVENT = { to: "telegram:test", content: "ORIGINAL-TEXT" }
const CTX = { channelId: "telegram", accountId: "acct", senderId: "user-1" }

// ── 1: der Host liest `content`, NICHT `message` ──
await t("Host liest `.content` (Rewriting wirkt)", async () => {
  const res = await runnerWith([async () => ({ content: "CLEANED" })]).runMessageSending(EVENT, CTX)
  assert.strictEqual(res?.content, "CLEANED")
})

await t("Host ignoriert `.message` (unser Alt-Shape war wirkungslos)", async () => {
  const res = await runnerWith([async () => ({ message: "CLEANED" })]).runMessageSending(EVENT, CTX)
  assert.ok(
    res?.content === undefined,
    `Host darf \`message\` nicht als content übernehmen (war ${JSON.stringify(res?.content)})`,
  )
})

// ── 2: jeder Handler sieht das ORIGINAL (Clobber-Gefahr) ──
await t("JEDER Handler sieht das ORIGINAL-Event (kein Fortschreiben)", async () => {
  let seen = null
  const res = await runnerWith([
    async () => ({ content: "CLEANED" }),
    async (event) => {
      seen = event.content
      return undefined
    },
  ]).runMessageSending(EVENT, CTX)
  assert.strictEqual(seen, "ORIGINAL-TEXT", "zweiter Handler darf den Original-Text sehen")
  assert.strictEqual(res?.content, "CLEANED", "der letzte definierte content gewinnt")
})

await t("Anhang-Handler auf Original reaktiviert den Leak (Probe S2) — Wache nötig", async () => {
  const res = await runnerWith([
    async () => ({ content: "CLEANED" }),
    async (event) => ({ content: `${event.content}+WARN` }),
  ]).runMessageSending(EVENT, CTX)
  assert.strictEqual(res?.content, "ORIGINAL-TEXT+WARN", "genau die Gefahr, die hasReasoningLeak abwehrt")
})

// ── 3: cancel ist terminal und unterdrückt Delivery ──
await t("cancel: true ist terminal (kein späterer Handler läuft)", async () => {
  let secondRan = false
  const res = await runnerWith([
    async () => ({ cancel: true, cancelReason: "pure leak" }),
    async () => {
      secondRan = true
      return { content: "SHOULD-NOT-WIN" }
    },
  ]).runMessageSending(EVENT, CTX)
  assert.strictEqual(res?.cancel, true)
  assert.strictEqual(secondRan, false, "Kette muss nach cancel stoppen")
})

// ── 4: undefined = keine Meinung, Text bleibt ──
await t("undefined-Ergebnis lässt den Text unverändert (unsere Fail-open-Semantik)", async () => {
  const res = await runnerWith([async () => undefined]).runMessageSending(EVENT, CTX)
  assert.strictEqual(res, undefined, "kein Ergebnis → Host nutzt event.content weiter")
})

await t("Kette ohne Content-Lieferant lässt den Original-Text passieren", async () => {
  const res = await runnerWith([
    async () => undefined,
    async () => ({ cancel: false, metadata: { note: "no opinion" } }),
  ]).runMessageSending(EVENT, CTX)
  assert.ok(res?.content === undefined, "kein Handler lieferte content → Host behält content")
})

// ── 5: unsere Handler-Ergebnisse fallen in die vom Host gelesenen Felder ──
await t("thought-filter + Warn-Handler komponieren über `content` korrekt", async () => {
  const { buildThoughtFilterHandler, hasReasoningLeak } = await import("./hooks/thought-filter.ts")
  const leak = "The runtime context is just a replay.\n\nHier die Antwort. 🦊"
  const filter = buildThoughtFilterHandler()
  // Warn-Handler-Simulation mit der echten Wache unseres Codes.
  const warnLike = async (event) => {
    if (hasReasoningLeak(event.content)) return undefined
    return { content: `${event.content}\n\nWARN` }
  }
  const res = await runnerWith([filter, warnLike]).runMessageSending({ ...EVENT, content: leak }, CTX)
  assert.ok(typeof res?.content === "string", "Ergebnis muss ein content-String sein")
  assert.ok(!/replay/i.test(res.content), `Leak muss raus sein (war: ${res.content.slice(0, 60)})`)
  assert.ok(
    !res.content.includes("WARN"),
    "Warn-Anhang darf bei Leak-Text nicht greifen (sonst Leak-Resurrection)",
  )
  const clean = "Alles läuft stabil und grün. 🦊"
  const res2 = await runnerWith([filter, warnLike]).runMessageSending({ ...EVENT, content: clean }, CTX)
  assert.ok(res2.content.includes("WARN"), "auf sauberem Text greift der Warn-Anhang")
})

console.log(`\n${failed === 0 ? "OK" : "FAILED"}  host-contract (${hooksChunk})`)
process.exit(failed === 0 ? 0 : 1)
