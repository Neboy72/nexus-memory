/**
 * Regressionstest: Schutzregeln greifen auch bei DATEI-Pfaden ohne Schrägstrich.
 *
 * Anlass (ClawHub-Audit 20.11.2026, MCP07): Write/Edit/MultiEdit/NotebookEdit
 * nennen ihr Ziel in file_path/notebook_path. Dieser Pfad lief durch
 * extractTargets(), und das erkennt nur Pfade mit `/`, `~` oder führendem `.`.
 * Ein blanker relativer Name wie `notes.md` erzeugte deshalb KEIN Ziel, die
 * Prüfung wurde übersprungen und der Aufruf endete als "Non-destructive
 * action" — eine geschützte Datei war so überschreibbar.
 *
 * Geprüft wird am echten Tool (registerGuardrailCheckTool) mit gestubbtem
 * Rule-Store: block bei geschütztem Ziel, indeterminate ohne vertrauenswürdiges
 * cwd, allow bei ungeschütztem Ziel.
 */
import assert from "node:assert"
import { register } from "node:module"

const REAL_HOME = process.env.HOME
process.env.HOME = "/home/tester"  // # guard-ok: Testdaten, HOME-Stub fuer deterministische Pfade

register("./_typebox-test-loader.mjs", import.meta.url)
const { registerGuardrailCheckTool } = await import("./tools/guardrail_check.ts")
const { setTurnContext, bindToolSession } = await import("./lib/turn-context.ts")

const SESSION = "guardrail-direct-path"
setTurnContext(false, SESSION, Date.now(), false, false, true)
bindToolSession("check-1", SESSION)

let failed = 0
const t = async (name, fn) => {
  try {
    await Promise.race([
      Promise.resolve().then(fn),
      new Promise((_, rej) => setTimeout(() => rej(new Error("timeout: test hing 10s")), 10000)),
    ])
    console.log("PASS ", name)
  } catch (e) {
    failed++
    console.log("FAIL ", name, "—", (e && e.stack) || String(e))
  }
}

function makeTool(ruleTexts) {
  let tool
  const api = {
    registerTool: (tt) => {
      if (!tt || typeof tt.execute !== "function") {
        throw new Error("registerTool erhielt kein ausfuehrbares Tool")
      }
      tool = tt
    },
  }
  const rules = ruleTexts.map((content, i) => ({
    id: `rule-${i}`,
    payload: { content, category: "rule" },
  }))
  registerGuardrailCheckTool(
    api,
    { scrollFilteredStrict: async () => ({ points: rules, complete: true }) },
    { collection: "nexus", accessLevel: "private" },
  )
  if (!tool) throw new Error("registerGuardrailCheckTool hat KEIN Tool registriert")
  return tool
}

const run = async (tool, toolName, toolInput, cwd) => {
  const params = { command: "", tool_name: toolName, tool_input: toolInput }
  if (cwd !== undefined) params.cwd = cwd
  const res = await tool.execute("check-1", params)
  const block = res?.content?.[0]
  if (!block || typeof block.text !== "string") {
    throw new Error(`unerwartetes Tool-Output-Shape: ${JSON.stringify(res).slice(0, 200)}`)
  }
  const out = JSON.parse(block.text)
  if (typeof out.verdict !== "string") {
    throw new Error(`verdict fehlt im Output: ${block.text.slice(0, 200)}`)
  }
  return out
}

const CWD = "/tmp/nexus-direct-path"
const PROTECTED = `${CWD}/notes.md`

await t("Write mit blankem Dateinamen wird gegen das cwd aufgeloest und GEBLOCKT", async () => {
  const tool = makeTool([`niemals ueberschreiben: ${PROTECTED}`])
  const out = await run(tool, "Write", { file_path: "notes.md" }, CWD)
  assert.strictEqual(out.verdict, "block", `erwartet block, bekam ${JSON.stringify(out)}`)
  assert.strictEqual((out.matched_rules ?? []).length, 1)
  assert.strictEqual(out.matched_rules[0].protected_path, PROTECTED)
})

await t("Edit mit blankem Dateinamen ohne absolutes cwd → indeterminate, nie allow", async () => {
  const tool = makeTool([`niemals ueberschreiben: ${PROTECTED}`])
  const out = await run(tool, "Edit", { file_path: "notes.md" }, "kein/absoluter/pfad")
  assert.strictEqual(out.verdict, "indeterminate", `unaufloesbar darf nie allow sein: ${JSON.stringify(out)}`)
  assert.match(out.reason ?? "", /trusted working directory/)
})

await t("verschachtelter relativer Pfad (./unter/ordner/x.md) wird geblockt", async () => {
  const nested = `${CWD}/unter/ordner/x.md`
  const tool = makeTool([`niemals ueberschreiben: ${nested}`])
  const out = await run(tool, "Write", { file_path: "./unter/ordner/x.md" }, CWD)
  assert.strictEqual(out.verdict, "block", JSON.stringify(out))
})

await t("NotebookEdit nutzt notebook_path genauso", async () => {
  const nb = `${CWD}/analysis.ipynb`
  const tool = makeTool([`niemals ueberschreiben: ${nb}`])
  const out = await run(tool, "NotebookEdit", { notebook_path: "analysis.ipynb" }, CWD)
  assert.strictEqual(out.verdict, "block", JSON.stringify(out))
})

await t("ungeschuetztes relatives Ziel bleibt erlaubt (kein Blind-Block)", async () => {
  const tool = makeTool([`niemals ueberschreiben: ${PROTECTED}`])
  const out = await run(tool, "Write", { file_path: "other.md" }, CWD)
  assert.strictEqual(out.verdict, "allow", JSON.stringify(out))
  // Kein Blind-Block: der Aufruf bleibt erlaubt, weil kein Ziel geschuetzt ist.
  assert.ok(!/protected target$/.test(out.reason ?? ""), `kein Block erwartet: ${out.reason}`)
})

await t("absoluter geschuetzter Pfad blockt weiterhin (Regression)", async () => {
  const tool = makeTool([`niemals ueberschreiben: ${PROTECTED}`])
  const out = await run(tool, "Write", { file_path: PROTECTED })
  assert.strictEqual(out.verdict, "block", JSON.stringify(out))
})

await t("Schraegstrich-Form mit Unterpfad blockt weiterhin (Regression)", async () => {
  const tool = makeTool(["niemals loeschen: /tmp/nexus-direct-path/sub/notes.md"])
  const out = await run(tool, "Write", { file_path: "sub/notes.md" }, CWD)
  assert.strictEqual(out.verdict, "block", JSON.stringify(out))
})

await t("leerer Dateipfad und leerer Befehl bleiben 'allow' (unveraendert)", async () => {
  const tool = makeTool([`niemals ueberschreiben: ${PROTECTED}`])
  const out = await run(tool, "Write", { file_path: "   " })
  assert.strictEqual(out.verdict, "allow", JSON.stringify(out))
  assert.match(out.reason ?? "", /Empty command/)
})

process.env.HOME = REAL_HOME
if (failed > 0) {
  console.log(`\n${failed} Test(s) fehlgeschlagen`)
  process.exit(1)
}
console.log("\nalle Tests gruen")
