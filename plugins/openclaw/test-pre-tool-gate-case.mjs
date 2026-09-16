/**
 * Regressionstest: Guardrail-Case-Normalisierung (A1)
 *
 * checkGuardrails() must lowercase params.command before matching against
 * PROTECTED_PATHS / the kill-ollama check. Before the fix, an uppercase
 * command such as `RM -RF /Users/miosha/.hermes` or `KILL ollama` slipped
 * past the guardrail (it only matched lowercase literals).
 *
 * Wir importieren den TS-Handler direkt (Node type-stripping) und prüfen
 * das Verhalten am echten Handler — kein Build nötig.
 */
import assert from "node:assert"
import os from "node:os"
import { buildPreToolGateHandler } from "./hooks/pre-tool-gate.ts"

let failed = 0
const t = (name, fn) =>
  fn()
    .then(() => console.log("PASS ", name))
    .catch((e) => {
      failed++
      console.log("FAIL ", name, "—", e.message)
    })

// Guardrail blocks before any embedder/Qdrant use, so stubs suffice.
const handler = buildPreToolGateHandler(
  { embed: async () => [] },
  { search: async () => [] },
  { accessLevel: "private" },
)

const exec = (command) => handler({ toolName: "exec", params: { command } }, {})

// PROTECTED_PATHS expandiert ~ gegen os.homedir() — der Test nutzt denselben
// Pfad, damit die Regression auf jeder Maschine (CI inklusive) greift.
const HOME = os.homedir()

await t("uppercase RM -RF auf geschütztem Pfad → BLOCK (Guardrail, nicht Plan-Gate)", async () => {
  const res = await exec(`RM -RF ${HOME}/.hermes`)
  assert.ok(res && res.block === true, "uppercase rm -rf muss geblockt werden")
  assert.match(res.blockReason, /verboten|BLOCKED.*rm/i, `Guardrail-Grund erwartet, bekam: ${res.blockReason}`)
})

await t("uppercase KILL ollama → BLOCK (Guardrail)", async () => {
  const res = await exec("KILL ollama")
  assert.ok(res && res.block === true, "uppercase KILL ollama muss geblockt werden")
  assert.match(res.blockReason, /Ollama/i, `Guardrail-Grund erwartet, bekam: ${res.blockReason}`)
})

await t("kleingeschriebenes rm -rf bleibt geblockt", async () => {
  const res = await exec(`rm -rf ${HOME}/.hermes`)
  assert.ok(res && res.block === true, "rm -rf auf geschütztem Pfad muss geblockt werden")
})

await t("unschädliches Kommando wird nicht vom Guardrail geblockt", async () => {
  const res = await exec("ls -la /tmp")
  assert.ok(!res || res.block !== true, "ls darf nicht geblockt werden")
})

// ── H2: Wortgrenzen statt Substring (kill/pkill/killall + ollama) ──

await t("H2: 'skill'-Substring + 'ollama' → KEIN Ollama-Guardrail-Block mehr", async () => {
  // Vor dem Fix: command.includes("kill") matcht "skill.md" → False-Positive-Block.
  const res = await exec("cat skill.md in ~/ollama-notes")
  assert.ok(
    !res?.blockReason || !/Ollama killen/.test(res.blockReason),
    `Guardrail-False-Positive nicht behoben, Grund: ${res?.blockReason}`,
  )
})

await t("H2: pkill -f ollama → weiter Guardrail-BLOCK", async () => {
  const res = await exec("pkill -f ollama runner")
  assert.ok(res && res.block === true, "pkill auf ollama muss geblockt werden")
  assert.match(res.blockReason, /Ollama/i, `Guardrail-Grund erwartet, bekam: ${res.blockReason}`)
})

await t("H2: killall ollama → weiter Guardrail-BLOCK", async () => {
  const res = await exec("killall ollama")
  assert.ok(res && res.block === true, "killall auf ollama muss geblockt werden")
  assert.match(res.blockReason, /Ollama/i, `Guardrail-Grund erwartet, bekam: ${res.blockReason}`)
})

process.exit(failed ? 1 : 0)
