/**
 * Regressionstest: config-Normalisierung (H7, Welle 12)
 *
 * parseConfig benutzte `(v as boolean) ?? dflt` / `(v as number) ?? 10`:
 * Strings wie "false"/"0" blieben truthy, maxRecallResults konnte NaN/string/
 * unbegrenzt sein. Jetzt: toBool/toClampedInt + Schema-Bounds minimum/maximum.
 */
import assert from "node:assert"
import { parseConfig, nexusConfigSchema } from "./lib/config.ts"
import { localEmbeddingProvider } from "./lib/embedder.ts"

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

// ── 27.09.2026: cronFormGate (universal konfigurierbares Unattended-Gate) ──
await t("cronFormGate: Default = AUS, titles [], Limits 6/900", () => {
  const g = parseConfig({}).cronFormGate
  assert.strictEqual(g.enabled, false, "neutraler Default muss AUS sein")
  assert.deepStrictEqual(g.titles, [])
  assert.strictEqual(g.maxLines, 6)
  assert.strictEqual(g.maxChars, 900)
})

await t("cronFormGate: enabled-Strings koerziert, Titel getrimmt + Nicht-Strings gefiltert", () => {
  assert.strictEqual(parseConfig({ cronFormGate: { enabled: "true" } }).cronFormGate.enabled, true)
  assert.strictEqual(parseConfig({ cronFormGate: { enabled: "0" } }).cronFormGate.enabled, false)
  const g = parseConfig({ cronFormGate: { titles: ["  ⚠️ Test Alarm  ", "", "   ", 7, null] } }).cronFormGate
  assert.deepStrictEqual(g.titles, ["⚠️ Test Alarm"])
})

await t("cronFormGate: Limits geclamped (0→1, 999→50, 10→50, 99999→5000)", () => {
  assert.strictEqual(parseConfig({ cronFormGate: { maxLines: 0 } }).cronFormGate.maxLines, 1)
  assert.strictEqual(parseConfig({ cronFormGate: { maxLines: 999 } }).cronFormGate.maxLines, 50)
  assert.strictEqual(parseConfig({ cronFormGate: { maxChars: 10 } }).cronFormGate.maxChars, 50)
  assert.strictEqual(parseConfig({ cronFormGate: { maxChars: 99999 } }).cronFormGate.maxChars, 5000)
})

await t("cronFormGate: unbekannter Key → fail-loud statt stillem Ignorieren", () => {
  assert.throws(() => parseConfig({ cronFormGate: { nope: 1 } }), /unknown keys/)
})

await t("Schema deklariert cronFormGate (Objekt, additionalProperties false)", () => {
  const props = nexusConfigSchema?.jsonSchema?.properties
  const gate = props?.cronFormGate
  assert.ok(gate && typeof gate === "object", "properties.cronFormGate fehlt — Schema-Shape hat sich geändert")
  assert.strictEqual(gate.additionalProperties, false)
  assert.ok(gate.properties?.titles, "properties.cronFormGate.titles fehlt")
})

// ── 27.09.2026: planGate (schaltbare Ebene 3; Ebene 1 bleibt unbedingt) ──
await t("planGate: Default = AUS, lockPath undefined, maxAgeSeconds 300", () => {
  const g = parseConfig({}).planGate
  assert.strictEqual(g.enabled, false, "neutraler Default muss AUS sein")
  assert.strictEqual(g.lockPath, undefined, "kein Deployment-Literal als Default")
  assert.strictEqual(g.maxAgeSeconds, 300)
})

await t("planGate: enabled-Strings koerziert, lockPath getrimmt, leer → undefined", () => {
  assert.strictEqual(parseConfig({ planGate: { enabled: "true" } }).planGate.enabled, true)
  assert.strictEqual(parseConfig({ planGate: { enabled: "0" } }).planGate.enabled, false)
  assert.strictEqual(parseConfig({ planGate: { lockPath: "  /x/y.lock  " } }).planGate.lockPath, "/x/y.lock")
  assert.strictEqual(parseConfig({ planGate: { lockPath: "   " } }).planGate.lockPath, undefined)
  assert.strictEqual(parseConfig({ planGate: { lockPath: 7 } }).planGate.lockPath, undefined)
})

await t("planGate: maxAgeSeconds geclamped (0→30, 99999→3600, 120 bleibt)", () => {
  assert.strictEqual(parseConfig({ planGate: { maxAgeSeconds: 0 } }).planGate.maxAgeSeconds, 30)
  assert.strictEqual(parseConfig({ planGate: { maxAgeSeconds: 99999 } }).planGate.maxAgeSeconds, 3600)
  assert.strictEqual(parseConfig({ planGate: { maxAgeSeconds: 120 } }).planGate.maxAgeSeconds, 120)
  assert.strictEqual(parseConfig({ planGate: { maxAgeSeconds: "abc" } }).planGate.maxAgeSeconds, 300)
})

await t("planGate: unbekannter Key → fail-loud statt stillem Ignorieren", () => {
  assert.throws(() => parseConfig({ planGate: { nope: 1 } }), /unknown keys/)
})

await t("Schema deklariert planGate (Objekt, additionalProperties false)", () => {
  const props = nexusConfigSchema?.jsonSchema?.properties
  const pg = props?.planGate
  assert.ok(pg && typeof pg === "object", "properties.planGate fehlt — Schema-Shape hat sich geändert")
  assert.strictEqual(pg.additionalProperties, false)
  assert.ok(pg.properties?.maxAgeSeconds, "properties.planGate.maxAgeSeconds fehlt")
})

await t("accessLevel: Vorgabe ist private, nicht public (Concern 10.10.2026)", () => {
  assert.strictEqual(parseConfig({}).accessLevel, "private")
  assert.strictEqual(parseConfig({ accessLevel: "public" }).accessLevel, "public")
})

await t("localEmbeddingProvider: Umgebung wird hereingereicht, nicht im Modul gelesen", () => {
  assert.strictEqual(localEmbeddingProvider({}), "nexus")
  assert.strictEqual(localEmbeddingProvider({ OLLAMA_HOST: "http://127.0.0.1:11434" }), "ollama")
  assert.strictEqual(localEmbeddingProvider({ OLLAMA_BASE_URL: "http://127.0.0.1:11434" }), "ollama")
})

process.exitCode = failed ? 1 : 0
