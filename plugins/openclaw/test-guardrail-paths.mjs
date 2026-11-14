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
// Fund W37 (low): Original-HOME sichern (Restore am Dateiende), kein Duplikat-Literal.
const REAL_HOME = process.env.HOME
process.env.HOME = "/home/tester"

register("./_typebox-test-loader.mjs", import.meta.url)
const { registerGuardrailCheckTool } = await import("./tools/guardrail_check.ts")
const { setTurnContext, bindToolSession } = await import("./lib/turn-context.ts")

// Deterministic viewer level: rule DETAILS are only disclosed at the rule's
// level (ClawHub audit 14.11.2026, MCP07). A direct chat at cfg.accessLevel
// "private" is the owner's own session — the case these tests are about.
const SESSION = "guardrail-paths"
setTurnContext(false, SESSION, Date.now(), false, false, true)
bindToolSession("id", SESSION)

let failed = 0
// Fund W37 (low): stack + timeout statt nur e.message.
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

const HOME = process.env.HOME // Fund W37 (low): single source statt zweitem Literal

/** Tool mit gestubbtem Rule-Store: jede Regel ist ein `content`-String. */
function makeTool(ruleTexts) {
  let tool
  // Fund W37 (medium): Register-Contract hart pruefen — wenn der Hook umzieht/
  // umbenannt wird, soll der Test FAILen, nicht still ein undefined-Tool liefern.
  const api = {
    registerTool: (tt) => {
      if (!tt || typeof tt.execute !== "function") {
        throw new Error("registerTool erhielt kein ausfuehrbares Tool: " + JSON.stringify(tt?.name ?? tt))
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

const run = async (tool, command, extra = {}) => {
  // Fund W37 (medium): Output-Shape validieren statt blind content[0].text zu parsen.
  const res = await tool.execute("id", { command, ...extra })
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

// ── (1) Klassiker / ~ / . erzeugen überhaupt Targets ────────────────────────

await t("`rm -rf /` liefert ein Target und blockt gegen eine geschützte Regel", async () => {
  const tool = makeTool([`niemals löschen: ${HOME}/.hermes`])
  const out = await run(tool, "rm -rf /")
  assert.strictEqual(out.verdict, "block", `erwartet block, bekam ${JSON.stringify(out)}`)
  // Fund W37 (low): shape-safe — block ohne matched_rules-Key darf nicht crashen.
  assert.ok((out.matched_rules ?? []).length > 0, "gematchte Regel erwartet")
})

await t("`rm -rf ~` liefert ein Target und blockt gegen eine geschützte Regel", async () => {
  const tool = makeTool([`niemals löschen: ${HOME}/.hermes`])
  const out = await run(tool, "rm -rf ~")
  assert.strictEqual(out.verdict, "block", `erwartet block, bekam ${JSON.stringify(out)}`)
})

// Befund 2 (ClawHub 14.11.2026, #5): relative Ziele werden gegen ein
// vertrauenswuerdiges Arbeitsverzeichnis aufgeloest; ohne eines wird
// "indeterminate" gemeldet statt allow.
const TEST_CWD = "/tmp/nexus-guardrail-paths"

await t("`rm -rf .` wird gegen das cwd aufgeloest (kein Silent-Drop mehr)", async () => {
  const tool = makeTool([`niemals löschen: ${HOME}/.hermes`])
  const out = await run(tool, "rm -rf .", { cwd: TEST_CWD })
  // POSITIV-Assert: "." ist ein echtes Target ("unprotected target", NICHT
  // "no protected target"); das aufgeloeste Ziel ist hier nicht geschuetzt.
  assert.ok(
    !/no protected target/.test(out.reason),
    `"." muss als Target erkannt sein (kein Silent-Drop), bekam: ${out.reason}`,
  )
  assert.strictEqual(out.verdict, "allow", `cwd ist nicht geschuetzt: ${JSON.stringify(out)}`)
  assert.ok(!String(out.reason).includes(TEST_CWD), "der Allow-Grund nennt keinen Pfad")
})

await t("relativer Zielpfad + relative Regel → aufgeloest und GEBLOCKT", async () => {
  const tool = makeTool(["niemals löschen: .hermes"])
  const out = await run(tool, "rm -rf .hermes", { cwd: TEST_CWD })
  assert.strictEqual(out.verdict, "block", `relative Regel muss greifen: ${JSON.stringify(out)}`)
  assert.strictEqual((out.matched_rules ?? []).length, 1)
  assert.strictEqual(out.matched_rules[0].protected_path, `${TEST_CWD}/.hermes`)
})

await t("relatives Ziel ohne vertrauenswuerdiges cwd → indeterminate (nie allow)", async () => {
  const tool = makeTool(["niemals löschen: .hermes"])
  const out = await run(tool, "rm -rf .hermes", { cwd: "kein/absoluter/pfad" })
  assert.strictEqual(out.verdict, "indeterminate", `unaufloesbar darf nie allow sein: ${JSON.stringify(out)}`)
  assert.match(out.reason ?? "", /trusted working directory/)
})

await t("Klassiker-Schutz: Regeln fuer `/`, `~`, `.` und `/a` greifen", async () => {
  const root = await run(makeTool(["verboten: /"]), "rm -rf /a", { cwd: TEST_CWD })
  assert.strictEqual(root.verdict, "block", `Regel fuer / muss /a sperren: ${JSON.stringify(root)}`)

  const home = await run(makeTool(["niemals löschen: ~"]), "rm -rf ~")
  assert.strictEqual(home.verdict, "block", `Regel fuer ~ muss ~ sperren: ${JSON.stringify(home)}`)

  const dot = await run(makeTool(["niemals löschen: ."]), "rm -rf .", { cwd: TEST_CWD })
  assert.strictEqual(dot.verdict, "block", `Regel fuer . muss das cwd sperren: ${JSON.stringify(dot)}`)

  const single = await run(makeTool(["niemals löschen: /a"]), "rm -rf /a")
  assert.strictEqual(single.verdict, "block", `Regel fuer /a muss /a sperren: ${JSON.stringify(single)}`)
})



// ── (2) Parent-Deletion ─────────────────────────────────────────────────────

await t("Parent-Deletion: `rm -rf ~/proj` vs protected `~/proj/secret` → BLOCK", async () => {
  const tool = makeTool([`niemals löschen: ${HOME}/proj/secret`])
  const out = await run(tool, "rm -rf ~/proj")
  assert.strictEqual(out.verdict, "block", `erwartet block, bekam ${JSON.stringify(out)}`)
  // Fund W37 (medium): matched_rules-Inhalt pruefen (override-tool braucht die Felder).
  const mr = out.matched_rules ?? []
  assert.strictEqual(mr.length, 1)
  assert.ok(mr[0].rule_text && typeof mr[0].rule_text === "string", "rule_text fehlt")
  assert.ok(mr[0].source_memory_id, "source_memory_id fehlt")
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

// Fund W37 (medium/low): exitCode statt process.exit + HOME-Restore.
if (REAL_HOME !== undefined) process.env.HOME = REAL_HOME
process.exitCode = failed ? 1 : 0
