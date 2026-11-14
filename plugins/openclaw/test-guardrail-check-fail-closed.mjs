/**
 * Regressionstest: guardrail_check fail-closed (A2)
 *
 * loadProtectionRules() used to return [] on ANY error, which the caller
 * read as "no protection rules" → verdict "allow" (fail-open). After the
 * fix an unavailable rule store must BLOCK destructive actions; only a
 * confirmed empty rule list may still allow.
 */
import assert from "node:assert"
import { register } from "node:module"

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

function makeTool(qdrantClient) {
  let tool
  let registeredCalled = false
  const api = { registerTool: (tt) => { tool = tt; registeredCalled = true } }
  registerGuardrailCheckTool(api, qdrantClient, { collection: "nexus" })
  if (!registeredCalled || !tool) throw new Error("registerGuardrailCheckTool hat registerTool nicht gerufen — Registration-Contract gebrochen")
  return tool
}

const parse = (res) => JSON.parse(res.content[0].text)

await t("Rule-Store nicht erreichbar → BLOCK (fail-closed)", async () => {
  const tool = makeTool({ scrollFilteredStrict: async () => { throw new Error("qdrant down") } })
  const res = await tool.execute("id", { command: "rm -rf ~/some-dir" })
  const out = parse(res)
  assert.strictEqual(out.verdict, "block", `erwartet block, bekam ${out.verdict}`)
  assert.ok(typeof out.reason === "string" && /fail-closed/i.test(out.reason), `Grund sollte fail-closed nennen: ${JSON.stringify(out)}`)
})

await t("bestätigt leere Rule-Liste → allow", async () => {
  const tool = makeTool({ scrollFilteredStrict: async () => ({ points: [], complete: true }) })
  const res = await tool.execute("id", { command: "rm -rf ~/some-dir" })
  const out = parse(res)
  assert.strictEqual(out.verdict, "allow", `erwartet allow, bekam ${out.verdict}`)
})

await t("nicht-destruktives Kommando bleibt allow trotz Store-Ausfall (reason gepinnt)", async () => {
  const tool = makeTool({ scrollFilteredStrict: async () => { throw new Error("qdrant down") } })
  const res = await tool.execute("id", { command: "ls -la /tmp" })
  const out = parse(res)
  assert.strictEqual(out.verdict, "allow", `erwartet allow, bekam ${out.verdict}`)
  assert.ok(typeof out.reason === "string" && !/fail-closed/i.test(out.reason),
            `nicht-destruktiv darf nicht in den fail-closed-Pfad laufen: ${out.reason}`)
})

// F4 (W39): Target-lose destructive Commands short-circuiten VOR der Rule-Load
// (dokumentierter 'no protected target'-Zweig) — gepinnt statt trivially-passend.
await t("destruktiv ohne Target short-circuitet vor der Rule-Load (bewusst-so, gepinnt)", async () => {
  let storeHit = false
  const tool = makeTool({ scrollFilteredStrict: async () => { storeHit = true; return { points: [], complete: true } } })
  const out = parse(await tool.execute("id", { command: "kill -9 meinprozess" }))
  assert.strictEqual(storeHit, false, "ohne Target darf die Rule-Load nicht laufen")
  assert.strictEqual(out.verdict, "allow", `target-los ist allow: ${JSON.stringify(out)}`)
  assert.ok(/no protected target/i.test(out.reason ?? ""), `Zweig-Text gepinnt: ${out.reason}`)
})

// F9 (medium): allow-Negativpfad auch mit reason pinnen (exakt der Zweig, nicht Short-circuit)
await t("bestätigt leere Rule-Liste → allow mit 'no protected'-Begründung (Zweig bewiesen)", async () => {
  const tool = makeTool({ scrollFilteredStrict: async () => ({ points: [], complete: true }) })
  const res = await tool.execute("id", { command: "rm -rf ~/some-dir" })
  const out = parse(res)
  assert.strictEqual(out.verdict, "allow", `erwartet allow, bekam ${out.verdict}`)
  assert.ok(/no protection rules/i.test(out.reason ?? ""),
            `leere Liste muss den 'no protection rules'-Zweig nehmen: ${out.reason}`)
})

// ── Befund 2 (ClawHub 14.11.2026): unvollständige Regeln sperren ────────────

await t("abgeschnittener Regel-Lauf (complete:false) → BLOCK (fail-closed)", async () => {
  const tool = makeTool({
    scrollFilteredStrict: async () => ({ points: [], complete: false }),
  })
  const out = parse(await tool.execute("id", { command: "rm -rf ~/some-dir" }))
  assert.strictEqual(out.verdict, "block", `Teilmenge darf nie allow sein: ${JSON.stringify(out)}`)
  assert.match(out.reason ?? "", /fail-closed/i)
})

await t("Regel-Laden geht über scrollFilteredStrict, nicht über das Best-Effort-Scroll", async () => {
  let strictCalls = 0
  let bestEffortCalls = 0
  const tool = makeTool({
    scrollFilteredStrict: async () => { strictCalls++; return { points: [], complete: true } },
    scrollFiltered: async () => { bestEffortCalls++; return [] },
  })
  await tool.execute("id", { command: "rm -rf ~/some-dir" })
  assert.strictEqual(strictCalls, 1, "die Regeln müssen strikt geladen werden")
  assert.strictEqual(bestEffortCalls, 0, "der Best-Effort-Pfad darf die Regeln nicht mehr tragen")
})

process.exitCode = failed ? 1 : 0
