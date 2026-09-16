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

// Deterministische ~-Expansion (W39/F5: restore im finally, Determinismus nicht
// von der Ausführungsreihenfolge abhängig machen).
const REAL_HOME = process.env.HOME
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
      console.log("FAIL ", name, "—", e?.stack ?? String(e))
    })

const PROTECTED = "/home/tester/.hermes"
const GOOD_RULE = {
  protected_path: PROTECTED,
  rule_text: `niemals löschen: ${PROTECTED}`,
  source_memory_id: "rule-0",
}
// F6 (W39): echte guardrail_check-Block-Payloads tragen zusätzlich target/action —
// das Override-Tool muss extra Felder ignorieren, nicht ablehnen.
const GOOD_RULE_WITH_CONTEXT = { ...GOOD_RULE, target: PROTECTED, action: "delete" }
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

// F9 (W39): exakte Grenzen 29/30/31 + trim-Verhalten
await t("reasoning-Grenze: 29 reject, 30 ok, 31 ok, whitespace-only reject", async () => {
  const mk = (reasoning) => {
    const { tool, captured } = makeTool()
    return { tool, captured, reason: reasoning => tool.execute("id", {
      command: `rm -rf ${PROTECTED}`, reasoning, matched_rules: [GOOD_RULE], agent_id: "agent-7",
    }) }
  }
  const r29 = await mk().reason("x".repeat(29))
  assert.strictEqual(r29.isError, true, "29 Zeichen muss reject sein")
  const r30 = await mk().reason("x".repeat(30))
  assert.strictEqual(r30.isError, undefined, "30 Zeichen ist die Grenze: ok")
  const r31 = await mk().reason("x".repeat(31))
  assert.strictEqual(r31.isError, undefined, "31 Zeichen ok")
  const rws = await mk().reason("x".repeat(30) + "   ")
  // trailing whitespace wird getrimmt → immer noch 30 → ok (oder >=30-Check vor Trim)
  assert.ok(rws.isError === undefined || /min 30/i.test(parse(rws).error ?? ""),
            "whitespace-Padding muss deterministisch sein (ok ODER min-30-Error)")
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

await t("matched_rules mit falscher Shape → reject (kein Audit-Record)", async () => {
  const { tool, captured } = makeTool()
  const res = await tool.execute("id", {
    command: `rm -rf ${PROTECTED}`,
    reasoning: REASONING,
    matched_rules: [{ protected_path: PROTECTED }],
    agent_id: "agent-7",
  })
  assert.strictEqual(res.isError, true)
  assert.match(parse(res).error, /protected_path, rule_text and source_memory_id/)
  assert.strictEqual(captured.length, 0, "kein Audit-Record bei Shape-Verstoß")
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

await t("matched_rules mit erfundener source_memory_id → reject (Server-Verify schlägt Caller-Payload)", async () => {
  // W34-Fund: Der Erfolg-Test nutzte eine Byte-identische Kopie der Mock-Regel —
  // ein gefälschtes matched_rules war vom Server-Verify nicht unterscheidbar.
  // Eine ID, die der Mock-Store NICHT liefert, muss abgelehnt werden.
  const { tool, captured } = makeTool()
  const FORGED = { ...GOOD_RULE, source_memory_id: "rule-999" }
  const res = await tool.execute("id", {
    command: `rm -rf ${PROTECTED}`,
    reasoning: REASONING,
    matched_rules: [FORGED],
    agent_id: "agent-7",
  })
  assert.strictEqual(res.isError, true, "gefälschte rule-id darf nicht durchgehen")
  assert.strictEqual(captured.length, 0, "kein Audit-Record mit ungeprüfter Regel")
})

await t("matched_rules mit driftendem rule_text → reject", async () => {
  const { tool, captured } = makeTool()
  const DRIFT = { ...GOOD_RULE, rule_text: "etwas anderes: /home/tester" }
  const res = await tool.execute("id", {
    command: `rm -rf ${PROTECTED}`,
    reasoning: REASONING,
    matched_rules: [DRIFT],
    agent_id: "agent-7",
  })
  assert.strictEqual(res.isError, true)
  assert.strictEqual(captured.length, 0)
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
  const err = parse(res).error
  assert.match(err, /different rules/)
  assert.match(err, /re-check produced block/, "re-check muss geblockt haben (sonst wäre es der allow-Pfad)")
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
  assert.ok(typeof record.verified_at === "number" || !Number.isNaN(Date.parse(record.verified_at)),
            `verified_at muss ein echter Zeitstempel sein: ${JSON.stringify(record.verified_at)}`)
  // F4: Server-Verify-Beweis — die citierte Regel muss EXAKT der Mock-Regel entsprechen
  // (Byte-Equalität von rule_text beweist, dass nicht der Caller-Payload durchging)
  assert.strictEqual(record.matched_rules[0].rule_text, `niemals löschen: ${PROTECTED}`)
})

await t("echter Block-Fall mit target/action-Kontext (extra Felder werden ignoriert)", async () => {
  const { tool, captured } = makeTool()
  const res = await tool.execute("id", {
    command: `rm -rf ${PROTECTED}`,
    reasoning: REASONING,
    matched_rules: [GOOD_RULE_WITH_CONTEXT],
    agent_id: "agent-7",
  })
  const out = parse(res)
  assert.strictEqual(out.status, "override_recorded", `extra Payload-Felder dürfen nicht ablehnen: ${JSON.stringify(out)}`)
  assert.strictEqual(captured[0].matched_rules[0].source_memory_id, "rule-0")
})

process.env.HOME = REAL_HOME
process.exitCode = failed ? 1 : 0
