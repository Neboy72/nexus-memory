/**
 * Regressionstest: Guardrail-Override mit Verifikation (H140, Welle 13)
 *
 * Vorher nahm das Override-Tool command + reasoning(>=10) + optional
 * matched_rules + agent_id(default "unknown") ungeprüft entgegen: es wurde nie
 * belegt, dass überhaupt ein realer Block existierte, matched_rules landete
 * verbatim im Audit-Record und "unknown" blieb als Akteur erlaubt. Jetzt wird
 * der Command gegen dieselben Protection-Rules nachgeprüft und nur ein weiter
 * blockender Command mit passenden Regeln + echter agent_id aufgezeichnet.
 */
import assert from "node:assert"
import { register } from "node:module"

// Deterministische ~-Expansion.
process.env.HOME = "/home/tester"

register("./_typebox-test-loader.mjs", import.meta.url)
const { registerGuardrailOverrideTool } = await import("./tools/guardrail_check.ts")

let failed = 0
const t = (name, fn) =>
  Promise.resolve()
    .then(fn)
    .then(() => console.log("PASS ", name))
    .catch((e) => {
      failed++
      console.log("FAIL ", name, "—", e.message)
    })

const PROTECTED = "/home/tester/.hermes"
const GOOD_RULE = {
  protected_path: PROTECTED,
  rule_text: `niemals löschen: ${PROTECTED}`,
  source_memory_id: "rule-0",
}
const REASONING = "Authorized by the operator for the migration window" // >= 30 chars

function makeTool(ruleTexts = [`niemals löschen: ${PROTECTED}`]) {
  let tool
  const captured = []
  const api = { registerTool: (tt) => { tool = tt } }
  const rules = ruleTexts.map((content, i) => ({
    id: `rule-${i}`,
    payload: { content, category: "rule" },
  }))
  registerGuardrailOverrideTool(
    api,
    {
      scrollFiltered: async () => rules,
      upsert: async (_id, _vec, payload) => { captured.push(payload) },
    },
    { collection: "nexus" },
    { embed: async () => [0.1, 0.2] },
  )
  return { tool, captured }
}

const parse = (res) => JSON.parse(res.content[0].text)

await t("reasoning < 30 Zeichen → reject", async () => {
  const { tool, captured } = makeTool()
  const res = await tool.execute("id", {
    command: `rm -rf ${PROTECTED}`,
    reasoning: "kurz",
    matched_rules: [GOOD_RULE],
    agent_id: "agent-7",
  })
  assert.strictEqual(res.isError, true)
  assert.match(parse(res).error, /min 30/i)
  assert.strictEqual(captured.length, 0)
})

await t("fehlende matched_rules → reject", async () => {
  const { tool, captured } = makeTool()
  const res = await tool.execute("id", {
    command: `rm -rf ${PROTECTED}`,
    reasoning: REASONING,
    agent_id: "agent-7",
  })
  assert.strictEqual(res.isError, true)
  assert.match(parse(res).error, /matched_rules/)
  assert.strictEqual(captured.length, 0)
})

await t("leeres matched_rules → reject", async () => {
  const { tool, captured } = makeTool()
  const res = await tool.execute("id", {
    command: `rm -rf ${PROTECTED}`,
    reasoning: REASONING,
    matched_rules: [],
    agent_id: "agent-7",
  })
  assert.strictEqual(res.isError, true)
  assert.match(parse(res).error, /matched_rules/)
  assert.strictEqual(captured.length, 0)
})

await t("matched_rules mit falscher Shape → reject", async () => {
  const { tool } = makeTool()
  const res = await tool.execute("id", {
    command: `rm -rf ${PROTECTED}`,
    reasoning: REASONING,
    matched_rules: [{ protected_path: PROTECTED }],
    agent_id: "agent-7",
  })
  assert.strictEqual(res.isError, true)
  assert.match(parse(res).error, /protected_path, rule_text and source_memory_id/)
})

await t('agent_id "unknown" → reject', async () => {
  const { tool, captured } = makeTool()
  const res = await tool.execute("id", {
    command: `rm -rf ${PROTECTED}`,
    reasoning: REASONING,
    matched_rules: [GOOD_RULE],
    agent_id: "unknown",
  })
  assert.strictEqual(res.isError, true)
  assert.match(parse(res).error, /agent_id/)
  assert.strictEqual(captured.length, 0)
})

await t("agent_id leer → reject", async () => {
  const { tool } = makeTool()
  const res = await tool.execute("id", {
    command: `rm -rf ${PROTECTED}`,
    reasoning: REASONING,
    matched_rules: [GOOD_RULE],
    agent_id: "   ",
  })
  assert.strictEqual(res.isError, true)
  assert.match(parse(res).error, /agent_id/)
})

await t("Command, der nicht (mehr) blockt → reject mit Re-check-Begründung", async () => {
  const { tool, captured } = makeTool()
  const res = await tool.execute("id", {
    command: "rm -rf /home/tester/safe-dir",
    reasoning: REASONING,
    matched_rules: [GOOD_RULE],
    agent_id: "agent-7",
  })
  assert.strictEqual(res.isError, true)
  const err = parse(res).error
  assert.match(err, /does not match a current guardrail block/)
  assert.match(err, /re-check produced allow/)
  assert.strictEqual(captured.length, 0, "kein Audit-Record ohne Verifikation")
})

await t("Re-check blockt, aber mit anderer Regel → reject", async () => {
  // protected /home/tester blockt den Command (Kind-Pfad), die citierte Regel
  // (protected .hermes) ist aber NICHT die, die der Re-check findet.
  const { tool, captured } = makeTool([`niemals löschen: /home/tester`])
  const res = await tool.execute("id", {
    command: `rm -rf ${PROTECTED}`,
    reasoning: REASONING,
    matched_rules: [GOOD_RULE],
    agent_id: "agent-7",
  })
  assert.strictEqual(res.isError, true)
  assert.match(parse(res).error, /different rules/)
  assert.strictEqual(captured.length, 0)
})

await t("echter Block-Fall → override_recorded mit verifizierten Regeln", async () => {
  const { tool, captured } = makeTool()
  const res = await tool.execute("id", {
    command: `rm -rf ${PROTECTED}`,
    reasoning: REASONING,
    matched_rules: [GOOD_RULE],
    agent_id: "agent-7",
  })
  const out = parse(res)
  assert.strictEqual(out.status, "override_recorded", JSON.stringify(out))
  assert.ok(out.override_id, "override_id erwartet")

  assert.strictEqual(captured.length, 1)
  const record = captured[0]
  assert.strictEqual(record.guardrail_override, true)
  assert.strictEqual(record.agent_id, "agent-7")
  assert.strictEqual(record.overridden_command, `rm -rf ${PROTECTED}`)
  assert.strictEqual(record.matched_rules.length, 1, "nur verifizierte Regeln")
  assert.strictEqual(record.matched_rules[0].protected_path, PROTECTED)
  assert.strictEqual(record.matched_rules[0].source_memory_id, "rule-0")
  assert.strictEqual(record.recheck_verdict, "block")
  assert.ok(record.verified_at, "verified_at erwartet")
})

process.exit(failed ? 1 : 0)
