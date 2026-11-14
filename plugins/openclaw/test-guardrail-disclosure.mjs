/**
 * Regressionstest: Guardrail-Antworten folgen der Sitzungsstufe (MCP07)
 *
 * ClawHub-Gutachten vom 14.11.2026, Befund 1: `nexus_guardrail_check` gab den
 * INHALT privater Schutzregeln (Pfade, Regeltext, Memory-ID) an jede Sitzung
 * heraus, weil die konfigurierte Stufe statt der wirksamen Stufe des Aufrufers
 * entschied. Jetzt gilt: durchgesetzt wird gegen ALLE Regeln (eine private
 * Regel blockt auch eine public-Sitzung), offengelegt wird nur, was die
 * wirksame Stufe (`toolCallAccessLevel`) sehen darf. Nicht Berechtigte
 * bekommen eine generische Sperre ohne Regelinhalt.
 */
import assert from "node:assert"
import { register } from "node:module"

import { bindToolSession, resetTurnContext, setTurnContext } from "./lib/turn-context.ts"

register("./_typebox-test-loader.mjs", import.meta.url)
const { registerGuardrailCheckTool } = await import("./tools/guardrail_check.ts")

let failed = 0
const t = (name, fn) =>
  Promise.resolve()
    .then(fn)
    .then(() => console.log("PASS ", name))
    .catch((e) => {
      failed++
      console.log("FAIL ", name, "—", e?.stack ?? String(e))
    })

const PRIVATE_PATH = "/srv/secret-db"
const TRUSTED_PATH = "/srv/team-share"
const PUBLIC_PATH = "/srv/public-www"
const PRIVATE_ID = "rule-private"
const RULES = [
  { id: PRIVATE_ID, payload: { content: `niemals löschen: ${PRIVATE_PATH}`, category: "rule", access_level: "private" } },
  { id: "rule-trusted", payload: { content: `niemals löschen: ${TRUSTED_PATH}`, category: "rule", access_level: "trusted" } },
  { id: "rule-public", payload: { content: `niemals löschen: ${PUBLIC_PATH}`, category: "rule", access_level: "public" } },
]

/**
 * Tool, dessen Aufruf "call-1" an eine Sitzung der gewuenschten Stufe gebunden
 * ist (Direktchat ohne Fremde → die konfigurierte Stufe gilt).
 */
function makeTool({ accessLevel = "private", session = "disclosure", markerAt = Date.now(), rules = RULES, bind = true } = {}) {
  resetTurnContext()
  if (bind) {
    setTurnContext(false, session, markerAt, false, false, true)
    bindToolSession("call-1", session)
  }
  let tool
  const api = { registerTool: (tt) => { tool = tt } }
  registerGuardrailCheckTool(
    api,
    { scrollFilteredStrict: async () => ({ points: rules, complete: true }) },
    { collection: "nexus", accessLevel },
  )
  if (!tool) throw new Error("registerGuardrailCheckTool hat KEIN Tool registriert")
  return tool
}

const ask = async (tool, command, callId = "call-1") => {
  const res = await tool.execute(callId, { command })
  const text = res?.content?.[0]?.text
  if (typeof text !== "string") throw new Error(`unerwartetes Tool-Output: ${JSON.stringify(res)}`)
  const out = JSON.parse(text)
  if (typeof out.verdict !== "string") throw new Error(`verdict fehlt: ${text}`)
  return { out, text }
}

// ── Sperre bleibt Sperre: die Stufe aendert den VERDICT nicht ───────────────

await t("public-Sitzung: private Regel blockt weiter, ohne Regelinhalt", async () => {
  const tool = makeTool({ accessLevel: "public" })
  const { out, text } = await ask(tool, `rm -rf ${PRIVATE_PATH}`)
  assert.strictEqual(out.verdict, "block", `Sperre darf nicht abgeschwaecht werden: ${text}`)
  assert.ok(!text.includes(PRIVATE_PATH), `protected_path/Literal darf nicht raus: ${text}`)
  assert.ok(!text.includes(PRIVATE_ID), `source_memory_id darf nicht raus: ${text}`)
  assert.ok(!text.includes("niemals"), `rule_text darf nicht raus: ${text}`)
  assert.strictEqual(out.withheld_rules, 1, "nur die ANZAHL der zurueckgehaltenen Regeln")
  assert.deepStrictEqual(out.matched_rules, [])
})

await t("trusted-Sitzung: private Regel bleibt verborgen (trusted < private)", async () => {
  const tool = makeTool({ accessLevel: "trusted" })
  const { out, text } = await ask(tool, `rm -rf ${PRIVATE_PATH}`)
  assert.strictEqual(out.verdict, "block")
  assert.ok(!text.includes(PRIVATE_PATH) && !text.includes(PRIVATE_ID), `Leak: ${text}`)
  assert.strictEqual(out.withheld_rules, 1)
})

await t("private-Sitzung: Regelinhalt wird offengelegt (Override braucht ihn)", async () => {
  const tool = makeTool({ accessLevel: "private" })
  const { out } = await ask(tool, `rm -rf ${PRIVATE_PATH}`)
  assert.strictEqual(out.verdict, "block")
  assert.strictEqual(out.matched_rules.length, 1)
  assert.strictEqual(out.matched_rules[0].protected_path, PRIVATE_PATH)
  assert.strictEqual(out.matched_rules[0].source_memory_id, PRIVATE_ID)
  assert.strictEqual(out.matched_rules[0].rule_text, `niemals löschen: ${PRIVATE_PATH}`)
  assert.strictEqual(out.withheld_rules, undefined, "nichts zurueckgehalten")
})

await t("Stufen-Hierarchie: trusted sieht trusted-Regeln", async () => {
  const only = RULES.filter((r) => r.payload.access_level === "trusted")
  const tool = makeTool({ accessLevel: "trusted", rules: only })
  const { out } = await ask(tool, `rm -rf ${TRUSTED_PATH}`)
  assert.strictEqual(out.verdict, "block")
  assert.strictEqual(out.matched_rules.length, 1, `trusted darf trusted sehen: ${JSON.stringify(out)}`)
})

// ── Ungebunden / abgelaufen: fail-closed public ─────────────────────────────

await t("ungebundener Aufruf (keine Sitzung) → public, kein Regelinhalt", async () => {
  const tool = makeTool({ accessLevel: "private", bind: false })
  const { out, text } = await ask(tool, `rm -rf ${PRIVATE_PATH}`)
  assert.strictEqual(out.verdict, "block")
  assert.ok(!text.includes(PRIVATE_PATH) && !text.includes(PRIVATE_ID), `Leak: ${text}`)
  assert.strictEqual(out.withheld_rules, 1)
})

await t("abgelaufene Sitzung (Marker aelter als TTL) → public, kein Regelinhalt", async () => {
  const tool = makeTool({ accessLevel: "private", markerAt: Date.now() - 200_000 })
  const { out, text } = await ask(tool, `rm -rf ${PRIVATE_PATH}`)
  assert.strictEqual(out.verdict, "block")
  assert.ok(!text.includes(PRIVATE_PATH) && !text.includes(PRIVATE_ID), `Leak: ${text}`)
  assert.strictEqual(out.withheld_rules, 1)
})

// ── Probe auf `/`: private Regeln bleiben verborgen, public darf erscheinen ──

await t("Probe auf `/`: public sieht nur die public-Regel, private bleiben verborgen", async () => {
  const tool = makeTool({ accessLevel: "public" })
  const { out, text } = await ask(tool, "rm -rf /")
  assert.strictEqual(out.verdict, "block", `Root-Wipe muss blocken: ${text}`)
  assert.strictEqual(out.withheld_rules, 2, "private + trusted zurueckgehalten")
  assert.strictEqual(out.matched_rules.length, 1, "nur die public-Regel ist sichtbar")
  assert.strictEqual(out.matched_rules[0].protected_path, PUBLIC_PATH)
  assert.ok(!text.includes(PRIVATE_PATH) && !text.includes(TRUSTED_PATH), `Leak: ${text}`)
  assert.ok(!text.includes(PRIVATE_ID), `Leak: ${text}`)
})

await t("Probe auf `/` bei private: alle drei Regeln sichtbar, nichts zurueckgehalten", async () => {
  const tool = makeTool({ accessLevel: "private" })
  const { out } = await ask(tool, "rm -rf /")
  assert.strictEqual(out.verdict, "block")
  assert.strictEqual(out.withheld_rules, undefined)
  assert.strictEqual(out.matched_rules.length, 3)
})

await t("die generische Sperre nennt keinen Pfad und keine Kennung", async () => {
  const tool = makeTool({ accessLevel: "public" })
  const { out } = await ask(tool, `rm -rf ${PRIVATE_PATH}`)
  assert.match(String(out.reason), /withheld/)
  assert.ok(!String(out.reason).includes("/"), `kein Pfad in der Begruendung: ${out.reason}`)
  assert.ok(!String(out.reason).includes(PRIVATE_ID))
})

resetTurnContext()
process.exitCode = failed ? 1 : 0
