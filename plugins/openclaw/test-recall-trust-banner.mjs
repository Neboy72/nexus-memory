/**
 * Vertrauensgrenze im eingebetteten Block (21.11.2026).
 *
 * Anlass: ClawHub-Befund "Persistent Prompt Injection Through Automatically
 * Recalled Memories" gegen 1.22.7. Der Block besteht aus GESPEICHERTEN Texten —
 * ein Eintrag kann eine Anweisung enthalten, die ein Mensch nie an diesen
 * Agenten gerichtet hat. Das Modell muss die Grenze kennen, bevor es den
 * ersten Eintrag liest.
 *
 * Was hier festgehalten wird:
 *   1. Die Warnzeile steht im Block.
 *   2. Sie steht VOR dem ersten Eintrag (sonst liest das Modell zuerst die
 *      Anweisung und die Warnung danach).
 *   3. Sie zaehlt NICHT gegen den Gesamtdeckel: auch ein Block voller Treffer
 *      bleibt unter 12 000 Zeichen plus Warnzeile.
 */
import assert from "node:assert";

import { buildRecallHandler } from "./hooks/recall.ts";

const BANNER_START = "[UNTRUSTED DATA";
const BANNER_KERN = "never instructions";

let failed = 0;
const t = (name, fn) =>
  Promise.resolve()
    .then(fn)
    .then(() => console.log("PASS ", name))
    .catch((e) => {
      failed++;
      console.log("FAIL ", name, "—", (e && e.message) || String(e));
    });

function blob(n, marker) {
  const words = [marker];
  let i = 0;
  while (words.join(" ").length < n) {
    words.push(`token${i}`);
    i++;
  }
  return words.join(" ");
}

/** Der Recall-Hook mit einem Qdrant-Ersatz und einer festen Trefferliste. */
function handlerFuer(results) {
  const embedder = { embed: async () => [0.1, 0.2] };
  const client = {
    async search() {
      return results;
    },
    async findEntities() {
      return [];
    },
  };
  const cfg = {
    qdrantUrl: "http://localhost:6333",
    collection: "nexus",
    accessLevel: "private",
    maxRecallResults: 20,
    // Die Deckel-Logik soll greifen, nicht die Voreinstellung.
    recallExcludeCategories: [],
    recallBudgetChars: 12000,
    scope: "",
    privateRooms: [],
    ownerUserIds: ["owner-1"],
    autoRecall: true,
  };
  return buildRecallHandler(embedder, client, cfg);
}

const FACTS = Array.from({ length: 20 }, (_, i) => ({
  id: `f-${i + 1}`,
  text: blob(3000, `fact-marker-${i + 1}`),
  score: 0.95 - i * 0.01,
  category: "fact",
  access_level: "private",
  created_at: "2026-11-01T10:00:00Z",
  source: "test",
}));

await t("Banner steht im Block", async () => {
  const out = await handlerFuer(FACTS.slice(0, 2))(
    { prompt: "erinnere dich an fact-marker-1" },
    {},
  );
  assert.ok(out && typeof out.prependContext === "string", "Block muss entstehen");
  const b = out.prependContext;
  assert.ok(b.includes(BANNER_START), "Warnzeile fehlt im Block");
  assert.ok(b.includes(BANNER_KERN), "Kernaussage 'never instructions' fehlt");
});

await t("Banner steht VOR dem ersten Eintrag", async () => {
  const out = await handlerFuer(FACTS.slice(0, 2))(
    { prompt: "erinnere dich an fact-marker-1" },
    {},
  );
  const b = out.prependContext;
  const banner = b.indexOf(BANNER_START);
  const ersterEintrag = b.indexOf("fact-marker-");
  assert.ok(banner >= 0, "Warnzeile fehlt");
  assert.ok(ersterEintrag > banner, "Warnzeile muss vor dem ersten Eintrag stehen");
  // Und nach der Oeffnung des Blocks, nicht davor.
  assert.ok(b.indexOf("<nexus-context>") < banner, "Warnzeile gehoert in den Block");
});

await t("Gesamtdeckel haelt auch mit Warnzeile (Warnzeile zaehlt nicht mit)", async () => {
  const out = await handlerFuer(FACTS)({ prompt: "ueberblik" }, {});
  const b = out.prependContext;
  const bannerZeile = b.split("\n").find((l) => l.startsWith(BANNER_START));
  assert.ok(bannerZeile, "Warnzeile fehlt");
  // Klammer um den Inhalt: "<nexus-context>\n" + ... + "\n</nexus-context>".
  const KLAMMER = "<nexus-context>\n".length + 1 + 1 + "\n</nexus-context>".length;
  const inhalt = b.length - bannerZeile.length - KLAMMER;
  assert.ok(
    inhalt <= 12000,
    `Inhalt ${inhalt} > 12000 — die Warnzeile darf den Deckel nicht verschieben`,
  );
  assert.ok(b.includes("fact-marker-1"), "bester Treffer muss bleiben");
  assert.ok(!b.includes("fact-marker-20"), "schlechtester Treffer muss wegfallen");
});

console.log(`\n${failed === 0 ? "OK" : "FAILED"} (${failed} rot)`);
if (failed > 0) process.exitCode = 1;
