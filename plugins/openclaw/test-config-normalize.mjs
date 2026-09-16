/**
 * Regressionstest: config-Normalisierung (H7, Welle 12)
 *
 * parseConfig benutzte `(v as boolean) ?? dflt` / `(v as number) ?? 10`:
 * Strings wie "false"/"0" blieben truthy, maxRecallResults konnte NaN/string/
 * unbegrenzt sein. Jetzt: toBool/toClampedInt + Schema-Bounds minimum/maximum.
 */
import assert from "node:assert"
import { parseConfig, nexusConfigSchema } from "./lib/config.ts"

let failed = 0
const t = (name, fn) =>
  Promise.resolve()
    .then(fn)
    .then(() => console.log("PASS ", name))
    .catch((e) => {
      failed++
      console.log("FAIL ", name, "—", e?.stack ?? String(e))
    })

await t("String 'false' → false, '0' → false (nicht mehr truthy)", () => {
  assert.strictEqual(parseConfig({ autoRecall: "false" }).autoRecall, false)
  assert.strictEqual(parseConfig({ autoRecall: "0" }).autoRecall, false)
  assert.strictEqual(parseConfig({ debug: "0" }).debug, false)
  assert.strictEqual(parseConfig({ debug: "false" }).debug, false)
})

await t("String 'true'/'1' → true", () => {
  assert.strictEqual(parseConfig({ debug: "true" }).debug, true)
  assert.strictEqual(parseConfig({ debug: "1" }).debug, true)
})

await t("echte Booleans bleiben erhalten, Defaults unverändert", () => {
  assert.strictEqual(parseConfig({ autoRecall: false }).autoRecall, false)
  assert.strictEqual(parseConfig({}).autoRecall, true)
  assert.strictEqual(parseConfig({}).autoCapture, true)
  assert.strictEqual(parseConfig({}).thoughtFilter, true)
  assert.strictEqual(parseConfig({}).debug, false)
})
await t("autoCapture + thoughtFilter: truthy-String-Bug (H7-Kern) auch auf diesen Feldern", () => {
  // W39/F8: '0'/'false' müssen auf ALLEN bool-Feldern falsifizieren, nicht nur auf
  // autoRecall/debug — genau die Regression, die H7 schloss.
  assert.strictEqual(parseConfig({ autoCapture: "0" }).autoCapture, false)
  assert.strictEqual(parseConfig({ autoCapture: "false" }).autoCapture, false)
  assert.strictEqual(parseConfig({ thoughtFilter: "0" }).thoughtFilter, false)
  assert.strictEqual(parseConfig({ thoughtFilter: "false" }).thoughtFilter, false)
  assert.strictEqual(parseConfig({ autoCapture: "1" }).autoCapture, true)
  assert.strictEqual(parseConfig({ thoughtFilter: "true" }).thoughtFilter, true)
})

await t("maxRecallResults: String '15' → 15", () => {
  assert.strictEqual(parseConfig({ maxRecallResults: "15" }).maxRecallResults, 15)
})

await t("maxRecallResults: 999 → 20 (geclamped)", () => {
  assert.strictEqual(parseConfig({ maxRecallResults: 999 }).maxRecallResults, 20)
})

await t("maxRecallResults: 0 → 1 (geclamped)", () => {
  assert.strictEqual(parseConfig({ maxRecallResults: 0 }).maxRecallResults, 1)
})

// W39/F4+F7: toClampedInt-Kontrakt-Vollzug — Grenzfälle, die derselbe Coercion-Pfad
// entscheidet (Fractional truncation, Infinity, negative, Whitespace-Strings).
await t("maxRecallResults: 3.7 → 3 (Fractional truncation Richtung Null)", () => {
  assert.strictEqual(parseConfig({ maxRecallResults: 3.7 }).maxRecallResults, 3)
  assert.strictEqual(parseConfig({ maxRecallResults: -3.7 }).maxRecallResults, 1, "negativer Fractional clamped auf minimum")
})
await t("maxRecallResults: Infinity → Default 10 (nicht-finite = fehlend), -5 → 1", () => {
  // Kontrakt (toClampedInt): !Number.isFinite → dflt. Infinity/NaN sind 'fehlend',
  // keine Clamps. Negative finite Zahlen werden auf minimum geclamped.
  assert.strictEqual(parseConfig({ maxRecallResults: Infinity }).maxRecallResults, 10)
  assert.strictEqual(parseConfig({ maxRecallResults: -5 }).maxRecallResults, 1)
})
await t("maxRecallResults: whitespace-String → 0-koerziert → clamp auf 1, String '0' → 1", () => {
  // Kontrakt: Number("   ") = 0 (finite) → clamp auf min=1. '0' ebenso. Beide sind
  // KEIN Default-Fall (Fund verlangte Coverage dieser Koerzions-Sonderfälle).
  assert.strictEqual(parseConfig({ maxRecallResults: "   " }).maxRecallResults, 1)
  assert.strictEqual(parseConfig({ maxRecallResults: "0" }).maxRecallResults, 1)
})
await t("maxRecallResults: nicht-numerisch → Default 10", () => {
  assert.strictEqual(parseConfig({ maxRecallResults: "abc" }).maxRecallResults, 10)
  assert.strictEqual(parseConfig({ maxRecallResults: NaN }).maxRecallResults, 10)
  assert.strictEqual(parseConfig({}).maxRecallResults, 10)
})

await t("Schema deklariert minimum 1 / maximum 20", () => {
  // W39/F3+F6: Schema-Pfad klar diagnostizieren statt TypeError-Blindflug.
  const props = nexusConfigSchema?.jsonSchema?.properties
  assert.ok(props && typeof props === "object", "nexusConfigSchema.jsonSchema.properties fehlt — Schema-Shape hat sich geändert")
  const schema = props.maxRecallResults
  assert.ok(schema && typeof schema === "object", "properties.maxRecallResults fehlt — Feld umbenannt?")
  assert.strictEqual(schema.minimum, 1)
  assert.strictEqual(schema.maximum, 20)
})

process.exitCode = failed ? 1 : 0
