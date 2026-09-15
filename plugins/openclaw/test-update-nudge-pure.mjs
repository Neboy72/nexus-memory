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
      console.log("FAIL ", name, "—", e.message)
    })

const TOOLS = ["nexus_search"]

await t("consumeUpdateNudge konsumiert NICHT, wenn kein Update vorliegt", () => {
  setUpdateCheckResult({ available: false, latest: "", url: "" })
  assert.deepStrictEqual(consumeUpdateNudge(), { text: null })
})

await t("buildPromptSection ist pure: 2 identische Calls → identische Ausgabe", () => {
  setUpdateCheckResult({ available: true, latest: "9.9.9", url: "https://example.invalid" })
  const a = buildPromptSection({ availableTools: new Set(TOOLS) })
  const b = buildPromptSection({ availableTools: new Set(TOOLS) })
  assert.deepStrictEqual(a, b, "identische Inputs müssen identische Outputs liefern")
  assert.ok(a.some((l) => /9\.9\.9/.test(l)), "Update-Nudge muss enthalten sein")
})

await t("buildPromptSection(nudged:true) unterdrückt den Nudge", () => {
  const withNudge = buildPromptSection({ availableTools: new Set(TOOLS), nudged: false })
  const without = buildPromptSection({ availableTools: new Set(TOOLS), nudged: true })
  assert.ok(withNudge.some((l) => /9\.9\.9/.test(l)))
  assert.ok(!without.some((l) => /9\.9\.9/.test(l)))
})

await t("consumeUpdateNudge liefert genau einmal Text, dann null", () => {
  const first = consumeUpdateNudge()
  assert.ok(first.text, "erster Consume muss Text liefern")
  assert.match(first.text, /9\.9\.9/)
  assert.deepStrictEqual(consumeUpdateNudge(), { text: null }, "zweiter Consume → null")
})

await t("Source-Contract: runtime.ts nutzt buildUpdateNudgeLines (keine Duplikate)", () => {
  const src = fs.readFileSync(new URL("./runtime.ts", import.meta.url), "utf8")
  assert.match(src, /buildUpdateNudgeLines/, "Nudge-Text muss aus lib/update-check.ts kommen")
  assert.ok(
    !/Nexus Memory update available: v\$\{/.test(src),
    "die Nudge-Zeile darf nicht mehr in runtime.ts dupliziert sein",
  )
})

process.exit(failed ? 1 : 0)
