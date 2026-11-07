/**
 * Regressionstest: Eigentuemer pro PLATTFORM lernen (Auftrag 13/14/15)
 *
 * Regel seit Auftrag 13: Der erste Mensch, der dem Agenten schreibt, wird
 * Eigentuemer — in JEDEM Chat, nicht nur im Einzelchat. Grund: OpenClaw nennt
 * einen Telegram-Direktchat `agent:main:main`, das traegt kein `direct:`/`dm:`
 * Segment, die alte Einzelchat-Regel feuerte nie. Die Chat-Art ist deshalb
 * KEIN Teil der Entscheidung mehr; `isDirectSessionKey` bleibt nur als
 * Diagnose-Helfer. Ein Bot wird nie gelernt.
 *
 * Regel seit Auftrag 15: derselbe Mensch hat pro Plattform eine ANDERE ID.
 * Gelernt wird deshalb pro Plattform (`channelId` des Hook-Kontexts) in
 * `<stateDir>/nexus/owner.json`:
 *   {"version":2,"owners":{"telegram":"…","discord":"…"}}
 * Eine bekannte Plattform wird nie ueberschrieben. Ein alter version:1-Eintrag
 * (ohne Plattform) wird verworfen und einmal vermerkt — ihn global anzuwenden
 * war genau der Fehler (Discord-Kanaele des Besitzers galten als geteilt).
 *
 * Geprueft wird die aktuelle Regel:
 *   a) isDirectSessionKey — Diagnose-Helfer, steuert das Lernen NICHT
 *   b) erste menschliche Nachricht im Einzelchat → gelernt, Datei 0600
 *   c) erste menschliche Nachricht in einer Gruppe → ebenfalls gelernt
 *   d) dieselbe Person nochmal → kein zweites Schreiben
 *   e) Bot als erster Absender → nicht gelernt
 *   f) ownerUserIds gesetzt → kein Lernen (Config hat Vorrang)
 *   g) gelernte Datei beim Start → Eigentuemer gilt (Config schlaegt Datei)
 *   h) Datei kaputt → genau eine Warnung, kein Absturz, wie ohne Eigentuemer
 *   i) Raum-Schluessel: `chat:<conversationId>` hat Vorrang vor `session:…`
 *   j) genau EINE Log-Zeile beim Lernen, ohne volle ID
 *   k) Telegram + Discord desselben Menschen → beide IDs bleiben stehen
 *   l) version:1-Datei → verworfen, einmal vermerkt, danach neu gelernt
 *   m) Fremder auf einer gelernten Plattform → nichts gelernt, Raum geteilt
 *   n) Nachricht ohne Plattform → nichts gelernt (fail-closed)
 *
 * Kein Netz, kein Qdrant, kein echtes Home: jeder Test bekommt ein frisches
 * Temp-Verzeichnis als OPENCLAW_STATE_DIR, HOME zeigt auf die Sandbox.
 */
import assert from "node:assert"
import { register } from "node:module"
import { mkdirSync, mkdtempSync, readFileSync, rmSync, statSync, writeFileSync } from "node:fs"
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
  learnOwnerFromMessage,
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
 * nicht die Chat-ID) — ohne sie wird nichts gelernt.
 */
const message = (senderId, sessionKey, chatId, { platform = "discord", event = {} } = {}) => [
  { senderId, ...event },
  { sessionKey, chatId, channelId: platform },
]

/** Inhalt der gelernten Datei, oder null. */
const ownerFile = () => {
  try {
    return JSON.parse(readFileSync(ownerFilePath(), "utf8"))
  } catch {
    return null
  }
}

/** Existiert die gelernte Datei? */
const ownerFileExists = () => {
  try {
    readFileSync(ownerFilePath())
    return true
  } catch {
    return false
  }
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

// ── b) erste menschliche Nachricht im Einzelchat ───────────────────────────

await t("b) erste Nachricht eines Menschen im Einzelchat → gelernt, Datei 0600", () => {
  const dir = freshState()
  const handler = handlerFor()
  handler(...message("4711", "discord:direct:123", "123"))

  const file = ownerFilePath()
  assert.strictEqual(file, join(dir, "nexus", "owner.json"), "Pfad muss <stateDir>/nexus/owner.json sein")
  const record = ownerFile()
  assert.strictEqual(record.version, 2)
  assert.deepStrictEqual(record.owners, { discord: "4711" }, "genau die Plattform der Nachricht")
  assert.strictEqual(record.reason, "first-human")
  assert.ok(Number.isFinite(record.learnedAt) && record.learnedAt > 0, "learnedAt fehlt")
  assert.strictEqual(statSync(file).mode & 0o777, 0o600, "Datei muss 0600 sein")

  // Dieselbe Nachricht zaehlt sofort als Eigentuemer: der Raum bleibt privat.
  const room = roomKeyOf({ conversationId: "123" })
  assert.strictEqual(room, "chat:123")
  assert.strictEqual(isRoomShared(room), false)
  assert.strictEqual(getRoomState(room)?.reason, "owner")
})

// ── c) erste menschliche Nachricht in einer Gruppe ─────────────────────────

await t("c) erste Nachricht eines Menschen in einer Gruppe → ebenfalls gelernt", () => {
  freshState()
  const handler = handlerFor()
  handler(...message("4711", "discord:group:456", "456"))

  assert.strictEqual(ownerFileExists(), true, "der erste Mensch wird auch in der Gruppe gelernt")
  assert.deepStrictEqual(readLearnedOwner()?.owners, { discord: "4711" })
  assert.strictEqual(resolveOwnerId({ ownerUserIds: [] }), "4711")

  // Die Chat-Art entscheidet nicht mehr: der Raum des Eigentuemers bleibt privat.
  const room = roomKeyOf({ conversationId: "456" })
  assert.strictEqual(isRoomShared(room), false, "Eigentuemer in der Gruppe → Raum nicht geteilt")
  assert.strictEqual(getRoomState(room)?.reason, "owner")

  // Ein Fremder im selben Raum markiert ihn weiterhin als geteilt.
  handler(...message("888", "discord:group:456", "456"))
  assert.strictEqual(isRoomShared(room), true)
  assert.strictEqual(getRoomState(room)?.reason, "foreign-author")
})

// ── d) kein zweites Schreiben ──────────────────────────────────────────────

await t("d) dieselbe Person nochmal → kein zweites Schreiben", () => {
  freshState()
  const first = learnOwnerFromMessage({ senderId: "4711", isBot: false, channelId: "discord" })
  assert.strictEqual(first.learned, true)
  assert.strictEqual(first.reason, "first-human")
  assert.strictEqual(first.ownerUserId, "4711")
  assert.strictEqual(first.platform, "discord")
  const before = readFileSync(ownerFilePath(), "utf8")

  const second = learnOwnerFromMessage({ senderId: "4711", isBot: false, channelId: "discord" })
  assert.strictEqual(second.learned, false)
  assert.strictEqual(second.reason, "already-known")
  assert.strictEqual(readFileSync(ownerFilePath(), "utf8"), before, "Datei darf unveraendert bleiben")

  // Auch ein ANDERER Mensch nimmt den Platz nicht ein.
  const third = learnOwnerFromMessage({ senderId: "9999", isBot: false, channelId: "discord" })
  assert.strictEqual(third.learned, false, "ein bereits gelernter Eigentuemer bleibt")
  assert.deepStrictEqual(readLearnedOwner()?.owners, { discord: "4711" })
})

// ── e) Bots lernen nicht ───────────────────────────────────────────────────

await t("e) Absender ist Bot → nicht gelernt", () => {
  freshState()
  for (const event of [{ isBot: true }, { senderIsBot: true }, { sender: { isBot: true } }]) {
    const handler = handlerFor()
    handler(...message("4711", "discord:direct:123", "123", { event }))
    assert.strictEqual(warns.length, 0, `kein Fehler erwartet (${JSON.stringify(event)})`)
  }
  assert.strictEqual(ownerFileExists(), false, "ein Bot darf keine owner.json anlegen")
  assert.strictEqual(resolveOwnerId({ ownerUserIds: [] }), null, "ein Bot wird nie Eigentuemer")

  const direct = learnOwnerFromMessage({ senderId: "4711", isBot: true, channelId: "discord" })
  assert.strictEqual(direct.learned, false)
  assert.strictEqual(direct.reason, "bot")

  // Der naechste Mensch — nicht der Bot — wird gelernt.
  const human = learnOwnerFromMessage({ senderId: "4712", isBot: false, channelId: "discord" })
  assert.strictEqual(human.learned, true)
  assert.deepStrictEqual(readLearnedOwner()?.owners, { discord: "4712" })
})

// ── f) Config hat Vorrang ──────────────────────────────────────────────────

await t("f) ownerUserIds gesetzt → kein Lernen", () => {
  freshState()
  const handler = handlerFor({ ownerUserIds: ["999"] })
  handler(...message("4711", "discord:direct:123", "123"))

  assert.strictEqual(ownerFileExists(), false, "die Config darf nicht von einer Beobachtung ueberschrieben werden")
  assert.deepStrictEqual(resolveOwnerIds({ ownerUserIds: ["999"] }), ["999"])
  assert.deepStrictEqual(
    resolveOwnerIds({ ownerUserIds: ["999"] }, "telegram"),
    ["999"],
    "die Config gilt global, nicht pro Plattform",
  )

  // Der konfigurierte Eigentuemer bleibt fremd fuer diesen Raum.
  const room = roomKeyOf({ conversationId: "123" })
  assert.strictEqual(isRoomShared(room), true)
  assert.strictEqual(getRoomState(room)?.reason, "foreign-author")
})

// ── g) gelernte Datei gilt beim Start ──────────────────────────────────────

await t("g) gelernte Datei beim Start gelesen → Eigentuemer gilt", () => {
  const dir = freshState()
  mkdirSync(join(dir, "nexus"), { recursive: true })
  writeFileSync(
    ownerFilePath(),
    JSON.stringify({ version: 2, owners: { discord: "777" }, learnedAt: Date.now(), reason: "first-human" }),
  )

  assert.deepStrictEqual(readLearnedOwner()?.owners, { discord: "777" })
  assert.deepStrictEqual(resolveOwnerIds({ ownerUserIds: [] }), ["777"])
  assert.strictEqual(resolveOwnerId({ ownerUserIds: [] }), "777")

  // Der Eigentuemer schreibt in seiner eigenen Gruppe → Raum bleibt privat.
  const handler = handlerFor()
  handler(...message("777", "discord:group:456", "456"))
  const room = roomKeyOf({ conversationId: "456" })
  assert.strictEqual(isRoomShared(room), false)
  assert.strictEqual(getRoomState(room)?.reason, "owner")

  // Ein Fremder im selben Raum → geteilt (die Raum-Regel selbst bleibt scharf).
  handler(...message("888", "discord:group:456", "456"))
  assert.strictEqual(isRoomShared(room), true)
  assert.strictEqual(getRoomState(room)?.reason, "foreign-author")

  // Config schlaegt die gelernte Datei.
  assert.deepStrictEqual(resolveOwnerIds({ ownerUserIds: ["999"] }), ["999"])
})

// ── h) kaputte Datei ───────────────────────────────────────────────────────

await t("h) Datei kaputt → eine Warnung, kein Absturz, wie ohne Eigentuemer", () => {
  const dir = freshState()
  mkdirSync(join(dir, "nexus"), { recursive: true })
  writeFileSync(ownerFilePath(), "{ das ist kein json")

  assert.strictEqual(readLearnedOwner(), null)
  assert.strictEqual(warns.length, 1, "genau eine Warnung")
  assert.match(warns[0], /owner: could not read/)
  assert.ok(!warns[0].includes("kein json"), "die Warnung darf den Inhalt nicht wiedergeben")

  // Zweiter Leseversuch warnt nicht erneut.
  assert.strictEqual(readLearnedOwner(), null)
  assert.strictEqual(warns.length, 1)

  // Verhalten wie ohne Eigentuemer: die Eigentuemer-Menge ist leer.
  assert.deepStrictEqual(resolveOwnerIds({ ownerUserIds: [] }), [], "kein Eigentuemer bekannt")
  assert.strictEqual(resolveOwnerId({ ownerUserIds: [] }), null)

  // Mit gesetzter Config bleibt ein Fremder fremd → Raum geteilt (kein Lernen,
  // die Config hat Vorrang). Das zeigt die Raum-Regel ohne gelernten Besitzer.
  const configured = handlerFor({ ownerUserIds: ["999"] })
  configured(...message("888", "discord:group:456", "456"))
  const room = roomKeyOf({ conversationId: "456" })
  assert.strictEqual(isRoomShared(room), true)
  assert.strictEqual(getRoomState(room)?.reason, "foreign-author")

  // ... und die naechste menschliche Nachricht repariert die Datei (wie beim Erststart).
  const handler = handlerFor()
  handler(...message("4711", "discord:direct:123", "123"))
  assert.deepStrictEqual(readLearnedOwner()?.owners, { discord: "4711" })
  assert.strictEqual(warns.length, 1, "das Reparieren ist keine neue Warnung")
})

// ── i) Raum-Schluessel: chat: vor session: ─────────────────────────────────

await t("i) chat:<conversationId> hat Vorrang vor session:<sessionKey>", () => {
  freshState()
  const handler = handlerFor()

  // OpenClaws Telegram-Direktchat: der sessionKey ist agent-weit und traegt
  // keine Chat-ID — nur die conversationId benennt den Raum.
  handler(...message("4711", "agent:main:main", "555"))

  assert.strictEqual(
    roomKeyOf({ sessionKey: "agent:main:main", conversationId: "555" }),
    "chat:555",
    "die conversationId muss den Raum benennen",
  )
  assert.strictEqual(isRoomShared("chat:555"), false, "der Raum wurde unter chat:555 beobachtet")
  assert.strictEqual(
    getRoomState(roomKeyOf({ sessionKey: "agent:main:main" })),
    null,
    "der sessionKey darf NICHT als Raum dienen, wenn eine conversationId da ist",
  )

  // Ein anderer Chat derselben Session ist ein anderer Raum.
  handler(...message("888", "agent:main:main", "556"))
  assert.strictEqual(isRoomShared("chat:556"), true, "fremder Mensch → eigener Raum geteilt")
  assert.strictEqual(isRoomShared("chat:555"), false, "der erste Raum bleibt unberuehrt")
})

// ── j) genau eine Log-Zeile, ohne volle ID ─────────────────────────────────

await t("j) Lernen loggt genau eine Zeile ohne die volle ID", () => {
  freshState()
  learnOwnerFromMessage({ senderId: "4711", isBot: false, channelId: "discord" })

  // Der Logger stellt "nexus: " voran — danach beginnt die Modulzeile.
  const lines = infos.filter((msg) => msg.startsWith("nexus: owner:"))
  assert.strictEqual(lines.length, 1, `genau eine Zeile, waren: ${lines.length}`)
  assert.match(lines[0], /learned the owner for discord/)
  assert.ok(!lines[0].includes("4711"), `die volle ID darf nicht im Log stehen: ${lines[0]}`)
})

// ── k) Telegram + Discord desselben Menschen ───────────────────────────────

await t("k) Telegram dann Discord (andere ID) → beide bleiben stehen, kein drittes Schreiben", () => {
  freshState()
  const handler = handlerFor()

  // Der Besitzer auf Telegram.
  handler(...message("5763330319", "agent:main:main", "tg-1", { platform: "telegram" }))
  assert.deepStrictEqual(ownerFile().owners, { telegram: "5763330319" })
  assert.strictEqual(ownerFile().owners.discord, undefined, "Discord ist noch leer")

  // Derselbe Mensch auf Discord — andere ID, andere Plattform.
  handler(...message("373816693522432001", "discord:group:456", "456", { platform: "discord" }))
  const afterBoth = ownerFile().owners
  assert.deepStrictEqual(afterBoth, {
    telegram: "5763330319",
    discord: "373816693522432001",
  })
  console.log("       owner.json nach zwei Plattformen:", JSON.stringify({ version: 2, owners: afterBoth }))

  // Die Eigentuemer-Menge enthaelt BEIDE — ein Raum unterscheidet nicht nach
  // Plattform. Mit channelId steht die eigene Plattform vorn.
  assert.deepStrictEqual(resolveOwnerIds({ ownerUserIds: [] }), ["5763330319", "373816693522432001"])
  assert.deepStrictEqual(resolveOwnerIds({ ownerUserIds: [] }, "discord"), [
    "373816693522432001",
    "5763330319",
  ])

  // Der Besitzer in einem weiteren Discord-Kanal → Raum bleibt privat.
  handler(...message("373816693522432001", "discord:channel:1497566179114352730", "1497566179114352730", { platform: "discord" }))
  assert.strictEqual(isRoomShared("chat:1497566179114352730"), false)
  assert.strictEqual(getRoomState("chat:1497566179114352730")?.reason, "owner")

  // Zweite Discord-Nachricht → kein drittes Schreiben.
  const before = readFileSync(ownerFilePath(), "utf8")
  handler(...message("373816693522432001", "discord:group:456", "456", { platform: "discord" }))
  assert.strictEqual(readFileSync(ownerFilePath(), "utf8"), before, "kein erneutes Schreiben")
})

// ── l) version:1 wird verworfen, nicht global angewendet ────────────────────

await t("l) version:1-Datei → verworfen, einmal vermerkt, danach neu gelernt", () => {
  const dir = freshState()
  mkdirSync(join(dir, "nexus"), { recursive: true })
  writeFileSync(
    ownerFilePath(),
    JSON.stringify({ version: 1, ownerUserId: "777", learnedAt: Date.now(), reason: "first-human" }),
  )

  // Nicht angewendet: die alte ID gilt auf KEINER Plattform.
  assert.strictEqual(readLearnedOwner(), null)
  assert.deepStrictEqual(resolveOwnerIds({ ownerUserIds: [] }), [], "v1 darf nicht global gelten")
  assert.deepStrictEqual(resolveOwnerIds({ ownerUserIds: [] }, "telegram"), [])
  assert.strictEqual(warns.length, 0, "eine v1-Datei ist kein Lesefehler")

  const notes = () => infos.filter((msg) => msg.includes("version-1 owner"))
  assert.strictEqual(notes().length, 1, "genau ein Vermerk")
  assert.ok(!notes()[0].includes("777"), "der Vermerk darf die alte ID nicht wiedergeben")

  // Zweiter Leseversuch vermerkt nicht erneut.
  readLearnedOwner()
  assert.strictEqual(notes().length, 1)

  // Kein Absturz — die naechste Nachricht lernt die Plattform neu.
  const handler = handlerFor()
  handler(...message("4711", "discord:group:456", "456"))
  assert.deepStrictEqual(ownerFile().owners, { discord: "4711" })
  assert.strictEqual(ownerFile().version, 2)
  assert.strictEqual(isRoomShared("chat:456"), false, "der neu gelernte Besitzer bleibt privat")
})

// ── m) Fremder auf einer bereits gelernten Plattform ───────────────────────

await t("m) Fremder auf gelernter Plattform → nichts gelernt, Raum geteilt", () => {
  freshState()
  const handler = handlerFor()
  handler(...message("5763330319", "agent:main:main", "tg-1", { platform: "telegram" }))
  const before = readFileSync(ownerFilePath(), "utf8")

  // Ein anderer Mensch auf derselben Plattform ist ein Fremder.
  handler(...message("1111", "telegram:group:-100999", "-100999", { platform: "telegram" }))
  assert.strictEqual(readFileSync(ownerFilePath(), "utf8"), before, "keine zweite ID auf derselben Plattform")
  assert.strictEqual(isRoomShared("chat:-100999"), true, "fremder Mensch → Raum geteilt")
  assert.strictEqual(getRoomState("chat:-100999")?.reason, "foreign-author")

  // Der Besitzer selbst markiert seinen Raum weiterhin nicht.
  handler(...message("5763330319", "telegram:group:-100888", "-100888", { platform: "telegram" }))
  assert.strictEqual(isRoomShared("chat:-100888"), false)
  assert.strictEqual(getRoomState("chat:-100888")?.reason, "owner")
})

// ── n) ohne Plattform wird nichts gelernt ──────────────────────────────────

await t("n) Nachricht ohne Plattform → nichts gelernt (fail-closed)", () => {
  const dir = freshState()
  const handler = handlerFor()
  // Kein channelId im Kontext: die ID laesst sich keiner Plattform zuordnen.
  handler({ senderId: "4711" }, { sessionKey: "agent:main:main", chatId: "555" })

  assert.strictEqual(ownerFileExists(), false, "ohne Plattform darf keine Datei entstehen")
  assert.deepStrictEqual(resolveOwnerIds({ ownerUserIds: [] }), [], "kein Eigentuemer bekannt")
  assert.strictEqual(isRoomShared("chat:555"), true, "ohne Besitzer gilt jeder Mensch als fremd")

  const direct = learnOwnerFromMessage({ senderId: "4711", isBot: false })
  assert.strictEqual(direct.learned, false)
  assert.strictEqual(direct.reason, "no-platform")
  assert.strictEqual(ownerFileExists(), false, `keine Datei in ${dir}`)
})

process.env.HOME = REAL_HOME
if (REAL_STATE === undefined) delete process.env.OPENCLAW_STATE_DIR
else process.env.OPENCLAW_STATE_DIR = REAL_STATE
rmSync(SANDBOX, { recursive: true, force: true })
process.exitCode = failed ? 1 : 0
