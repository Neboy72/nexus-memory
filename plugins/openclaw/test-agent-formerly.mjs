/**
 * Punkt 4c: der direkte Registry-Writer des OpenClaw-Plugins darf aus einem
 * ``formerly``-Namen keinen zweiten Eintrag machen.
 *
 * Das Plugin spricht Qdrant direkt an und zaehlt deshalb selbst in
 * ``~/.nexus-memory/agents.json``. Nach ``--rename hermes hx7m2p`` traegt der
 * Eintrag ``formerly: ["hermes"]``; ein Aufrufer, der sich noch alt meldet,
 * muss auf die heutige Kennung zaehlen — sonst entsteht neben dem umbenannten
 * Agenten ein Geistereintrag mit 0 Zugriffen.
 *
 * Der Test lenkt HOME in eine Sandbox (``os.homedir()`` liest ``$HOME`` auf
 * POSIX) und importiert das Modul erst danach — der Pfad wird beim Import
 * gebunden.
 */
import assert from "node:assert"
import { mkdtempSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs"
import { tmpdir } from "node:os"
import { join } from "node:path"
import { register } from "node:module"

register("./_typebox-test-loader.mjs", import.meta.url)

const SANDBOX = mkdtempSync(join(tmpdir(), "nexus-formerly-"))
process.env.HOME = SANDBOX
// Diesen Writer pruefen — der Selbsttest-Riegel muss aus sein.
delete process.env.NEXUS_AGENT_SELFTEST
mkdirSync(join(SANDBOX, ".nexus-memory"), { recursive: true })
const REGISTRY = join(SANDBOX, ".nexus-memory", "agents.json")

const ISO = "2026-01-01T00:00:00+00:00"
writeFileSync(
  REGISTRY,
  JSON.stringify(
    {
      agents: [
        {
          id: "hx7m2p",
          name: "Hermes Buero",
          icon: "H",
          trust_level: "private",
          install_type: "plugin+mcp",
          config_dir: null,
          connected_at: ISO,
          last_seen: ISO,
          reads: 4,
          writes: 2,
          formerly: ["hermes"],
        },
      ],
    },
    null,
    2,
  ) + "\n",
)

process.env.NEXUS_AGENT_ID = "hermes"

const { recordAgentAccess } = await import("./lib/qdrant-client.ts")

let failed = 0
const t = (name, fn) =>
  Promise.resolve()
    .then(fn)
    .then(() => console.log("PASS ", name))
    .catch((e) => {
      failed++
      console.log("FAIL ", name, "—", e?.message ?? String(e))
    })

await t("recordAgentAccess zaehlt auf die heutige Kennung", () => {
  recordAgentAccess(true, true)
  const registry = JSON.parse(readFileSync(REGISTRY, "utf8"))
  assert.deepStrictEqual(
    registry.agents.map((a) => a.id),
    ["hx7m2p"],
    "kein zweiter Eintrag unter dem alten Namen",
  )
  const entry = registry.agents[0]
  assert.strictEqual(entry.reads, 5)
  assert.strictEqual(entry.writes, 3)
  assert.strictEqual(entry.name, "Hermes Buero", "Alias darf das Label nicht ueberschreiben")
  assert.strictEqual(entry.trust_level, "private")
})

rmSync(SANDBOX, { recursive: true, force: true })
process.exitCode = failed ? 1 : 0
