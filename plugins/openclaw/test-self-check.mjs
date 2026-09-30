/**
 * Regressionstest: Self-Report / Self-Check (Portierung aus dem Hermes-Plugin,
 * v0.22.2 → OpenClaw). Das Plugin veröffentlicht beim Start eine Health-Datei
 * `<datenverzeichnis>/agent-selfcheck-<agent-id>.json`, die der server-seitige
 * Watchdog (src/nexus_memory/self_report.py) liest. Der Vertrag (Dateiname,
 * 7 exakte JSON-Keys, Sekunden-ts, atomarer Write, Fail-open) wird hier
 * gepinnt — ein externer Watchdog parst das Format.
 */
import assert from "node:assert/strict"
import fs from "node:fs"
import os from "node:os"
import path from "node:path"
import {
  buildSelfCheckWarning,
  resetSelfCheckForTest,
  setSelfCheckResult,
  writeSelfCheck,
} from "./lib/self-check.ts"
import { buildPromptSection } from "./runtime.ts"

let failed = 0
const t = (name, fn) =>
  Promise.resolve()
    .then(fn)
    .then(() => console.log("PASS  ", name))
    .catch((e) => {
      failed++
      console.log("FAIL  ", name, "—", e?.stack ?? String(e))
    })

// Exakt diese 7 Keys — der Watchdog liest nur sie.
const EXPECTED_KEYS = ["agent_id", "fix", "interpreter", "ok", "plugin_version", "reason", "ts"]

const prevDataDir = process.env.NEXUS_DATA_DIR
const prevAgentId = process.env.NEXUS_AGENT_ID

function freshDir() {
  return fs.mkdtempSync(path.join(os.tmpdir(), "nexus-selfcheck-"))
}

function setEnv(dir, agentId) {
  if (dir === undefined) delete process.env.NEXUS_DATA_DIR
  else process.env.NEXUS_DATA_DIR = dir
  if (agentId === undefined) delete process.env.NEXUS_AGENT_ID
  else process.env.NEXUS_AGENT_ID = agentId
}

/** Genau eine selfcheck-Datei im Verzeichnis — und ihr Pfad. */
function findReport(dir) {
  const hits = fs.readdirSync(dir).filter((f) => /^agent-selfcheck-.*\.json$/.test(f))
  assert.strictEqual(hits.length, 1, `genau eine selfcheck-Datei erwartet, waren: ${JSON.stringify(hits)}`)
  return path.join(dir, hits[0])
}

await t("gesunder Write → Datei existiert, parst, exakt 7 Keys, ok:true", () => {
  resetSelfCheckForTest()
  const dir = freshDir()
  setEnv(dir, "openclaw-test")
  writeSelfCheck(true, "", "fix-me")
  const parsed = JSON.parse(fs.readFileSync(findReport(dir), "utf8"))
  assert.deepStrictEqual(Object.keys(parsed).sort(), EXPECTED_KEYS, "exakt die 7 Watchdog-Keys")
  assert.strictEqual(parsed.ok, true)
  assert.strictEqual(parsed.reason, "")
  assert.strictEqual(parsed.fix, "fix-me")
  assert.strictEqual(parsed.agent_id, "openclaw-test")
  assert.strictEqual(parsed.interpreter, process.execPath)
  assert.ok(
    typeof parsed.plugin_version === "string" && parsed.plugin_version.length > 0,
    `plugin_version muss String sein, war ${JSON.stringify(parsed.plugin_version)}`,
  )
})

await t("ts hat Sekunden-Präzision (kein Millisekunden-Suffix)", () => {
  resetSelfCheckForTest()
  const dir = freshDir()
  setEnv(dir, "openclaw")
  writeSelfCheck(true, "", "f")
  const parsed = JSON.parse(fs.readFileSync(findReport(dir), "utf8"))
  assert.match(parsed.ts, /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/, `ts-Format falsch: ${parsed.ts}`)
})

await t("NEXUS_AGENT_ID='../../evil' → Datei bleibt im Datenverzeichnis", () => {
  resetSelfCheckForTest()
  const dir = freshDir()
  setEnv(dir, "../../evil")
  writeSelfCheck(true, "", "f")
  const p = findReport(dir)
  // Der Bericht liegt DIREKT im Datenverzeichnis (kein Pfad-Ausbruch).
  assert.strictEqual(path.dirname(p), dir, `Bericht entkam: ${p}`)
  assert.ok(path.resolve(p).startsWith(path.resolve(dir) + path.sep), `Pfad außerhalb: ${p}`)
  assert.ok(!path.basename(p).includes("/"), `Dateiname enthält Separator: ${p}`)
  // Der unsanitisierte Pfad darf nirgends entstanden sein.
  assert.ok(!fs.existsSync(path.resolve(dir, "..", "evil.json")), "roher Agent-Id-Pfad entstand")
})

await t("NEXUS_DATA_DIR-Umleitung wird beachtet", () => {
  resetSelfCheckForTest()
  const dir = freshDir()
  setEnv(dir, "redirected")
  writeSelfCheck(true, "", "f")
  const parsed = JSON.parse(fs.readFileSync(findReport(dir), "utf8"))
  assert.strictEqual(parsed.agent_id, "redirected")
  assert.strictEqual(parsed.ok, true)
})

await t("ungültiges/unbeschreibbares Datenverzeichnis → wirft NICHT (fail-open)", () => {
  resetSelfCheckForTest()
  const base = freshDir()
  const asFile = path.join(base, "not-a-dir")
  fs.writeFileSync(asFile, "x")
  // Kind eines regulären Files ist kein Verzeichnis → mkdir schlägt fehl.
  setEnv(path.join(asFile, "sub"), "openclaw")
  assert.doesNotThrow(() => writeSelfCheck(false, "boom", "fix"), "writeSelfCheck darf nie werfen")
  // Trotz Write-Fehler muss das Urteil im Prompt ankommen.
  const warn = buildSelfCheckWarning()
  assert.ok(warn.length > 0, "Warnung muss auch bei Write-Fehler gesetzt sein")
  assert.ok(warn.some((l) => l.includes("boom")), "Cause muss in der Warnung stehen")
})

await t("keine *.tmp-* Reste nach erfolgreichem Write", () => {
  resetSelfCheckForTest()
  const dir = freshDir()
  setEnv(dir, "openclaw")
  writeSelfCheck(true, "", "f")
  const leftovers = fs.readdirSync(dir).filter((f) => f.includes(".tmp-"))
  assert.deepStrictEqual(leftovers, [], "der pid-suffigierte temp-File muss weggeräumt sein")
})

await t("buildSelfCheckWarning: [] bei ok, Warnung mit Cause+Fix bei broken", () => {
  resetSelfCheckForTest()
  const dir = freshDir()
  setEnv(dir, "openclaw")
  writeSelfCheck(true, "", "f")
  assert.deepStrictEqual(buildSelfCheckWarning(), [], "gesund → keine Warnzeilen")
  setSelfCheckResult({ ok: false, reason: "qdrant down", fix: "run the fix" })
  const warn = buildSelfCheckWarning()
  assert.ok(warn.length > 0, "broken → Warnzeilen")
  assert.ok(warn[0].includes("NOT WORKING"), `erste Zeile ist der Header: ${warn[0]}`)
  assert.ok(warn.some((l) => l.includes("qdrant down")), "Cause fehlt")
  assert.ok(warn.some((l) => l.includes("run the fix")), "Fix fehlt")
})

await t("Warnung bleibt bis zu einem gesunden Write (nicht einmal-pro-Prozess)", () => {
  resetSelfCheckForTest()
  const dir = freshDir()
  setEnv(dir, "openclaw")
  writeSelfCheck(false, "r", "f")
  assert.ok(buildSelfCheckWarning().length > 0, "erster Build warnt")
  assert.ok(buildSelfCheckWarning().length > 0, "zweiter Build warnt weiterhin")
  writeSelfCheck(true, "", "f")
  assert.deepStrictEqual(buildSelfCheckWarning(), [], "gesunder Write räumt die Warnung ab")
})

await t("runtime buildPromptSection hängt die Warnung an (auch ohne Memory-Tools)", () => {
  resetSelfCheckForTest()
  const healthy = buildPromptSection({ availableTools: new Set(["nexus_search"]) })
  assert.ok(!healthy.some((l) => l.includes("NOT WORKING")), "gesund → keine Warnung im Prompt")
  setSelfCheckResult({ ok: false, reason: "r", fix: "f" })
  const broken = buildPromptSection({ availableTools: new Set(["nexus_search"]) })
  assert.ok(broken.some((l) => l.includes("NOT WORKING")), "broken → Warnung im Prompt")
  const noTools = buildPromptSection({ availableTools: new Set() })
  assert.ok(noTools.some((l) => l.includes("NOT WORKING")), "Warnung darf nicht am Tool-Check verloren gehen")
})

// Env-Restore + Zustand zurücksetzen für nachfolgende Runner.
setEnv(prevDataDir, prevAgentId)
resetSelfCheckForTest()

process.exitCode = failed ? 1 : 0
