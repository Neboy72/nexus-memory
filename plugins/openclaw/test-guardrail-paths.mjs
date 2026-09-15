/**
 * Regressionstest: Guardrail-Pfad-Extraktion + Matching (H124, Welle 13)
 *
 * Vorher: extractTargets() verwarf per `length > 2`-Guard genau die
 * katastrophalen Ziele (`rm -rf /`, `rm -rf ~`, `rm -rf .`); normalizePath()
 * lowercaste (falsch auf case-sensitiven Dateisystemen) und löste `.`/`..`
 * nicht auf; pathMatches() erkannte Parent-Deletion nicht (`rm -rf ~/proj`
 * gegen protected `~/proj/secret` → erlaubt).
 *
 * Getestet wird am echten Tool (registerGuardrailCheckTool) mit gestubbtem
 * Rule-Store — also inkl. Extraktion, Matching und Verdict.
 */
import assert from "node:assert"
import { register } from "node:module"

// Deterministische ~-Expansion, unabhängig von der Maschine.
process.env.HOME = "/home/tester"

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

const HOME = "/home/tester"

/** Tool mit gestubbtem Rule-Store: jede Regel ist ein `content`-String. */
function makeTool(ruleTexts) {
  let tool
  const api = { registerTool: (tt) => { tool = tt } }
  const rules = ruleTexts.map((content, i) => ({
    id: `rule-${i}`,
    payload: { content, category: "rule" },
  }))
  registerGuardrailCheckTool(
    api,
    { scrollFiltered: async () => rules },
    { collection: "nexus" },
  )
  return tool
}

const run = async (tool, command) =>
  JSON.parse((await tool.execute("id", { command })).content[0].text)

// ── (1) Klassiker / ~ / . erzeugen überhaupt Targets ────────────────────────

await t("`rm -rf /` liefert ein Target und blockt gegen eine geschützte Regel", async () => {
  const tool = makeTool([`niemals löschen: ${HOME}/.hermes`])
  const out = await run(tool, "rm -rf /")
  assert.strictEqual(out.verdict, "block", `erwartet block, bekam ${JSON.stringify(out)}`)
  assert.ok(out.matched_rules.length > 0, "gematchte Regel erwartet")
})

await t("`rm -rf ~` liefert ein Target und blockt gegen eine geschützte Regel", async () => {
  const tool = makeTool([`niemals löschen: ${HOME}/.hermes`])
  const out = await run(tool, "rm -rf ~")
  assert.strictEqual(out.verdict, "block", `erwartet block, bekam ${JSON.stringify(out)}`)
})

await t("`rm -rf .` liefert ein Target (kein Silent-Drop mehr)", async () => {
  // "." ist relativ — ohne cwd kein Match, aber der Guard darf es nicht mehr
  // verschlucken: die Extraktion muss ein Target sehen, also darf der
  // allow-Grund NICHT "no protected target" sein.
  const tool = makeTool([`niemals löschen: ${HOME}/.hermes`])
  const out = await run(tool, "rm -rf .")
  assert.ok(
    !/no protected target/.test(out.reason),
    `"." muss als Target extrahiert werden, bekam: ${out.reason}`,
  )
})

// ── (2) Parent-Deletion ─────────────────────────────────────────────────────

await t("Parent-Deletion: `rm -rf ~/proj` vs protected `~/proj/secret` → BLOCK", async () => {
  const tool = makeTool([`niemals löschen: ${HOME}/proj/secret`])
  const out = await run(tool, "rm -rf ~/proj")
  assert.strictEqual(out.verdict, "block", `erwartet block, bekam ${JSON.stringify(out)}`)
})

await t("Root-Deletion blockt jede geschützte Regel (protected liegt unter /)", async () => {
  const tool = makeTool([`niemals löschen: /etc/nginx`])
  const out = await run(tool, "rm -rf /")
  assert.strictEqual(out.verdict, "block", `erwartet block, bekam ${JSON.stringify(out)}`)
})

// ── (3) Case-Sensitivität ───────────────────────────────────────────────────

await t("case-sensitive: `rm -rf /Data` matcht protected `/data` NICHT", async () => {
  const tool = makeTool(["niemals löschen: /data"])
  const upper = await run(tool, "rm -rf /Data")
  assert.strictEqual(upper.verdict, "allow", `case darf nicht egal sein: ${JSON.stringify(upper)}`)
  const lower = await run(tool, "rm -rf /data")
  assert.strictEqual(lower.verdict, "block", "identische Schreibweise muss blocken")
})

// ── (4) . / .. Normalisierung ───────────────────────────────────────────────

await t("`.`/`..`-Segmente werden textuell aufgelöst", async () => {
  const tool = makeTool([`niemals löschen: ${HOME}/proj/secret`])
  const out = await run(tool, "rm -rf ~/proj/./sub/../secret")
  assert.strictEqual(out.verdict, "block", `Normalisierung fehlt: ${JSON.stringify(out)}`)
})

// ── (5) Keine False Positives an Segment-Grenzen ────────────────────────────

await t("kein False Positive: `~/projekt-alt` vs protected `~/proj`", async () => {
  const tool = makeTool([`niemals löschen: ${HOME}/proj`])
  const out = await run(tool, "rm -rf ~/projekt-alt")
  assert.strictEqual(out.verdict, "allow", `Segment-Grenze verletzt: ${JSON.stringify(out)}`)
})

await t("Sanity: exakter Treffer + Kind-Pfad blocken weiterhin", async () => {
  const tool = makeTool([`niemals löschen: ${HOME}/proj`])
  assert.strictEqual((await run(tool, "rm -rf ~/proj")).verdict, "block")
  assert.strictEqual((await run(tool, "rm -rf ~/proj/sub/file")).verdict, "block")
})

process.exit(failed ? 1 : 0)
