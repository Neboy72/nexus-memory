/**
 * Regressionstest: Gruppen-Gates und Eigentuemer-Kennungen
 *
 * Die Kanal-Freigabe (`privateGroupIds`) ist am 07.11.2026 verworfen worden —
 * die Autoren-Erkennung (lib/room-state.ts + `ownerUserIds`) ersetzt sie.
 * Dieser Test haelt fest, was davon bleibt:
 *
 *   f) `ownerUserIds`: Strings und Nummern, getrimmt, Blanks/Duplikate raus.
 *   f2) Default leer; Nicht-Array (Config-Tippfehler) → leer.
 *   f4) Schema (Manifest UND nexusConfigSchema) deklariert ownerUserIds als Array.
 *   a) Gruppen-Turn ohne Beobachtung → "public" (fail-closed).
 *   b) Gruppen-Turn → Werkzeug-Merkmal isGroup=true.
 *   c) Ohne (frisches) Merkmal bleibt es "public" — fail-closed.
 *   d4) Einzelchat → cfg.accessLevel wie bisher.
 *   e) CAPTURE bleibt in JEDEM Gruppen-Turn gesperrt.
 *
 * Kein Qdrant, kein Netz: Clients sind Stubs.
 */
import assert from "node:assert"
import { register } from "node:module"
import { mkdtempSync, readFileSync, rmSync } from "node:fs"
import { tmpdir } from "node:os"
import { join } from "node:path"

register("./_typebox-test-loader.mjs", import.meta.url)

// Sandbox VOR jedem Modul-Import: Capture-Queue, Self-Check-Datenverzeichnis und
// der Update-Check-Cache (HOME) duerfen nie im echten Home landen.
const SANDBOX = mkdtempSync(join(tmpdir(), "nexus-private-groups-"))
process.env.NEXUS_CAPTURE_QUEUE_FILE = join(SANDBOX, "capture-retry-queue.jsonl")
process.env.NEXUS_AGENT_SELFTEST = "1"
const REAL_HOME = process.env.HOME
process.env.HOME = SANDBOX

const { parseConfig, nexusConfigSchema } = await import("./lib/config.ts")
const { resetTurnContext, getTurnContext, toolAccessLevel } = await import(
  "./lib/turn-context.ts"
)
const { buildRecallHandler } = await import("./hooks/recall.ts")
const { buildCaptureHandler } = await import("./hooks/capture.ts")
const { registerSearchTool } = await import("./tools/search.ts")

let failed = 0
const t = (name, fn) =>
  Promise.resolve()
    .then(fn)
    .then(() => console.log("PASS ", name))
    .catch((e) => {
      failed++
      console.log("FAIL ", name, "—", e?.message ?? String(e))
    })

/** Synthetische IDs — echte Deploy-IDs gehoeren nicht ins Repo. */
const GROUP = "777001"
const OTHER = "777002"

const cfgFor = (over = {}) => ({
  accessLevel: "private",
  maxRecallResults: 5,
  scope: "",
  ...over,
})

/** Recall-Handler mit Stub-Client: zeichnet auf, mit WELCHER Stufe gesucht wird. */
async function recallLevels(cfg, ctx, prompt = "was weiss ich ueber das projekt") {
  const levels = []
  const handler = buildRecallHandler(
    { embed: async () => [0.1] },
    {
      search: async (_vector, _limit, level) => {
        levels.push(level)
        return []
      },
    },
    cfg,
  )
  await handler({ prompt }, ctx)
  return levels
}

// ── f) Normalisierung ──────────────────────────────────────────────────────

await t("f) ownerUserIds: Nummern/Strings getrimmt, Blanks + Duplikate raus", () => {
  const ids = parseConfig({
    ownerUserIds: [777001, " 777002 ", "  ", "", null, "777002", true, {}],
  }).ownerUserIds
  assert.deepStrictEqual(ids, ["777001", "777002"])
})

await t("f2) ownerUserIds: Default leer, Nicht-Array → leer (Eigentuemer unbekannt)", () => {
  assert.deepStrictEqual(parseConfig({}).ownerUserIds, [])
  assert.deepStrictEqual(parseConfig({ ownerUserIds: "777001" }).ownerUserIds, [])
  assert.deepStrictEqual(parseConfig({ ownerUserIds: 777001 }).ownerUserIds, [])
})

// ── f4) Schema ─────────────────────────────────────────────────────────────

await t("f4) Schema deklariert ownerUserIds als Array (Code UND Manifest)", () => {
  const props = nexusConfigSchema?.jsonSchema?.properties
  assert.ok(props?.ownerUserIds, "properties.ownerUserIds fehlt")
  assert.strictEqual(props.ownerUserIds.type, "array")

  const manifest = JSON.parse(
    readFileSync(new URL("./openclaw.plugin.json", import.meta.url), "utf8"),
  )
  const declared = manifest?.configSchema?.properties?.ownerUserIds
  assert.ok(declared, "configSchema.properties.ownerUserIds fehlt")
  assert.strictEqual(declared.type, "array")
})

// ── a) Gruppen-Turn ohne Beobachtung → public ──────────────────────────────

await t("a) Gruppen-Turn ohne Beobachtung → Recall fragt 'public'", async () => {
  for (const groupId of [GROUP, Number(GROUP), `  ${GROUP}  `]) {
    resetTurnContext()
    const levels = await recallLevels(cfgFor(), { trigger: "user", groupId })
    assert.deepStrictEqual(
      levels,
      ["public"],
      `groupId=${JSON.stringify(groupId)} muss gecappt bleiben`,
    )
  }
})

// ── b) Gruppen-Turn wirkt auf die Werkzeuge (Turn-Merkmal) ─────────────────

await t("b) Gruppen-Turn → Werkzeug-Merkmal isGroup=true", async () => {
  resetTurnContext()
  await recallLevels(cfgFor(), { trigger: "user", groupId: GROUP, sessionKey: "grp-1" })
  const marker = getTurnContext()
  assert.ok(marker, "Merkmal muss gesetzt sein")
  assert.strictEqual(marker.isGroup, true, "Gruppen-Turn bleibt eine Gruppe")
  assert.strictEqual(toolAccessLevel("private"), "public", "Werkzeug sieht nur 'public'")
})

await t("b2) Gruppen-Turn → nexus_search fragt wirklich 'public'", async () => {
  resetTurnContext()
  await recallLevels(cfgFor(), { trigger: "user", groupId: GROUP })
  const seen = []
  let tool = null
  registerSearchTool(
    { registerTool: (tt) => { tool = tt } },
    { embed: async () => [0.1] },
    {
      search: async (_vector, _limit, level) => {
        seen.push(level)
        return []
      },
    },
    cfgFor(),
  )
  assert.ok(tool, "nexus_search muss registriert sein")
  const res = await tool.execute("t", { query: "irgendetwas" })
  assert.ok(!res.isError, "Suche darf nicht fehlschlagen")
  assert.deepStrictEqual(seen, ["public"], `Werkzeug muss cappen, war: ${seen[0]}`)
})

// ── c) ohne Merkmal bleibt es fail-closed ──────────────────────────────────

await t("c) kein Merkmal → 'public'", async () => {
  resetTurnContext()
  assert.strictEqual(toolAccessLevel("private"), "public")
  const seen = []
  let tool = null
  registerSearchTool(
    { registerTool: (tt) => { tool = tt } },
    { embed: async () => [0.1] },
    {
      search: async (_vector, _limit, level) => {
        seen.push(level)
        return []
      },
    },
    cfgFor(),
  )
  await tool.execute("t", { query: "irgendetwas" })
  assert.deepStrictEqual(seen, ["public"], "ohne Merkmal darf nichts durch")
})

// ── d) Einzelchat ──────────────────────────────────────────────────────────

await t("d4) Einzelchat ohne ownerUserIds → cfg-Stufe wie bisher", async () => {
  resetTurnContext()
  // Alte Konfiguration ohne den Key darf nicht werfen (Bestandstests bauen cfg so).
  const levels = await recallLevels(cfgFor(), { trigger: "user", groupId: null })
  assert.deepStrictEqual(levels, ["private"])
  assert.strictEqual(getTurnContext()?.isGroup, false)
})

await t("d5) leere Gruppen-ID → Gruppe (fail-closed)", async () => {
  resetTurnContext()
  const levels = await recallLevels(cfgFor(), { trigger: "user", groupId: "" })
  assert.deepStrictEqual(levels, ["public"])
  assert.strictEqual(getTurnContext()?.isGroup, true)
})

// ── e) Capture bleibt gesperrt ─────────────────────────────────────────────

function captureHandler(cfg) {
  const upserts = []
  const handler = buildCaptureHandler(
    { embed: async () => [0.1] },
    {
      upsert: async (id, _vector, payload) => {
        upserts.push({ id, payload })
      },
    },
    cfg,
  )
  return { handler, upserts }
}

const captureEvent = (text) => ({
  success: true,
  messages: [{ role: "user", content: text }],
})

await t("e) Gruppen-Turn → Capture bleibt gesperrt (Regression)", async () => {
  const { handler, upserts } = captureHandler(cfgFor({ ownerUserIds: [GROUP] }))
  await handler(captureEvent("Kanalinhalt mit Agenten-Geruell, gehoert nicht ins Gedaechtnis"), {
    trigger: "user",
    messageProvider: "telegram",
    groupId: GROUP,
  })
  assert.strictEqual(upserts.length, 0, "ein Gruppen-Turn darf NICHT gecaptured werden")
})

await t("e2) fremder Kanal → Capture gesperrt (unveraendert)", async () => {
  const { handler, upserts } = captureHandler(cfgFor())
  await handler(captureEvent("Fremder Gruppeninhalt, darf nie ins private Gedaechtnis"), {
    trigger: "user",
    messageProvider: "telegram",
    groupId: OTHER,
  })
  assert.strictEqual(upserts.length, 0)
})

await t("e3) Einzelchat → Capture speichert (Harness-Gegenprobe)", async () => {
  const { handler, upserts } = captureHandler(cfgFor())
  await handler(captureEvent("DM-Nachricht fuer das private Gedaechtnis des Eigentuemers"), {
    trigger: "user",
    messageProvider: "telegram",
    groupId: null,
  })
  assert.strictEqual(upserts.length, 1, "DM muss weiterhin gespeichert werden")
  assert.strictEqual(upserts[0].payload.access_level, "private")
})

process.env.HOME = REAL_HOME
rmSync(SANDBOX, { recursive: true, force: true })
process.exitCode = failed ? 1 : 0
