/**
 * Regressionstest: Eigentuemer-Quellen (ClawHub-Audit 14.11.2026, MCP07-1)
 *
 * Befund: Die erste angenommene menschliche Nachricht auf einer Plattform wurde
 * dauerhaft als Eigentuemer gespeichert (`<stateDir>/nexus/owner.json`,
 * `reason: "first-human"`). Ein Fremder, der zuerst schreibt, wurde damit
 * Eigentuemer und bekam private Erinnerungen.
 *
 * Regel seit 1.22.4: Der Plugin vergibt den Eigentuemer NICHT mehr selbst. Es
 * gibt genau zwei Quellen:
 *   1. Konfiguration `ownerUserIds` (eine ID je Plattform),
 *   2. eine bereits vorhandene `owner.json` — sie bleibt LESBAR, damit laufende
 *      Installationen ihren Zustand nicht verlieren, wird aber nie erweitert.
 * Ohne beides ist der Eigentuemer unbekannt: jede menschliche Nachricht markiert
 * ihren Raum als geteilt, und Lesezugriffe bleiben `public` (fail-closed).
 * Der Reset ist das Loeschen der Datei (README) — danach gilt wieder Quelle 1
 * bzw. "unbekannt".
 *
 * Der Dateiname ist historisch; geprueft wird die AKTUELLE Regel.
 *
 * Geprueft wird:
 *   a) isDirectSessionKey — Diagnose-Helfer, steuert nichts
 *   b) ERSTER FREMDER in einem neuen Raum → kein Eigentuemer, Raum geteilt
 *   c) Bot und Mensch ohne Konfiguration → niemand wird Eigentuemer
 *   d) NEUSTART mit vorhandener owner.json → Datei wird gelesen, nicht erweitert
 *   e) ownerUserIds gesetzt → Config gewinnt, Datei wird ignoriert
 *   f) Reset (Datei geloescht) → Eigentuemer wieder unbekannt
 *   g) kaputte Datei → genau eine Warnung, kein Absturz, wie ohne Eigentuemer
 *   h) version:1-Datei → verworfen (keine Plattform), einmal vermerkt
 *   i) Raum-Schluessel: `chat:<conversationId>` hat Vorrang vor `session:…`
 *   j) kein Schreibvorgang mehr aus einer Nachricht (Datei entsteht nie)
 *   k) Nachricht ohne Plattform → keine Wirkung auf die Eigentuemer-Frage
 *
 * Kein Netz, kein Qdrant, kein echtes Home: jeder Test bekommt ein frisches
 * Temp-Verzeichnis als OPENCLAW_STATE_DIR, HOME zeigt auf die Sandbox.
 */
import assert from "node:assert"
import { register } from "node:module"
import {
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs"
import { tmpdir } from "node:os"
import { join } from "node:path"

register("./_typebox-test-loader.mjs", import.meta.url)

const SANDBOX = mkdtempSync(join(tmpdir(), "nexus-owner-"))
const REAL_HOME = process.env.HOME
const REAL_STATE = process.env.OPENCLAW_STATE_DIR
process.env.HOME = SANDBOX

const { initLogger } = await import("./logger.ts")
const {
  isDirectSessionKey,
  ownerFilePath,
  readLearnedOwner,
  resetOwnerWarnings,
  resolveOwnerId,
  resolveOwnerIds,
} = await import("./lib/owner.ts")
const { getRoomState, isRoomShared, resetRoomState, roomKeyOf } = await import(
  "./lib/room-state.ts"
)
const { buildMessageReceivedHandler } = await import("./hooks/room-observer.ts")

let failed = 0
const t = (name, fn) =>
  Promise.resolve()
    .then(fn)
    .then(() => console.log("PASS ", name))
    .catch((e) => {
      failed++
      console.log("FAIL ", name, "—", e?.message ?? String(e))
    })

let infos = []
let warns = []
initLogger(
  {
    info: (msg) => infos.push(String(msg)),
    warn: (msg) => warns.push(String(msg)),
    error: () => {},
    debug: () => {},
  },
  false,
)

/** Frisches State-Verzeichnis + frischer Prozesszustand fuer einen Test. */
let dirCount = 0
function freshState() {
  const dir = join(SANDBOX, `state-${++dirCount}`)
  mkdirSync(dir, { recursive: true })
  process.env.OPENCLAW_STATE_DIR = dir
  resetRoomState()
  resetOwnerWarnings()
  infos = []
  warns = []
  return dir
}

const handlerFor = (cfg = { ownerUserIds: [] }) => buildMessageReceivedHandler(cfg)

/**
 * Eine eingehende Nachricht, wie der Host sie liefert.
 *
 * `sessionKey` und `conversationId` sind getrennte Felder, weil die Raum-Regel
 * `conversationId` den Vorrang gibt (Fall i) — im echten Telegram-Direktchat
 * ist der sessionKey `agent:main:main` und traegt gar keine Chat-ID.
 * `platform` landet als `channelId` im Hook-Kontext (das ist die Plattform,
 * nicht die Chat-ID).
 */
const message = (senderId, sessionKey, chatId, { platform = "discord", event = {} } = {}) => [
  { senderId, ...event },
  { sessionKey, chatId, channelId: platform },
]

/** Schreibt eine owner.json, wie sie eine bestehende Installation hat. */
function writeOwnerFile(dir, record) {
  const file = join(dir, "nexus", "owner.json")
  mkdirSync(join(dir, "nexus"), { recursive: true })
  writeFileSync(file, JSON.stringify(record))
  return file
}

// ── a) Einzelchat-Erkennung (nur Diagnose) ─────────────────────────────────

await t("a) isDirectSessionKey: direct/dm erkannt — reine Diagnose, kein Gate", () => {
  freshState()
  for (const key of [
    "discord:direct:123",
    "discord:dm:123",
    "telegram:direct:42",
    "Discord:Direct:123",
    "agent:main:discord:direct:123",
  ]) {
    assert.strictEqual(isDirectSessionKey(key), true, `${key} muss Einzelchat sein`)
  }
  for (const key of [
    "discord:group:456",
    "discord:channel:456",
    "telegram:group:-100123",
    "discord:admin:1",
    "", // OpenClaws Telegram-Direktchat: kein direct/dm-Segment
    "agent:main:main",
    "   ",
    null,
    undefined,
    {},
    ["discord:direct:123"],
  ]) {
    assert.strictEqual(isDirectSessionKey(key), false, `${JSON.stringify(key)} darf kein Einzelchat sein`)
  }
})

// ── b) der erste Fremde wird NICHT Eigentuemer ────────────────────────────

await t("b) erster Fremder in einem neuen Raum → kein Eigentuemer, Raum geteilt", () => {
  const dir = freshState()
  const handler = handlerFor()
  handler(...message("6666", "discord:direct:999", "999"))

  assert.deepStrictEqual(
    resolveOwnerIds({ ownerUserIds: [] }),
    [],
    "eine eingehende Nachricht darf keinen Eigentuemer erzeugen",
  )
  assert.strictEqual(resolveOwnerId({ ownerUserIds: [] }), null)
  assert.strictEqual(
    existsSync(ownerFilePath()),
    false,
    `der Hook darf keine owner.json schreiben (${join(dir, "nexus")})`,
  )

  // Ohne Eigentuemer gilt der Schreiber als fremd → Raum geteilt → public.
  const room = roomKeyOf({ conversationId: "999" })
  assert.strictEqual(room, "chat:999")
  assert.strictEqual(isRoomShared(room), true, "ohne Eigentuemer ist jeder Mensch fremd")
  assert.strictEqual(getRoomState(room)?.reason, "unknown-owner")
})

// ── c) Bot und Mensch ohne Konfiguration ──────────────────────────────────

await t("c) Bot zuerst, dann Mensch — niemand wird Eigentuemer", () => {
  freshState()
  const handler = handlerFor()
  handler(...message("9001", "discord:direct:1", "1", { event: { isBot: true } }))
  handler(...message("9002", "discord:direct:1", "1"))

  assert.deepStrictEqual(resolveOwnerIds({ ownerUserIds: [] }), [])
  assert.strictEqual(existsSync(ownerFilePath()), false)
  assert.strictEqual(isRoomShared("chat:1"), true, "der Mensch bleibt fremd")
})

// ── d) NEUSTART mit vorhandener Datei ─────────────────────────────────────

await t("d) Neustart mit vorhandener owner.json → gelesen, aber nie erweitert", () => {
  const dir = freshState()
  writeOwnerFile(dir, {
    version: 2,
    owners: { discord: "4711" },
    learnedAt: 1_700_000_000_000,
    reason: "first-human",
  })

  // "Neustart": frischer Prozesszustand, gleiche Datei.
  resetRoomState()
  resetOwnerWarnings()

  assert.deepStrictEqual(resolveOwnerIds({ ownerUserIds: [] }), ["4711"], "Bestand bleibt lesbar")
  assert.strictEqual(resolveOwnerId({ ownerUserIds: [] }, "discord"), "4711")

  const handler = handlerFor()
  // Der Eigentuemer schreibt: sein Raum bleibt nicht geteilt.
  handler(...message("4711", "discord:direct:123", "123"))
  assert.strictEqual(isRoomShared("chat:123"), false)
  assert.strictEqual(getRoomState("chat:123")?.reason, "owner")

  // Ein Fremder auf einer NEUEN Plattform wird nicht eingetragen.
  handler(...message("8888", "telegram:group:-100", "-100", { platform: "telegram" }))
  const record = JSON.parse(readFileSync(ownerFilePath(), "utf8"))
  assert.deepStrictEqual(record.owners, { discord: "4711" }, "die Datei wird nicht erweitert")
  assert.strictEqual(isRoomShared("chat:-100"), true, "der Fremde markiert seinen Raum geteilt")
})

// ── e) Konfiguration gewinnt ──────────────────────────────────────────────

await t("e) ownerUserIds gesetzt → Config gewinnt, Datei wird ignoriert", () => {
  const dir = freshState()
  writeOwnerFile(dir, { version: 2, owners: { discord: "4711" }, learnedAt: 1, reason: "first-human" })

  const cfg = { ownerUserIds: ["999"] }
  assert.deepStrictEqual(resolveOwnerIds(cfg), ["999"])
  assert.strictEqual(resolveOwnerId(cfg, "discord"), "999")

  const handler = handlerFor(cfg)
  handler(...message("999", "discord:direct:5", "5"))
  assert.strictEqual(isRoomShared("chat:5"), false, "die konfigurierte ID ist der Eigentuemer")
  assert.strictEqual(getRoomState("chat:5")?.reason, "owner")

  handler(...message("4711", "discord:direct:6", "6"))
  assert.strictEqual(isRoomShared("chat:6"), true, "die ID aus der Datei gilt nicht mehr")
})

// ── f) Reset ──────────────────────────────────────────────────────────────

await t("f) Reset: Datei geloescht → Eigentuemer wieder unbekannt", () => {
  const dir = freshState()
  const file = writeOwnerFile(dir, {
    version: 2,
    owners: { telegram: "5763330319" },
    learnedAt: 1,
    reason: "first-human",
  })
  assert.deepStrictEqual(resolveOwnerIds({ ownerUserIds: [] }), ["5763330319"])

  rmSync(file)

  resetOwnerWarnings()
  assert.deepStrictEqual(resolveOwnerIds({ ownerUserIds: [] }), [], "nach dem Reset ist niemand Eigentuemer")
  assert.strictEqual(readLearnedOwner(), null)
  assert.strictEqual(existsSync(file), false)
})

// ── g) kaputte Datei ──────────────────────────────────────────────────────

await t("g) kaputte Datei → genau eine Warnung, kein Absturz", () => {
  const dir = freshState()
  mkdirSync(join(dir, "nexus"), { recursive: true })
  writeFileSync(join(dir, "nexus", "owner.json"), "{ das ist kein json")

  assert.strictEqual(readLearnedOwner(), null)
  assert.strictEqual(warns.length, 1, `genau eine Warnung erwartet, waren ${warns.length}`)
  assert.deepStrictEqual(resolveOwnerIds({ ownerUserIds: [] }), [], "kein Eigentuemer bekannt")

  // Ein zweiter Aufruf warnt nicht erneut (einmal pro Prozess und Datei).
  readLearnedOwner()
  assert.strictEqual(warns.length, 1)
})

// ── h) version:1 ──────────────────────────────────────────────────────────

await t("h) version:1-Datei (keine Plattform) → verworfen, einmal vermerkt", () => {
  const dir = freshState()
  writeOwnerFile(dir, { version: 1, ownerUserId: "4711" })

  assert.strictEqual(readLearnedOwner(), null, "ohne Plattform nicht zuordenbar")
  assert.deepStrictEqual(resolveOwnerIds({ ownerUserIds: [] }), [], "v1 darf nicht global gelten")
  assert.deepStrictEqual(resolveOwnerIds({ ownerUserIds: [] }, "telegram"), [])
  assert.strictEqual(warns.length, 1, "einmal vermerkt")

  // Der Hinweis nennt den Ausweg, nicht die ID.
  assert.ok(
    /ownerUserIds|version/.test(warns[0]),
    `Warnung muss den Ausweg nennen, war: ${warns[0]}`,
  )
})

// ── i) Raum-Schluessel ────────────────────────────────────────────────────

await t("i) Raum-Schluessel: chat:<conversationId> hat Vorrang vor session:…", () => {
  freshState()
  assert.strictEqual(roomKeyOf({ conversationId: "123", sessionKey: "agent:main:main" }), "chat:123")
  assert.strictEqual(roomKeyOf({ sessionKey: "agent:main:main" }), "session:agent:main:main")
  assert.strictEqual(roomKeyOf({}), null)
})

// ── j) kein Schreibvorgang mehr ───────────────────────────────────────────

await t("j) viele Nachrichten auf vielen Plattformen → nie eine Datei", () => {
  freshState()
  const handler = handlerFor()
  for (const platform of ["telegram", "discord", "slack"]) {
    for (let i = 0; i < 5; i++) {
      handler(...message(`user-${i}`, `${platform}:group:${i}`, `${platform}-${i}`, { platform }))
    }
  }
  assert.strictEqual(existsSync(ownerFilePath()), false, "der Hook schreibt grundsaetzlich nichts")
  assert.strictEqual(infos.filter((m) => /owner: learned/.test(m)).length, 0, "keine Lern-Zeile mehr")
})

// ── k) ohne Plattform ─────────────────────────────────────────────────────

await t("k) Nachricht ohne Plattform → keine Wirkung auf die Eigentuemer-Frage", () => {
  freshState()
  const handler = handlerFor()
  handler({ senderId: "4711" }, { sessionKey: "agent:main:main", chatId: "555" })

  assert.strictEqual(existsSync(ownerFilePath()), false)
  assert.deepStrictEqual(resolveOwnerIds({ ownerUserIds: [] }), [], "kein Eigentuemer bekannt")
  assert.strictEqual(isRoomShared("chat:555"), true, "ohne Besitzer gilt jeder Mensch als fremd")
})

process.env.HOME = REAL_HOME
if (REAL_STATE === undefined) delete process.env.OPENCLAW_STATE_DIR
else process.env.OPENCLAW_STATE_DIR = REAL_STATE
rmSync(SANDBOX, { recursive: true, force: true })
process.exitCode = failed ? 1 : 0
