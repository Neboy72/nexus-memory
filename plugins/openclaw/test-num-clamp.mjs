/**
 * Regressionstest: Tool-Limit-Clamping (H123, Welle 13)
 *
 * Tool-Parameter kommen vom Host/Modell. Ohne Clamping landete ein roher Wert
 * direkt in Qdrant (limit=1e9) bzw. in der BFS-Schleife (negatives/NaN-Depth).
 * clampInt() ist die eine Grenze; zusätzlich prüfen wir per Source-Contract,
 * dass jedes Tool sie auch wirklich anwendet und das Schema die Maxima nennt.
 */
import assert from "node:assert"
import fs from "node:fs"
import { clampInt } from "./lib/num.ts"

let failed = 0
const t = (name, fn) =>
  Promise.resolve()
    .then(fn)
    .then(() => console.log("PASS ", name))
    .catch((e) => {
      failed++
      console.log("FAIL ", name, "—", e.message)
    })

// ── clampInt direkt ─────────────────────────────────────────────────────────

await t("clampInt: Bruchzahlen werden Richtung Null abgeschnitten", () => {
  assert.strictEqual(clampInt(3.9, 5, 1, 50), 3)
  assert.strictEqual(clampInt(-3.9, 5, 1, 50), 1, "negativ → min-Grenze")
})

await t("clampInt: NaN/Infinity/undefined/null → default", () => {
  assert.strictEqual(clampInt(NaN, 5, 1, 50), 5)
  assert.strictEqual(clampInt(Infinity, 5, 1, 50), 5)
  assert.strictEqual(clampInt(-Infinity, 5, 1, 50), 5)
  assert.strictEqual(clampInt(undefined, 5, 1, 50), 5)
  assert.strictEqual(clampInt(null, 5, 1, 50), 5)
  assert.strictEqual(clampInt({}, 5, 1, 50), 5)
  assert.strictEqual(clampInt([], 5, 1, 50), 5)
})

await t("clampInt: String-Zahlen werden akzeptiert, Müll nicht", () => {
  assert.strictEqual(clampInt("12", 5, 1, 50), 12)
  assert.strictEqual(clampInt(" 7 ", 5, 1, 50), 7)
  assert.strictEqual(clampInt("abc", 5, 1, 50), 5)
  assert.strictEqual(clampInt("", 5, 1, 50), 5)
})

await t("clampInt: negative Zahlen werden nicht als 'fehlend' gewertet", () => {
  assert.strictEqual(clampInt(-5, 5, 1, 50), 1)
  assert.strictEqual(clampInt(-0, 5, 1, 50), 1)
})

await t("clampInt: Grenzen werden eingehalten", () => {
  assert.strictEqual(clampInt(999, 5, 1, 50), 50)
  assert.strictEqual(clampInt(0, 5, 1, 50), 1)
  assert.strictEqual(clampInt(1, 5, 1, 50), 1)
  assert.strictEqual(clampInt(50, 5, 1, 50), 50)
  assert.strictEqual(clampInt(51, 5, 1, 50), 50)
})

await t("clampInt: auch ein kaputter default kann die Grenzen nicht verlassen", () => {
  assert.strictEqual(clampInt(undefined, 999, 1, 50), 50)
  assert.strictEqual(clampInt(undefined, -3, 1, 50), 1)
})

// ── Source-Contract: jedes Tool wendet clampInt an ──────────────────────────

const read = (rel) => fs.readFileSync(new URL(rel, import.meta.url), "utf8")

await t("search.ts: limit läuft durch clampInt(…, 5, 1, 50)", () => {
  const src = read("./tools/search.ts")
  assert.match(src, /clampInt\(params\.limit,\s*SEARCH_LIMIT_DEFAULT,\s*1,\s*SEARCH_LIMIT_MAX\)/)
  assert.ok(!/const limit = params\.limit \?\? 5/.test(src), "roher params.limit darf nicht mehr durchgereicht werden")
})

await t("graph_traverse.ts: alle drei Limits/Depths laufen durch clampInt", () => {
  const src = read("./tools/graph_traverse.ts")
  assert.match(src, /clampInt\(params\.max_depth,\s*TRAVERSE_DEPTH_DEFAULT,\s*1,\s*TRAVERSE_DEPTH_MAX\)/)
  assert.match(src, /clampInt\(params\.limit,\s*ENTITIES_LIMIT_DEFAULT,\s*1,\s*ENTITIES_LIMIT_MAX\)/)
  assert.match(src, /clampInt\(params\.max_depth,\s*SUBGRAPH_DEPTH_DEFAULT,\s*1,\s*SUBGRAPH_DEPTH_MAX\)/)
})

await t("Schemata spiegeln die Maxima (minimum/maximum)", () => {
  const search = read("./tools/search.ts")
  const graph = read("./tools/graph_traverse.ts")
  assert.match(search, /maximum: SEARCH_LIMIT_MAX/)
  assert.match(search, /minimum: 1/)
  assert.match(graph, /maximum: TRAVERSE_DEPTH_MAX/)
  assert.match(graph, /maximum: ENTITIES_LIMIT_MAX/)
  assert.match(graph, /maximum: SUBGRAPH_DEPTH_MAX/)
})

process.exit(failed ? 1 : 0)
