/**
 * Regressionstest: nexus_forget räumt die Capture-Retry-Queue mit (ClawHub-Audit 1.22.0)
 *
 * Befund: `nexus_forget` löschte nur in Qdrant. Ein Text, dessen Capture
 * früher fehlgeschlagen war, lag weiter in capture-retry-queue.jsonl und kam
 * beim nächsten Drain ZURÜCK — eine gelöschte Erinnerung tauchte wieder auf.
 *
 * Gepinnt wird hier:
 *  a) Treffer über die Erinnerungs-ID wird entfernt, andere Einträge bleiben
 *  b) Eintrag OHNE ID wird über den Text gefunden
 *  c) Eintrag mit FREMDER ID bleibt stehen, auch bei gleichem Text
 *  d) unparsebare Zeile bleibt unverändert erhalten (nicht "sicher" Löschbar)
 *  e) nichts passt → Datei wird nicht angefasst
 *  f) Datei nicht lesbar → kein Absturz, Warnung, Qdrant-Löschung gilt als erfolgreich
 *  g) keine Queue-Datei → kein Fehler, keine Datei wird angelegt
 *  h) Datei-Rechte bleiben erhalten (atomarer Rewrite über Temp + rename)
 *
 * Kein Qdrant: der Client ist ein Stub. Die Queue liegt immer in einem
 * Temp-Verzeichnis (NEXUS_CAPTURE_QUEUE_FILE) — ~/.openclaw bleibt unberührt.
 */
import assert from "node:assert"
import { register } from "node:module"
import {
  chmodSync,
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  statSync,
  writeFileSync,
} from "node:fs"
import { tmpdir } from "node:os"
import { join } from "node:path"

register("./_typebox-test-loader.mjs", import.meta.url)

const SANDBOX = mkdtempSync(join(tmpdir(), "nexus-forget-queue-"))
let queuePath = join(SANDBOX, "capture-retry-queue.jsonl")
process.env.NEXUS_CAPTURE_QUEUE_FILE = queuePath
process.env.NEXUS_AGENT_SELFTEST = "1"

const warnings = []
const { initLogger } = await import("./logger.ts")
initLogger(
  {
    info() {},
    warn: (msg, ...args) => warnings.push(`${msg} ${args.map(String).join(" ")}`),
    error() {},
    debug() {},
  },
  false,
)

const { registerForgetTool } = await import("./tools/forget.ts")
// ClawHub-Audit 1.22.0: forget liest seine Stufe aus dem Turn-Kontext —
// ohne Merkmal gilt fail-closed "public" (dann schlüge die Löschung fehl).
const { setTurnContext, resetTurnContext } = await import("./lib/turn-context.ts")

let failed = 0
const t = (name, fn) =>
  Promise.resolve()
    .then(fn)
    .then(() => console.log("PASS ", name))
    .catch((e) => {
      failed++
      console.log("FAIL ", name, "—", e?.message ?? String(e))
    })

// ── Fixtures ───────────────────────────────────────────────────────────────

const MEMORY_TEXT = "Erinnerung eins: der wichtige Inhalt"

const ENTRY_BY_ID = { id: "mem-1", text: MEMORY_TEXT, payload: { text: MEMORY_TEXT } }
// Ohne id: kann nur über den Text zugeordnet werden (Alt-/Handschreib-Eintrag).
const ENTRY_NO_ID = { text: MEMORY_TEXT, payload: { text: MEMORY_TEXT } }
const ENTRY_OTHER = {
  id: "mem-2",
  text: "andere Erinnerung",
  payload: { text: "andere Erinnerung" },
}

const lines = (entries) => entries.map((e) => JSON.stringify(e)).join("\n") + "\n"
const queueLines = () =>
  readFileSync(queuePath, "utf8")
    .split("\n")
    .filter(Boolean)

function seedQueue(content) {
  writeFileSync(queuePath, content, "utf8")
}

function forgetTool({ level = "private" } = {}) {
  let tool = null
  let deleted = []
  registerForgetTool(
    { registerTool: (tt) => { tool = tt } },
    { embed: async () => [0.1, 0.2] },
    {
      scrollPointStrict: async (id) => ({
        id,
        payload: { text: MEMORY_TEXT, access_level: "private" },
      }),
      searchByVector: async () => [],
      delete: async (id) => { deleted.push(id) },
    },
    { accessLevel: level, forgetMinScore: 0.5 },
  )
  assert.ok(tool, "nexus_forget muss registriert sein")
  return { tool, deleted: () => deleted }
}

const dmTurn = () => {
  resetTurnContext()
  setTurnContext(false) // Direktchat: cfg.accessLevel = private ist wirksam
}

const textOf = (res) => (res.content ?? []).map((c) => c.text).join("\n")

// ── a) Treffer über die ID ─────────────────────────────────────────────────

await t("a) Löschen nach ID entfernt den passenden Eintrag, andere bleiben", async () => {
  seedQueue(lines([ENTRY_BY_ID, ENTRY_OTHER]))
  dmTurn()
  const { tool, deleted } = forgetTool()
  const res = await tool.execute("t", { memoryId: "mem-1" })
  assert.strictEqual(res.isError, undefined, `Löschung fehlgeschlagen: ${textOf(res)}`)
  assert.deepStrictEqual(deleted(), ["mem-1"], "Qdrant-Löschung muss erfolgen")
  const left = queueLines().map((l) => JSON.parse(l).id)
  assert.deepStrictEqual(left, ["mem-2"], `nur mem-2 darf bleiben, war: ${left.join(",")}`)
})

// ── b) Treffer über den Text, wenn der Eintrag keine ID trägt ──────────────

await t("b) Eintrag ohne ID wird über den Text gefunden", async () => {
  seedQueue(lines([ENTRY_NO_ID, ENTRY_OTHER]))
  dmTurn()
  const { tool } = forgetTool()
  await tool.execute("t", { memoryId: "mem-1" })
  const left = queueLines().map((l) => JSON.parse(l).id)
  assert.deepStrictEqual(left, ["mem-2"], `nur mem-2 darf bleiben, war: ${left.join(",")}`)
})

// ── c) fremde ID bleibt stehen, auch bei gleichem Text ─────────────────────

await t("c) Eintrag mit FREMDER ID bleibt stehen (Text allein reicht nicht)", async () => {
  const foreign = { id: "mem-9", text: MEMORY_TEXT, payload: { text: MEMORY_TEXT } }
  seedQueue(lines([foreign]))
  dmTurn()
  const { tool } = forgetTool()
  await tool.execute("t", { memoryId: "mem-1" })
  const left = queueLines().map((l) => JSON.parse(l).id)
  assert.deepStrictEqual(left, ["mem-9"], "fremde ID gehört zu einer anderen Erinnerung")
})

// ── d) unparsebare Zeile bleibt unangetastet ───────────────────────────────

await t("d) unparsebare Zeile bleibt unverändert erhalten", async () => {
  const corrupt = '{"id":"mem-1","text":"abgeschnitten'
  seedQueue(corrupt + "\n" + lines([ENTRY_BY_ID]))
  dmTurn()
  const { tool } = forgetTool()
  await tool.execute("t", { memoryId: "mem-1" })
  const left = queueLines()
  assert.strictEqual(left.length, 1, `genau die kaputte Zeile darf bleiben, waren ${left.length}`)
  assert.strictEqual(left[0], corrupt, "kaputte Zeile muss zeichengleich erhalten bleiben")
})

// ── e) nichts passt → Datei unangetastet ──────────────────────────────────

await t("e) kein Treffer → Datei wird nicht angefasst", async () => {
  seedQueue(lines([ENTRY_OTHER]))
  const before = readFileSync(queuePath, "utf8")
  dmTurn()
  const { tool } = forgetTool()
  await tool.execute("t", { memoryId: "mem-1" })
  assert.strictEqual(readFileSync(queuePath, "utf8"), before, "ohne Treffer darf nicht neu geschrieben werden")
})

// ── f) Queue nicht lesbar → Warnung, Löschung bleibt erfolgreich ──────────

await t("f) Queue nicht lesbar → Warnung, Qdrant-Löschung gilt als erfolgreich", async () => {
  // Ein Verzeichnis an der Queue-Position ist zuverlässig unlesbar (EISDIR) —
  // unabhängig von chmod/root.
  const dirPath = join(SANDBOX, "queue-als-verzeichnis")
  mkdirSync(dirPath, { recursive: true })
  process.env.NEXUS_CAPTURE_QUEUE_FILE = dirPath
  try {
    warnings.length = 0
    dmTurn()
    const { tool, deleted } = forgetTool()
    const res = await tool.execute("t", { memoryId: "mem-1" })
    assert.strictEqual(res.isError, undefined, `Löschung muss gelingen: ${textOf(res)}`)
    assert.match(textOf(res), /Memory forgotten/i)
    assert.deepStrictEqual(deleted(), ["mem-1"], "Qdrant-Löschung muss erfolgen")
    assert.ok(
      warnings.some((w) => /nicht lesbar/i.test(w)),
      `Warnung erwartet, gesehen: ${JSON.stringify(warnings)}`,
    )
  } finally {
    process.env.NEXUS_CAPTURE_QUEUE_FILE = queuePath
    rmSync(dirPath, { recursive: true, force: true })
  }
})

// ── g) keine Queue-Datei ──────────────────────────────────────────────────

await t("g) keine Queue-Datei → kein Fehler, keine Datei angelegt", async () => {
  rmSync(queuePath, { force: true })
  dmTurn()
  const { tool, deleted } = forgetTool()
  const res = await tool.execute("t", { memoryId: "mem-1" })
  assert.strictEqual(res.isError, undefined)
  assert.deepStrictEqual(deleted(), ["mem-1"])
  assert.strictEqual(existsSync(queuePath), false, "ohne Queue darf keine Datei entstehen")
})

// ── h) Rechte bleiben erhalten ────────────────────────────────────────────

await t("h) Datei-Rechte bleiben beim Rewrite erhalten", async () => {
  seedQueue(lines([ENTRY_BY_ID, ENTRY_OTHER]))
  chmodSync(queuePath, 0o640)
  const before = statSync(queuePath).mode & 0o777
  dmTurn()
  const { tool } = forgetTool()
  await tool.execute("t", { memoryId: "mem-1" })
  const after = statSync(queuePath).mode & 0o777
  assert.strictEqual(after, before, `Rechte müssen erhalten bleiben (vorher ${before.toString(8)}, nachher ${after.toString(8)})`)
  assert.deepStrictEqual(queueLines().map((l) => JSON.parse(l).id), ["mem-2"])
})

rmSync(SANDBOX, { recursive: true, force: true })

process.exitCode = failed ? 1 : 0
