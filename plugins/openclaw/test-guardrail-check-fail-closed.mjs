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
      console.log("FAIL ", name, "—", e.message)
    })

function makeTool(qdrantClient) {
  let tool
  const api = { registerTool: (t) => { tool = t } }
  registerGuardrailCheckTool(api, qdrantClient, { collection: "nexus" })
  return tool
}

const parse = (res) => JSON.parse(res.content[0].text)

await t("Rule-Store nicht erreichbar → BLOCK (fail-closed)", async () => {
  const tool = makeTool({ scrollFiltered: async () => { throw new Error("qdrant down") } })
  const res = await tool.execute("id", { command: "rm -rf ~/some-dir" })
  const out = parse(res)
  assert.strictEqual(out.verdict, "block", `erwartet block, bekam ${out.verdict}`)
  assert.match(out.reason, /fail-closed/i, `Grund sollte fail-closed nennen: ${out.reason}`)
})

await t("bestätigt leere Rule-Liste → allow", async () => {
  const tool = makeTool({ scrollFiltered: async () => [] })
  const res = await tool.execute("id", { command: "rm -rf ~/some-dir" })
  const out = parse(res)
  assert.strictEqual(out.verdict, "allow", `erwartet allow, bekam ${out.verdict}`)
})

await t("nicht-destruktives Kommando bleibt allow trotz Store-Ausfall", async () => {
  const tool = makeTool({ scrollFiltered: async () => { throw new Error("qdrant down") } })
  const res = await tool.execute("id", { command: "ls -la /tmp" })
  const out = parse(res)
  assert.strictEqual(out.verdict, "allow", `erwartet allow, bekam ${out.verdict}`)
})

process.exit(failed ? 1 : 0)
