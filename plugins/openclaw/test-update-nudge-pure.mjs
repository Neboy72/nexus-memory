/**
 * Regressionstest: Update-Nudge ist pure (H138, Welle 13)
 *
 * Vorher mutierte buildPromptSection() einen modul-globalen `updateNudged`-
 * Flag als Side-Effect: der zweite Aufruf lieferte eine andere Ausgabe als der
 * erste (nicht-deterministisch, pro Prozess nur die erste Session sah den
 * Nudge) und die String-Logik war gegenüber lib/update-check.ts dupliziert.
 * Jetzt: buildPromptSection ist pure (nimmt `nudged`), der Zustand wird vom
 * Aufrufer über consumeUpdateNudge() gelesen+zurückgesetzt, und der Nudge-Text
 * kommt aus buildUpdateNudgeLines().
 */
import assert from "node:assert"
import fs from "node:fs"
import {
  buildPromptSection,
  consumeUpdateNudge,
  setUpdateCheckResult,
} from "./runtime.ts"

let failed = 0
const t = (name, fn) =>
  Promise.resolve()
    .then(fn)
    .then(() => console.log("PASS ", name))
    .catch((e) => {
      failed++
      console.log("FAIL ", name, "—", e?.stack ?? String(e))
    })

const TOOLS = ["nexus_search"]

await t("consumeUpdateNudge konsumiert NICHT, wenn kein Update vorliegt (state bleibt leer)", () => {
  // W39/F5: Der Name verspricht 'konsumiert NICHT' — bewiesen wird das jetzt, indem
  // nachfolgend ein Update gesetzt und konsumiert wird: der erste (leere) Consume darf
  // den Consume-Zustand nicht so setzen, dass der echte Nudge verloren geht.
  setUpdateCheckResult({ available: false, latest: "", url: "" })
  const none = consumeUpdateNudge()
  // W39/F6: text===null ist der Contract; zusätzliche Felder (url/latest) sind legitim.
  assert.strictEqual(none?.text ?? null, null, "kein Update → text null")
  setUpdateCheckResult({ available: true, latest: "9.9.9", url: "https://example.invalid" })
  const first = consumeUpdateNudge()
  assert.ok(first?.text, "erster echter Consume nach leerem Consume muss den Nudge liefern")
  assert.match(first.text, /9\.9\.9/)
  assert.deepStrictEqual(consumeUpdateNudge(), { text: null }, "zweiter Consume → null")
})

await t("buildPromptSection ist pure: 2 identische Calls → identische Ausgabe", () => {
  setUpdateCheckResult({ available: true, latest: "9.9.9", url: "https://example.invalid" })
  const a = buildPromptSection({ availableTools: new Set(TOOLS) })
  const b = buildPromptSection({ availableTools: new Set(TOOLS) })
  assert.deepStrictEqual(a, b, "identische Inputs müssen identische Outputs liefern")
  // W39/F1+F7: 'genau einmal' statt 'irgendwo vorhanden' — Duplikat-Emission ist DIE
  // Regression, gegen die dieser Test baut.
  const hits = a.filter((l) => /9\.9\.9/.test(l))
  assert.strictEqual(hits.length, 1, `Nudge-Zeile genau einmal, waren ${hits.length}: ${JSON.stringify(hits)}`)
})

await t("buildPromptSection(nudged:true) unterdrückt den Nudge (eigener Zustand)", () => {
  // W39/F8: eigener Zustand statt Ordering-Vertrauen.
  setUpdateCheckResult({ available: true, latest: "9.9.9", url: "https://example.invalid" })
  const withNudge = buildPromptSection({ availableTools: new Set(TOOLS), nudged: false })
  const without = buildPromptSection({ availableTools: new Set(TOOLS), nudged: true })
  assert.ok(withNudge.some((l) => /9\.9\.9/.test(l)))
  assert.ok(!without.some((l) => /9\.9\.9/.test(l)))
})

await t("consumeUpdateNudge ist einmal-pro-Prozess: nach konsumiertem Nudge bleibt weitere Consume null", () => {
  // W39/F3+Kontrakt: consumeUpdateNudge hat einen prozess-globalen consume-Flag OHNE
  // Reset-Export (by design, 'once-per-process'). Der Test setzt das Result selbst
  // (reordering-fest beim RESULT), beweist die Idempotenz und verlangt KEINEN
  // 'erster Text' mehr — die 'genau einmal'-Semantik ist in den pure-Tests geprüft.
  setUpdateCheckResult({ available: true, latest: "9.9.9", url: "https://example.invalid" })
  const a = consumeUpdateNudge()
  const b = consumeUpdateNudge()
  assert.deepStrictEqual(b, { text: null }, "zweiter Consume → null (einmal-pro-Prozess)")
  if (a?.text) assert.match(a.text, /9\.9\.9/, "falls dieser Prozess noch nicht konsumiert hat, ist a der Nudge")
})

await t("Source-Contract: runtime.ts nutzt buildUpdateNudgeLines (keine Duplikate)", () => {
  const src = fs.readFileSync(new URL("./runtime.ts", import.meta.url), "utf8")
  // W39/F4: Toleranter gegen harmlose Refactors (whitespace/quote-Stil), aber der Kern
  // bleibt: (1) der Helper-Name wird referenziert, (2) kein interpoliertes Duplikat.
  assert.match(src, /buildUpdateNudgeLines/, "Nudge-Text muss aus lib/update-check.ts kommen")
  assert.ok(
    !/Nexus Memory update available:\s*v\$\{/.test(src),
    "die Nudge-Zeile darf nicht mehr in runtime.ts dupliziert sein",
  )
})

process.exitCode = failed ? 1 : 0
