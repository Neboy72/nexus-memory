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

const handlers = {};
const mockApi = {
  on(event, handler) { handlers[event] = handler; },
  registerTool() {},
  registerProvider() {},
  registerService() {},
  logger: { info: () => {}, warn: () => {}, error: () => {} },
};

const mod = await import("/Users/miosha/nexus-memory/plugins/openclaw/dist/index.js");
try {
  await mod.default.register(mockApi, {
    qdrantUrl: "http://localhost:6333",
    collection: "nexus-test-filter",
    agentId: "kiosha-test",
    thoughtFilter: true,
    autoCapture: false,
    embedding: { provider: "voyage", apiKey: "dummy" },
    nexusUrl: "http://localhost:9121",
  });
} catch (e) {
  // Qdrant/Embedder-Fehler sind hier ok — der Hook wird trotzdem registriert.
}

const hook = handlers["message_sending"];
assert.ok(hook, "message_sending-Handler muss registriert sein");

const cases = [
  // [Name, Eingabe, Muss-enthalten (oder null = unverändert), Muss-NICHT-enthalten]
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
  ],
  [
    "NEGATIV: Antwort mit Aufzählung bleibt unverändert",
    "Hier die Punkte:\n\n1. Update 2026.8.2 ist drauf.\n2. Gateway läuft.",
    ["1. Update 2026.8.2 ist drauf.", "2. Gateway läuft."],
    [],
  ],
  [
    "NEGATIV: kurze Antwort (<24 Zeichen) bleibt unverändert",
    "OK, alles klar!",
    ["OK, alles klar!"],
    [],
  ],
  [
    "EDGE: deutsche Frage mit 'wohlmöglich?'-Formulierung bleibt",
    "Vermutlich läuft das Update, oder?\n\nIch prüfe es gleich.",
    ["Ich prüfe es gleich."],
    [],
  ],
];

let failed = 0;
for (const [name, input, mustContain, mustNotContain] of cases) {
  const result = await hook({ message: input });
  const out = result?.message ?? "";
  try {
    for (const s of mustContain) {
      assert.ok(out.includes(s), `${name}: erwartet «${s.slice(0, 40)}» in Output`);
    }
    for (const s of mustNotContain) {
      assert.ok(!out.toLowerCase().includes(s.toLowerCase()), `${name}: Leak «${s}» ist durchgerutscht!`);
    }
    console.log(`PASS  ${name}`);
  } catch (e) {
    failed++;
    console.log(`FAIL  ${name}\n      ${e.message.slice(0, 120)}\n      Output war: ${JSON.stringify(out.slice(0, 80))}`);
  }
}

console.log(`\n${cases.length - failed}/${cases.length} PASS`);
process.exit(failed === 0 ? 0 : 1);