/**
 * Regressionstest: Self-Check-Chat-Warnung (30.09.2026)
 *
 * Prüft den message_sending-Handler aus hooks/self-check-warning.ts (lokale
 * Quelle via Node type-stripping, kein Build nötig) gegen:
 * 1. Gesunder Self-check → Content UNVERÄNDERT (Handler neutral)
 * 2. Kaputter Self-check → Warnblock angehängt, Original-Text erhalten
 * 3. Zweiter Send in derselben Session → unverändert (Throttle 1 pro Session)
 * 4. Fehlender/nicht-stringiger Content → undefined (keine Meinung)
 * 5. Unbeaufsichtigte sessionKey ("agent:main:cron:…") → unverändert
 * 6. Handler-Crash → fail-open (undefined, keine Exception nach außen)
 * 7. Bounded bookkeeping des Session-Throttles
 *
 * Beweis-Kontext (hooks-CKanWLsK.mjs): message_sending merged content mit
 * lastDefined — der letzte definierte content gewinnt; cancel:true stoppt
 * die Kette. Deshalb: dieser Handler gibt nur undefined oder {content} zurück.
 * Exit 0 = alle Cases PASS, Exit 1 = mindestens ein FAIL.
 */
import assert from "node:assert"
import { buildSelfCheckWarningHandler, resetSelfCheckWarningStateForTest } from "./hooks/self-check-warning.ts"
import { buildSelfCheckWarning, writeSelfCheck, resetSelfCheckForTest } from "./lib/self-check.ts"
import { initLogger } from "./logger.ts"

initLogger({ info() {}, warn() {}, error() {}, debug() {} }, false)

let failed = 0
let passed = 0
const t = async (name, fn) => {
  try {
    await fn()
    passed++
    console.log("PASS  ", name)
  } catch (e) {
    failed++
    console.log("FAIL  ", name, "—", e?.message || e)
  }
}

const DM_KEY = "agent:main:telegram:default:direct:5763330319"
const W_HEADER = "Nexus Memory self-check"
const BROKEN = {
  ok: false,
  reason: "Qdrant at http://localhost:6333 is unreachable (ECONNREFUSED)",
  fix: "start Qdrant or point qdrantUrl at your instance",
}

// Jeder Case startet mit gesundem Self-check und leerem Throttle.
const freshHandler = () => {
  resetSelfCheckForTest()
  resetSelfCheckWarningStateForTest()
  return buildSelfCheckWarningHandler()
}

const healthy = "Alles läuft stabil und grün. 🦊"

// ── 1: gesund → unverändert ──
await t("Gesunder Self-check → Content unverändert (undefined = keine Meinung)", async () => {
  const h = freshHandler()
  assert.strictEqual(await h({ to: "telegram:x", content: healthy }, { sessionKey: DM_KEY }), undefined)
})

// ── 2: kaputt → Anhang, Original erhalten ──
await t("Kaputter Self-check → Warnblock angehängt, Original-Text 1:1 erhalten", async () => {
  const h = freshHandler()
  writeSelfCheck(BROKEN.ok, BROKEN.reason, BROKEN.fix)
  assert.ok(buildSelfCheckWarning().length > 0, "Fixture: buildSelfCheckWarning muss non-empty sein")
  const res = await h({ to: "telegram:x", content: healthy }, { sessionKey: DM_KEY })
  assert.ok(res, "kaputt → Handler MUSS ein Ergebnis liefern")
  assert.strictEqual(res.cancel, undefined, "Handler darf NIEMALS cancel setzen")
  assert.ok(typeof res.content === "string" && res.content.length > 0, "content muss nicht-leerer String sein")
  assert.ok(res.content.startsWith(healthy), "Original-Text muss unverändert am Anfang stehen")
  assert.ok(res.content.includes(W_HEADER), "Warnblock-Kopf muss enthalten sein")
  assert.ok(res.content.includes(BROKEN.reason), "Cause muss durchgereicht sein")
  assert.ok(res.content.includes(BROKEN.fix), "Fix muss durchgereicht sein")
  assert.ok(res.content.split("\n\n").length >= 2, "Warnblock muss per Leerzeile abgetrennt sein")
  // ≤ 4 Zeilen Warnblock + Trennung + Original.
  const blockLines = res.content.slice(healthy.length).trim().split("\n")
  assert.ok(blockLines.length <= 4, `Warnblock muss ≤ 4 Zeilen haben, hat ${blockLines.length}`)
})

// ── 3: Throttle — zweiter Send in derselben Session bleibt unverändert ──
await t("Throttle: zweiter Send in derselben Session → unverändert", async () => {
  const h = freshHandler()
  writeSelfCheck(BROKEN.ok, BROKEN.reason, BROKEN.fix)
  const first = await h({ to: "telegram:x", content: healthy }, { sessionKey: DM_KEY })
  assert.ok(first && first.content.startsWith(healthy), "erster Send muss den Anhang tragen")
  const second = await h({ to: "telegram:x", content: healthy }, { sessionKey: DM_KEY })
  assert.strictEqual(second, undefined, "zweiter Send in derselben Session muss neutral sein")
  const other = await h({ to: "telegram:x", content: healthy }, { sessionKey: DM_KEY + "-andere" })
  assert.ok(other && other.content.startsWith(healthy), "andere Session darf separat gewarnt werden")
})

// ── 4: fehlender / nicht-stringiger Content → undefined ──
await t("Fehlender Content (undefined) → undefined (keine Meinung)", async () => {
  const h = freshHandler()
  writeSelfCheck(BROKEN.ok, BROKEN.reason, BROKEN.fix)
  assert.strictEqual(await h({}, { sessionKey: DM_KEY }), undefined)
})

await t("Nicht-stringiger Content (Objekt) → undefined (keine Meinung)", async () => {
  const h = freshHandler()
  writeSelfCheck(BROKEN.ok, BROKEN.reason, BROKEN.fix)
  const res = await h({ to: "telegram:x", content: { body: healthy } }, { sessionKey: DM_KEY })
  assert.strictEqual(res, undefined, "Objekt-Content darf NIE einen Anhang erzeugen")
})

await t("Whitespace-only Content → undefined", async () => {
  const h = freshHandler()
  writeSelfCheck(BROKEN.ok, BROKEN.reason, BROKEN.fix)
  assert.strictEqual(await h({ to: "telegram:x", content: "   \n  " }, { sessionKey: DM_KEY }), undefined)
})

await t("Event null/undefined → undefined (kein Crash)", async () => {
  const h = freshHandler()
  writeSelfCheck(BROKEN.ok, BROKEN.reason, BROKEN.fix)
  assert.strictEqual(await h(null, { sessionKey: DM_KEY }), undefined)
  assert.strictEqual(await h(undefined, { sessionKey: DM_KEY }), undefined)
})

// ── 5: unbeaufsichtigte Sessions bleiben unangetastet ──
await t("Unbeaufsichtigt (cron-sessionKey) → unverändert, auch bei kaputtem Memory", async () => {
  const h = freshHandler()
  writeSelfCheck(BROKEN.ok, BROKEN.reason, BROKEN.fix)
  const cronKey = "agent:main:cron:98d4e5bb-7971-4348-805b-2a38b640878f:run:9b65cf0a"
  assert.strictEqual(await h({ to: "telegram:x", content: healthy }, { sessionKey: cronKey }), undefined)
  assert.strictEqual(await h({ to: "telegram:x", content: healthy }, { sessionKey: "agent:main:main:heartbeat" }), undefined)
})

await t("SessionKey fehlt → Warnung geht (Key '(ohne sessionKey)', throttle-safe)", async () => {
  const h = freshHandler()
  writeSelfCheck(BROKEN.ok, BROKEN.reason, BROKEN.fix)
  const res = await h({ to: "telegram:x", content: healthy }, {})
  assert.ok(res && res.content.startsWith(healthy), "fehlender sessionKey darf die Warnung nicht killen")
  assert.strictEqual(await h({ to: "telegram:x", content: healthy }, undefined), undefined, "zweiter Send ohne sessionKey muss vom selben Throttle-Key erfasst sein")
})

// ── 5b: Clobber-Wache — leak-tragender Text wird NIE angefasst ──
// Kompositions-Gefahr (Probe oc-composition-probe.mjs, S2): jeder Handler sieht
// das ORIGINAL-Event, der Host nimmt aber den LETZTEN `content`. Hinge dieser
// Hook an leak-tragenden Text an, würde der bereinigte Text des thought-filter
// durch Original+Anhang ersetzt und der Leak ginge wieder raus. Deshalb:
// undefined bei erkanntem Leak — und zwar für BEIDE Leak-Formen (Text mit
// Antwort drin und reiner Leak) und ohne die Session zu verbrauchen.
await t("Clobber-Wache: Leak-tragender Text → undefined (Filter-Text bleibt unangetastet)", async () => {
  const h = freshHandler()
  writeSelfCheck(BROKEN.ok, BROKEN.reason, BROKEN.fix)
  const mixedLeak = "The runtime context is just a replay.\n\nHier die eigentliche Antwort. 🦊"
  assert.strictEqual(
    await h({ to: "telegram:x", content: mixedLeak }, { sessionKey: DM_KEY }),
    undefined,
    "Leak + Antwort darf nicht angehängt werden (sonst überschreibt der Rest den Filter-Text)",
  )
  const pureLeak = "Let me work through this task. Steps:\n\n1. Fetch the Atom feed"
  assert.strictEqual(
    await h({ to: "telegram:x", content: pureLeak }, { sessionKey: DM_KEY }),
    undefined,
    "reiner Leak darf nicht angehängt werden (würde die Unterdrückung wiederbeleben)",
  )
  // Die Wache darf den Throttle NICHT verbrauchen — sauberer Text muss danach
  // weiterhin genau eine Warnung bekommen.
  const clean = await h({ to: "telegram:x", content: healthy }, { sessionKey: DM_KEY })
  assert.ok(clean && clean.content.startsWith(healthy), "sauberer Text muss danach noch gewarnt werden")
})

await t("Crash im Handler (Logger wirft) → fail-open, keine Exception nach außen", async () => {
  resetSelfCheckForTest()
  resetSelfCheckWarningStateForTest()
  writeSelfCheck(BROKEN.ok, BROKEN.reason, BROKEN.fix)
  initLogger({ info() {}, warn() { throw new Error("simulierter Crash") }, error() {}, debug() {} }, false)
  try {
    const h = buildSelfCheckWarningHandler()
    assert.strictEqual(
      await h({ to: "telegram:x", content: healthy }, { sessionKey: DM_KEY }),
      undefined,
      "Crash-Fail-open muss undefined liefern (keine Meinung)",
    )
  } finally {
    initLogger({ info() {}, warn() {}, error() {}, debug() {} }, false)
  }
})

// ── 7: Throttle-Buchhaltung bleibt bounded ──
await t("Bounded bookkeeping: 400 Sessions laufen durch, Set wirft nicht", async () => {
  const h = freshHandler()
  writeSelfCheck(BROKEN.ok, BROKEN.reason, BROKEN.fix)
  for (let i = 0; i < 400; i++) {
    await h({ to: "telegram:x", content: healthy }, { sessionKey: `agent:main:telegram:direct:${i}` })
  }
})

// Reset: State sauber hinterlassen.
resetSelfCheckForTest()
resetSelfCheckWarningStateForTest()

console.log(`\n${passed}/${passed + failed} PASS`)
process.exitCode = failed === 0 ? 0 : 1