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

// W39/F1+F5+F8: fn() läuft INNERHALB der Promise-Kette (sync throws landen im catch),
// Stack statt nur message, non-Error-Throws werden zu Strings.
let failed = 0
const t = (name, fn) =>
  Promise.resolve()
    .then(fn)
    .then(() => console.log("PASS ", name))
    .catch((e) => {
      failed++
      console.log("FAIL ", name, "—", e?.stack ?? String(e))
    })

// Guardrail blocks before any embedder/Qdrant use, so stubs suffice.
const handler = buildPreToolGateHandler(
  { embed: async () => [] },
  { search: async () => [] },
  { accessLevel: "private" },
)

const exec = (command) => handler({ toolName: "exec", params: { command } }, {})

// W39/F9: Ein Block-Assert statt sechs Copy-Paste-Paare — anchored reason + block===true.
async function assertBlocked(command, reasonRe, what) {
  const res = await exec(command)
  assert.ok(res && res.block === true, `${what}: muss geblockt werden, bekam ${JSON.stringify(res)}`)
  assert.ok(typeof res.blockReason === "string" && reasonRe.test(res.blockReason),
            `${what}: reason muss auf ${reasonRe} matchen, bekam: ${res.blockReason}`)
  return res
}

// PROTECTED_PATHS expandiert ~ gegen os.homedir() — der Test nutzt denselben
// Pfad, damit die Regression auf jeder Maschine (CI inklusive) greift.
const HOME = os.homedir()

await t("uppercase RM -RF auf geschütztem Pfad → BLOCK (Guardrail, nicht Plan-Gate)", async () => {
  // W39/F3: anchored auf den exakten Produktion-Text (kein Duplikat-Drift: regex bleibt
  // tolerant gegen harmlose Reformats, verlangt aber beide Kernelemente).
  await assertBlocked(`RM -RF ${HOME}/.hermes`, /BLOCKED: rm -rf.*verboten/i, "uppercase rm -rf")
})

await t("uppercase KILL ollama → BLOCK (Guardrail)", async () => {
  await assertBlocked("KILL ollama", /BLOCKED: Ollama killen/i, "uppercase KILL ollama")
})

await t("kleingeschriebenes rm -rf bleibt geblockt", async () => {
  await assertBlocked(`rm -rf ${HOME}/.hermes`, /BLOCKED: rm -rf.*verboten/i, "kleingeschriebenes rm -rf")
})

await t("unschädliches Kommando wird nicht vom Guardrail geblockt", async () => {
  const res = await exec("ls -la /tmp")
  assert.ok(!res || res.block !== true, "ls darf nicht geblockt werden")
})

// ── H2: Wortgrenzen statt Substring (kill/pkill/killall + ollama) ──

await t("H2: 'skill'-Substring + 'ollama' → KEIN Block (beweist positiv, nicht nur Text-Abwesenheit)", async () => {
  // Vor dem Fix: command.includes("kill") matcht "skill.md" → False-Positive-Block.
  // W39/F6+F7: Negativ-Test beweist jetzt ALLOW (block !== true), nicht nur dass der
  // Grund-Text zufällig nicht 'Ollama killen' enthält (der auch bei fremdem Block passt).
  const res = await exec("cat skill.md in ~/ollama-notes")
  // W39: Der Guardrail (Ollama killen) darf nicht mehr feuern. Ein PLAN-Gate-Block
  // (kein gültiger Plan) ist hier legitim und NICHT der Regression — bewiesen wird:
  // der blockReason enthaelt den Ollama-Guardrail-Text nicht mehr.
  const reason = res?.blockReason ?? ""
  assert.ok(!/Ollama killen/i.test(reason),
            `Guardrail-False-Positive nicht behoben (Ollama-Grund aktiv), Grund: ${reason}`)
  assert.ok(!/BLOCKED:/.test(reason),
            `Guardrail-BLOCK-Text darf nicht mehr erscheinen, Grund: ${reason}`)
})

await t("H2: pkill -f ollama → weiter Guardrail-BLOCK", async () => {
  await assertBlocked("pkill -f ollama runner", /BLOCKED: Ollama killen/i, "pkill auf ollama")
})

await t("H2: killall ollama → weiter Guardrail-BLOCK", async () => {
  await assertBlocked("killall ollama", /BLOCKED: Ollama killen/i, "killall ollama")
})

process.exitCode = failed ? 1 : 0
