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
      console.log("FAIL ", name, "—", e.message)
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

await t("maxRecallResults: String '15' → 15", () => {
  assert.strictEqual(parseConfig({ maxRecallResults: "15" }).maxRecallResults, 15)
})

await t("maxRecallResults: 999 → 20 (geclamped)", () => {
  assert.strictEqual(parseConfig({ maxRecallResults: 999 }).maxRecallResults, 20)
})

await t("maxRecallResults: 0 → 1 (geclamped)", () => {
  assert.strictEqual(parseConfig({ maxRecallResults: 0 }).maxRecallResults, 1)
})

await t("maxRecallResults: nicht-numerisch → Default 10", () => {
  assert.strictEqual(parseConfig({ maxRecallResults: "abc" }).maxRecallResults, 10)
  assert.strictEqual(parseConfig({ maxRecallResults: NaN }).maxRecallResults, 10)
  assert.strictEqual(parseConfig({}).maxRecallResults, 10)
})

await t("Schema deklariert minimum 1 / maximum 20", () => {
  const schema = nexusConfigSchema.jsonSchema.properties.maxRecallResults
  assert.strictEqual(schema.minimum, 1)
  assert.strictEqual(schema.maximum, 20)
})

process.exit(failed ? 1 : 0)
