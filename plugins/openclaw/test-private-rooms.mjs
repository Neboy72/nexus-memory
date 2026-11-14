/**
 * Regressionstest: Gruppen-Privatsphaere nur mit ausdruecklicher Freigabe
 * (ClawHub-Audit 14.11.2026, MCP07-2)
 *
 * Befund: Ein Raum galt als privat, solange kein fremder Mensch darin
 * geschrieben hatte. Stille Mitleser, spaeter Hinzugekommene, fremde Bots und
 * Threads waren unsichtbar — "es hat niemand Fremdes geschrieben" ist kein
 * Beweis fuer ein privates Publikum.
 *
 * Regel seit 1.22.4: Ein Gruppen-/Kanal-Turn liest `public`, solange der Raum
 * nicht ausdruecklich in `privateRooms` freigegeben ist (Vorgabe: leer). Die
 * Raum-Beobachtung bleibt bestehen, kann aber nur noch VERSCHAERFEN (ein
 * Fremder im Raum → public), nie mehr allein privaten Zugriff erlauben.
 *
 * Geprueft wird:
 *   a) stiller Mitleser: nur der Eigentuemer hat geschrieben, keine Freigabe → public
 *   b) freigegebener Raum → cfg.accessLevel
 *   c) freigegebener Raum, aber ein Fremder hat geschrieben → public
 *   d) Vorgabe ist leer (kein Raum ist versehentlich freigegeben)
 *   e) Freigabe-Normalisierung: nackte Chat-ID == chat:<id>
 *   f) ohne bekannten Eigentuemer → public, auch im Direktchat
 */
import assert from "node:assert"
import { register } from "node:module"
import { mkdtempSync, rmSync } from "node:fs"
import { tmpdir } from "node:os"
import { join } from "node:path"

register("./_typebox-test-loader.mjs", import.meta.url)

const SANDBOX = mkdtempSync(join(tmpdir(), "nexus-private-rooms-"))
process.env.NEXUS_AGENT_SELFTEST = "1"
// Der Eigentuemer-Status wird aus dem State-Verzeichnis gelesen: ohne diese
// Zeile wuerde der Test die echte ~/.openclaw/nexus/owner.json des Rechners
// sehen und je nach Maschine anders urteilen.
process.env.OPENCLAW_STATE_DIR = join(SANDBOX, "state")

const { parseConfig } = await import("./lib/config.ts")
const { isRoomApproved, normalizeRoomKey, observeAuthor, resetRoomState, roomKeyOf } =
  await import("./lib/room-state.ts")
const { resetTurnContext } = await import("./lib/turn-context.ts")
const { buildRecallHandler } = await import("./hooks/recall.ts")

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
const CHAT = "888001"
const OWNER = "owner-1"
const STRANGER = "stranger-1"

const cfgFor = (over = {}) => ({
  accessLevel: "private",
  maxRecallResults: 5,
  scope: "",
  ownerUserIds: [OWNER],
  privateRooms: [],
  ...over,
})

/** Recall-Handler mit Stub-Client: zeichnet auf, mit WELCHER Stufe gesucht wird. */
async function recallLevels(cfg, ctx) {
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
  await handler({ prompt: "was weiss ich ueber das projekt" }, ctx)
  return levels
}

/** Frischer Prozesszustand fuer einen Test. */
function fresh() {
  resetRoomState()
  resetTurnContext()
}

const group = (chatId = CHAT) => ({
  trigger: "user",
  groupId: chatId,
  chatId,
  sessionKey: `session-${chatId}`,
})

// ── a) stiller Mitleser ────────────────────────────────────────────────────

await t("a) stiller Mitleser: nur der Eigentuemer schrieb, keine Freigabe → public", async () => {
  fresh()
  // Der Eigentuemer hat geschrieben — der Raum ist aus Sicht der Beobachtung
  // "nicht geteilt". Genau daraus wurde vorher privater Zugriff abgeleitet.
  observeAuthor(roomKeyOf({ chatId: CHAT }), OWNER, false, [OWNER])
  assert.strictEqual(isRoomApproved([], roomKeyOf({ chatId: CHAT })), false)

  const levels = await recallLevels(cfgFor(), group())
  assert.deepStrictEqual(
    levels,
    ["public"],
    `ohne Freigabe darf ein Kanal nichts Privates sehen, war: ${levels[0]}`,
  )
})

// ── b) freigegebener Raum ──────────────────────────────────────────────────

await t("b) freigegebener Raum → cfg.accessLevel", async () => {
  fresh()
  observeAuthor(roomKeyOf({ chatId: CHAT }), OWNER, false, [OWNER])

  const levels = await recallLevels(cfgFor({ privateRooms: [CHAT] }), group())
  assert.deepStrictEqual(levels, ["private"], `freigegebener Raum sieht cfg-Stufe, war: ${levels[0]}`)
})

await t("b2) freigegebener Raum ohne jede Beobachtung → cfg.accessLevel", async () => {
  fresh()
  const levels = await recallLevels(cfgFor({ privateRooms: [`chat:${CHAT}`] }), group())
  assert.deepStrictEqual(levels, ["private"], "die Freigabe allein genuegt")
})

// ── c) ein Fremder schlaegt die Freigabe ───────────────────────────────────

await t("c) freigegebener Raum, aber ein Fremder schrieb → public", async () => {
  fresh()
  const room = roomKeyOf({ chatId: CHAT })
  observeAuthor(room, STRANGER, false, [OWNER])
  assert.strictEqual(isRoomApproved([CHAT], room), true, "die Freigabe besteht")

  const levels = await recallLevels(cfgFor({ privateRooms: [CHAT] }), group())
  assert.deepStrictEqual(levels, ["public"], "ein Fremder im Raum kappt auf public")
})

// ── d) Vorgabe leer ────────────────────────────────────────────────────────

await t("d) Vorgabe: privateRooms ist leer", () => {
  assert.deepStrictEqual(parseConfig({}).privateRooms, [])
  assert.deepStrictEqual(parseConfig({ privateRooms: [] }).privateRooms, [])
})

// ── e) Normalisierung ──────────────────────────────────────────────────────

await t("e) Freigabe-Normalisierung: nackte Chat-ID == chat:<id>", () => {
  assert.deepStrictEqual(parseConfig({ privateRooms: [CHAT, ` ${CHAT} `, 888002] }).privateRooms, [
    `chat:${CHAT}`,
    "chat:888002",
  ])
  assert.deepStrictEqual(parseConfig({ privateRooms: [`session:agent:main`] }).privateRooms, [
    "session:agent:main",
  ])
  // Nicht-Array (Config-Tippfehler), Blanks und Container → keine Freigabe.
  assert.deepStrictEqual(parseConfig({ privateRooms: CHAT }).privateRooms, [])
  assert.deepStrictEqual(parseConfig({ privateRooms: ["  ", null, {}, true] }).privateRooms, [])

  assert.strictEqual(normalizeRoomKey(CHAT), `chat:${CHAT}`)
  assert.strictEqual(normalizeRoomKey(`chat:${CHAT}`), `chat:${CHAT}`)
  assert.strictEqual(normalizeRoomKey(""), "")
  assert.strictEqual(isRoomApproved([CHAT], `chat:${CHAT}`), true)
  assert.strictEqual(isRoomApproved([CHAT], null), false, "ohne Raum keine Freigabe")
  assert.strictEqual(isRoomApproved([], `chat:${CHAT}`), false)
})

// ── f) ohne bekannten Eigentuemer ──────────────────────────────────────────

await t("f) ohne bekannten Eigentuemer → public, auch im Direktchat", async () => {
  fresh()
  // Kein ownerUserIds: jeder Mensch ist fremd, also ist der Raum geteilt.
  const cfg = cfgFor({ ownerUserIds: [] })
  const levels = await recallLevels(cfg, {
    trigger: "user",
    groupId: null,
    sessionKey: "dm-ohne-besitzer",
  })
  assert.deepStrictEqual(
    levels,
    ["public"],
    `ohne Eigentuemer bleibt der Direktchat public, war: ${levels[0]}`,
  )
})

rmSync(SANDBOX, { recursive: true, force: true })
process.exitCode = failed ? 1 : 0
