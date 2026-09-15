/**
 * Regressions-Test für den thought-filter (v2, 02.09.2026).
 *
 * Prüft den ECHTEN message_sending-Handler aus dem gebauten dist-Bundle
 * (nicht nur Regex-Matching) gegen alle heute verifizierten Leak-Klassen
 * UND gegen Negative-Cases (legitime Antworten müssen unverändert bleiben).
 *
 * Exit 0 = alle Cases PASS, Exit 1 = mindestens ein FAIL.
 */
import fs from "node:fs";
import assert from "node:assert";
import { fileURLToPath } from "node:url";
// H1: Direkt-Import der lokalen Quelle (Node type-stripping, kein Build nötig).
import { buildThoughtFilterHandler } from "./hooks/thought-filter.ts";
import { initLogger } from "./logger.ts";

// T1: Pfad relativ zum Test-File (nicht machine-specific hardcoded) → portabel.
const DIST_ENTRY = fileURLToPath(new URL("./dist/index.js", import.meta.url));
if (!fs.existsSync(DIST_ENTRY)) {
  console.error("dist/index.js fehlt — erst `npm run build` im plugins/openclaw-Verzeichnis ausführen.");
  process.exit(1);
}

// T5: Test-Config gebündelt. Nur Test-Dummy-Werte — KEIN echter API-Key.
// localhost bleibt ok: der Test macht keinen echten Netz-Call (nur Handler-Asserts).
const TEST_BASE_URL = "http://localhost:6333"; // Qdrant-Mock
const TEST_NEXUS_URL = "http://localhost:9121"; // Nexus-API-Mock
const TEST_AGENT_ID = "kiosha-test";
const TEST_API_KEY = "dummy"; // nur Test-Dummy, kein echter Key

const handlers = {};
const handlerLists = {};
const mockApi = {
  on(event, handler) { if (!handlerLists[event]) handlerLists[event] = []; handlerLists[event].push(handler); handlers[event] = handlerLists[event][0]; },
  registerTool() {},
  registerProvider() {},
  registerService() {},
  registerMemoryCapability() {}, // Nr 364
  logger: { info: () => {}, warn: () => {}, error: () => {} },
};

const mod = await import(DIST_ENTRY);
try {
  await mod.default.register(mockApi, {
    qdrantUrl: TEST_BASE_URL,
    collection: "nexus-test-filter",
    agentId: TEST_AGENT_ID,
    thoughtFilter: true,
    autoCapture: false,
    embedding: { provider: "voyage", apiKey: TEST_API_KEY },
    nexusUrl: TEST_NEXUS_URL,
  });
} catch (e) {
  // T2: Register-Fehler NICHT schlucken — die Test-Umgebung ist kaputt und
  // spätere Fehler wären sonst unerklärliche TypeErrors.
  console.error("Plugin-Registrierung fehlgeschlagen — Test-Umgebung kaputt:", e);
  process.exit(1);
}

const hook = handlers["message_sending"];
assert.ok(hook, "message_sending-Handler muss registriert sein");

const cases = [
  // [Name, Eingabe, Muss-enthalten (oder null = unverändert), Muss-NICHT-enthalten, exact?]
  // H168: 5. Feld `exact` = true erzwingt volle Gleichheit out === input.
  // Für NEGATIV-Cases gesetzt, damit ein zu aggressiver Filter, der legitime
  // Teile abschneidet, nicht mehr nur per Substring durchrutscht.
  [
    "LEAK: heutiger Dump (runtime context replay)",
    "The runtime context is just a replay of the conversation.\n\nLet me get my bearings.\n\nWhere I am:\n\nGO erhalten, Patch sitzt. ✅",
    ["GO erhalten, Patch sitzt. ✅"],
    ["runtime context", "bearings", "Where I am"],
  ],
  [
    "LEAK: Kimi 09:38 (last system message)",
    "The last system message was a retry. I need to continue recovery.\n\nAlles grün, ich bin da. 🦊",
    ["Alles grün, ich bin da. 🦊"],
    ["last system message", "I need to continue"],
  ],
  [
    "LEAK: Re-Delivery",
    "Runtime context re-delivery — same message.\n\nKurz: alles läuft. 🦊",
    ["Kurz: alles läuft. 🦊"],
    ["re-delivery"],
  ],
  [
    "LEAK: Critical-honesty-Deklaration",
    "Critical honesty requirement: I must NOT claim success.\n\nErledigt, alles dokumentiert. ✅",
    ["Erledigt, alles dokumentiert. ✅"],
    ["Critical honesty"],
  ],
  [
    "NEGATIV: normale deutsche Antwort bleibt unverändert",
    "Danke Nebo! Ich bin wieder da und alles läuft stabil. 🦊",
    ["Danke Nebo! Ich bin wieder da und alles läuft stabil. 🦊"],
    [],
    true,
  ],
  [
    "NEGATIV: Antwort mit Aufzählung bleibt unverändert",
    "Hier die Punkte:\n\n1. Update 2026.8.2 ist drauf.\n2. Gateway läuft.",
    ["1. Update 2026.8.2 ist drauf.", "2. Gateway läuft."],
    [],
    true,
  ],
  [
    "NEGATIV: kurze Antwort (<24 Zeichen) bleibt unverändert",
    "OK, alles klar!",
    ["OK, alles klar!"],
    [],
    true,
  ],
  [
    "EDGE: deutsche Frage mit 'wohlmöglich?'-Formulierung bleibt",
    "Vermutlich läuft das Update, oder?\n\nIch prüfe es gleich.",
    ["Ich prüfe es gleich."],
    [],
  ],
  // --- Leak-Welle 08.09.2026 (echte Leaks aus dem Telegram-Chat) ---
  [
    "LEAK: Re-Orientierung (very carefully, REAL right now)",
    "Let me very carefully re-orient on what is REAL right now.\n\nThe current message is an internal context wrapper.\n\nReplay-Noise komplett ignoriert — hier ist der frische Pairing-Link.",
    ["Replay-Noise komplett ignoriert — hier ist der frische Pairing-Link."],
    ["re-orient", "internal context wrapper"],
  ],
  [
    "LEAK: Re-Orientierung (CURRENT state, letzte User-Nachricht)",
    "Let me re-orient on the CURRENT state.\n\nThe last message: Nebo's message at 00:37:13: \"gute nacht\"\n\nGute Nacht Nebo! 🦊",
    ["Gute Nacht Nebo! 🦊"],
    ["CURRENT state", "00:37:13"],
  ],
  [
    "LEAK: Cron-Selbstplanung (Release Tracker, 08.09. 08:30)",
    "Let me work through this task. I'm the OpenClaw Release Tracker cron job. Steps:\n\n1. Fetch the Atom feed\n2. Compare with last known release",
    [],
    ["Release Tracker", "Fetch the Atom feed"],
  ],
  // Geschärfter "This message"-Marker: Leak-Formen gefiltert, legitime "This
  // message contains …"-Antworten (z.B. Pairing-Link-Übergabe) bleiben stehen.
  [
    "LEAK: This-message-Contains-Doppelpunkt (Replay-Kontext)",
    "This message contains:\n\n- Replay der alten Nachricht\n\nHier der eigentliche Stand: alles läuft. 🦊",
    ["Hier der eigentliche Stand: alles läuft. 🦊"],
    ["Replay der alten Nachricht"],
  ],
  [
    "NEGATIV: legitime 'This message contains'-Antwort bleibt unverändert",
    "This message contains the pairing link you asked for:\n\nhttps://example.com/pairing",
    ["This message contains the pairing link you asked for:"],
    [],
    true,
  ],
  [
    "LEAK: Cron-Analyse (Let me analyze what I got)",
    "Let me analyze what I got:\n\n**Last known release:** `2026.9.2`",
    [],
    ["Last known release"],
  ],
  [
    "LEAK: Heartbeat-Parse (Memory-Cron)",
    "Let me parse this heartbeat. The scratch context says:\n\n1. Output discipline: If everything OK, reply NO_REPLY",
    [],
    ["parse this heartbeat", "Output discipline"],
  ],
  [
    "LEAK: Daily-memory-Zeile (Memory-Cron 06:23)",
    "1. Daily memory file exists (2026-09-08.md, last modified 00:24). Need to check if anything else is missing.",
    [],
    ["Daily memory file exists"],
  ],
  [
    "NEGATIV: User zitiert 're-orient' in legitimer Frage bleibt",
    "Was meinst du mit Re-Orientierung genau?\n\nIch erkläre es dir gern.",
    ["Ich erkläre es dir gern."],
    [],
    true,
  ],
  [
    "NEGATIV: Stufenplan auf Deutsch bleibt (steps-Marker nur am Blockanfang)",
    "So gehen wir vor:\n\n1. Erst prüfen.\n2. Dann bauen.",
    ["1. Erst prüfen.", "2. Dann bauen."],
    [],
    true,
  ],
];

let failed = 0;
for (const [name, input, mustContain, mustNotContain, exact] of cases) {
  const result = await hook({ message: input });
  const out = result?.message ?? "";
  try {
    for (const s of mustContain) {
      assert.ok(out.includes(s), `${name}: erwartet «${s.slice(0, 40)}» in Output`);
    }
    for (const s of mustNotContain) {
      assert.ok(!out.toLowerCase().includes(s.toLowerCase()), `${name}: Leak «${s}» ist durchgerutscht!`);
    }
    // H168: NEGATIV-Cases müssen UNVERÄNDERT durchgehen — sonst würde ein zu
    // aggressiver Filter, der legitime Teile abschneidet, nur per Substring passieren.
    if (exact) {
      assert.strictEqual(out, input, `${name}: Output weicht vom Input ab (Filter zu aggressiv)`);
    }
    console.log(`PASS  ${name}`);
  } catch (e) {
    failed++;
    console.log(`FAIL  ${name}\n      ${e.message.slice(0, 120)}\n      Output war: ${JSON.stringify(out.slice(0, 80))}`);
  }
}

// ── H1: Fail-open darf die Message nicht droppen (lokale Quelle) ──
// Payload liegt in ctx.content (ctx.message === undefined) und der Gate-Crash
// wird über einen werfenden Logger erzwungen. Der catch MUSS den Original-Text
// zurückgeben — vor dem Fix kam { message: undefined } zurück (Message gedroppt).
try {
  initLogger(
    { info() {}, warn() { throw new Error("simulierter Gate-Crash") }, error() {}, debug() {} },
    false,
  );
  const localHook = buildThoughtFilterHandler();
  const leak = "The runtime context is just a replay.\n\nAlles läuft stabil und grün. 🦊";
  const r = await localHook({ content: leak });
  assert.strictEqual(r?.message, leak, "Fail-open muss den Original-Text liefern, nicht undefined");
  console.log("PASS  H1: Fail-open bei Gate-Crash liefert Original-Text (kein Drop)");
} catch (e) {
  failed++;
  console.log(`FAIL  H1: Fail-open — ${e.message}`);
}
initLogger({ info() {}, warn() {}, error() {}, debug() {} }, false); // Backend zurücksetzen

console.log(`\n${cases.length - failed}/${cases.length} PASS`);
process.exit(failed === 0 ? 0 : 1);