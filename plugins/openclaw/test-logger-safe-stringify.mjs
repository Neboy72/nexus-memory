/**
 * Regressionstest: logger safeStringify (H9, Welle 12)
 *
 * debugRequest/debugResponse riefen JSON.stringify roh auf: Zyklen/BigInt
 * warfen, Payloads (inkl. Secrets) wurden unbefiltert gedumpt. Jetzt:
 * safeStringify mit Zyklus-/BigInt-Schutz und Secret-Redaktion.
 */
import assert from "node:assert"
import { safeStringify } from "./logger.ts"

let failed = 0
// Fund W37 (medium/low): async-Harness mit Stack + Timeout — assertion-Diffs
// (e.expected/e.actual) und Haenger sind in CI sonst nicht diagnostizierbar.
const t = async (name, fn) => {
  try {
    await Promise.race([
      Promise.resolve().then(fn),
      new Promise((_, rej) => setTimeout(() => rej(new Error("timeout: test hing 10s")), 10000)),
    ])
    console.log("PASS ", name)
  } catch (e) {
    failed++
    console.log("FAIL ", name, "—", (e && e.stack) || String(e))
  }
}

await t("zirkuläres Objekt → kein Wurf, enthält '[circular]'", () => {
  const o = { name: "x" }
  o.self = o
  let out
  assert.doesNotThrow(() => { out = safeStringify(o) })
  assert.match(out, /\[circular\]/)
})

await t("BigInt → String-Repräsentation, kein Wurf", () => {
  const out = safeStringify({ n: 12345678901234567890n })
  // Fund W37 (low): geparst pruefen statt Regex — BigInt MUSS als JSON-String
  // serialisiert werden (raw number = Praezisionsverlust beim Consumer).
  const parsed = JSON.parse(out)
  assert.strictEqual(parsed.n, "12345678901234567890", "BigInt muss als String serialisiert werden")
  assert.ok(!out.includes(": 12345678901234567890,"), "kein raw number")
})

await t("Secret-Keys werden redigiert", () => {
  const out = safeStringify({ apiKey: "sk-xyz", nested: { authorization: "Bearer abc" } })
  // Fund W37 (medium): pro Key geparst pruefen statt loser Regex.
  const parsed = JSON.parse(out)
  assert.strictEqual(parsed.apiKey, "[redacted]", "apiKey muss redigiert sein")
  assert.strictEqual(parsed.nested.authorization, "[redacted]", "authorization muss redigiert sein")
  assert.ok(!out.includes("sk-xyz"), "apiKey-Wert darf nicht im Log stehen")
  assert.ok(!out.includes("Bearer abc"), "authorization darf nicht im Log stehen")
})

await t("normale Payload bleibt 1:1", () => {
  const payload = { text: "hallo", count: 3, list: [1, 2], ok: true }
  assert.deepStrictEqual(JSON.parse(safeStringify(payload)), payload)
})

await t("undefined → definierter String, kein Wurf", () => {
  assert.strictEqual(safeStringify(undefined), "undefined")
})

await t("Funktion → String-Fallback statt Wurf", () => {
  // Fund W37 (medium): documented fallback contract verifizieren — kein nur-doesNotThrow.
  const out = safeStringify(() => {})
  assert.strictEqual(typeof out, "string", "Kontrakt: immer ein String")
  // Implementierung: top-level function → JSON.stringify undefined → String(v).
  assert.strictEqual(out, "() => {}", "Fallback ist String(v): " + out)
})
await t("getter, der wirft → catch-branch als [unserializable]", () => {
  // Fund W37 (medium): der einzige Zweig, der den inneren catch erreicht.
  const evil = { get boom() { throw new Error("getter-explode") } }
  const out = safeStringify({ evil })
  assert.strictEqual(typeof out, "string")
  assert.ok(out.includes("unserializable"), "wurfender getter muss als unserializable landen, got: " + out)
})

// Fund W37 (medium): Integration — die beiden Entry-Points muessen wirklich
// durch safeStringify laufen (Regression: zurueck auf rohes JSON.stringify waere
// sonst unsichtbar). Capturing-Backend + initLogger.
await t("debugRequest/debugResponse routen durch safeStringify (Zyklus+Secret)", async () => {
  const { initLogger, log } = await import("./logger.ts")
  const captured = []
  initLogger({ info: () => {}, warn: () => {}, error: () => {}, debug: (...a) => captured.push(a) }, true)
  try {
    const cyc = { q: "geheim" }
    cyc.self = cyc
    log.debugRequest("search", { apiKey: "sk-leak", payload: cyc })
    log.debugResponse("search", { token: "tok-123" })
    const flat = JSON.stringify(captured)
    assert.ok(!flat.includes("sk-leak"), "debugRequest darf Secrets nicht roh dumpen")
    assert.ok(!flat.includes("tok-123"), "debugResponse darf Secrets nicht roh dumpen")
    assert.ok(flat.includes("[redacted]"), "Redaktion muss in debugRequest/Response ankommen")
    assert.ok(flat.includes("[circular]"), "Zyklus-Schutz muss durch die Entry-Points wirken")
  } finally {
    // Harness-Backend neutralisieren (kein Einfluss auf Folgetests)
    initLogger({ info: () => {}, warn: () => {}, error: () => {}, debug: () => {} }, false)
  }
})

// Fund W37 (low/medium): exitCode statt process.exit — Hard-Exit kappt gepufferte
// stdout-Ausgabe (PASS/FAIL-Diagnosen) bei gepipestem stdout (CI).
process.exitCode = failed ? 1 : 0
