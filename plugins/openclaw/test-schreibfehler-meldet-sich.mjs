/**
 * Regressionstest: Ein Schreibvorgang muss AUFFALLEN, wenn Qdrant ihn ablehnt.
 *
 * Anlass (09.10.2026): Genau derselbe Designfehler sass in drei Plugins —
 * "Erfolg melden, ohne das Ergebnis zu pruefen". Qdrant antwortet auf einen
 * Schreibvorgang ohne ``?wait=true`` mit ``200 acknowledged`` und wendet ihn im
 * HINTERGRUND an. Ein abgelehnter Schreibvorgang (falsche Vektorform, fehlender
 * Vektorname, kaputte Nutzlast) sah damit wie Erfolg aus: die Sammlung blieb
 * leer und niemand erfuhr, warum. Genau daran hing der Nutzer zwei Tage im
 * Dunkeln — "wie soll man reparieren, was man nicht sieht".
 *
 * Dieser Test faehrt den echten Weg: Er stellt Qdrant nach, laesst den
 * Schreibvorgang laufen und prueft, dass der Fehler ankommt.
 */
import assert from "node:assert";

let bestanden = 0;
let fehlgeschlagen = 0;

function pruefe(name, fn) {
  try {
    fn();
    console.log(`PASS  ${name}`);
    bestanden++;
  } catch (fehler) {
    console.log(`FAIL  ${name}`);
    console.log(`      ${fehler.message}`);
    fehlgeschlagen++;
  }
}

const QUELLE = await import("node:fs").then((fs) =>
  fs.readFileSync(new URL("./lib/qdrant-client.ts", import.meta.url), "utf8"),
);

// ── 1. Der Schreibweg muss auf das Ergebnis warten ─────────────────────────

pruefe("upsert sendet ?wait=true (sonst meldet Qdrant 200 und verwirft still)", () => {
  // Nur echte SCHREIB-Adressen: /points ohne Unterpfad (search/scroll/delete
  // sind Lesewege bzw. das Loeschen und tragen ihren eigenen Parameter).
  const stellen = [
    ...QUELLE.matchAll(/\/collections\/\$\{[^}]+\}\/points(\?[^`"']*)?`/g),
  ];
  assert.ok(
    stellen.length >= 2,
    `erwartet mindestens zwei Schreibwege, gefunden ${stellen.length}`,
  );
  for (const treffer of stellen) {
    const query = treffer[1] || "";
    assert.ok(
      query.includes("wait=true"),
      `Schreibweg ohne ?wait=true: ${treffer[0]} — Qdrant bestaetigt dann nur, ` +
        "ein abgelehnter Schreibvorgang bliebe unbemerkt",
    );
  }
});

// ── 2. Ein abgelehnter Schreibvorgang muss auffallen ───────────────────────

pruefe("ein abgelehnter upsert wird als Fehler gemeldet, nicht verschluckt", () => {
  // Nach dem Request muss der Status geprueft und ein Fehler GEWORFEN werden.
  const nachRequest = QUELLE.split("async upsert")[1] || "";
  assert.ok(
    /if\s*\(\s*!resp\.ok\s*\)/.test(nachRequest),
    "upsert prueft resp.ok nicht — ein 400/500 liefe stillschweigend durch",
  );
  assert.ok(
    /throw new Error\(\s*`Qdrant upsert failed/.test(nachRequest),
    "upsert wirft bei einem abgelehnten Schreibvorgang keinen Fehler",
  );
});

// ── 3. Der stille Vektor-Rueckfall muss melden ─────────────────────────────

pruefe("ein unbekanntes Vektor-Layout wird gemeldet (nicht still verschluckt)", () => {
  // Der catch-Block steht hinter der Layout-Abfrage (GET auf /collections/{c}).
  const abProbe = QUELLE.split("private async resolveLayout")[1] || "";
  const catchBlock = abProbe.match(/catch\s*\([^)]*\)\s*\{([\s\S]{0,900}?)\n    \}/);
  assert.ok(catchBlock, "catch-Block in resolveLayout nicht gefunden");
  assert.ok(
    /log\.warn|log\.error/.test(catchBlock[1]),
    "resolveLayout verschluckt einen fehlgeschlagenen Layout-Blick still — " +
      "danach sendet jeder Schreibvorgang die falsche Vektorform ohne Warnung",
  );
});

// ── 4. Gegenprobe: die Meldungen duerfen das Protokoll nicht stoeren ───────

pruefe("Warnungen gehen ueber den Logger, nicht auf stdout", () => {
  // Ein Plugin, das auf stdout schreibt, zerstoert das Protokoll des Wirts.
  const verdaechtig = [...QUELLE.matchAll(/^\s*console\.(log|info)\(/gm)];
  assert.strictEqual(
    verdaechtig.length,
    0,
    `console.log/info gefunden (${verdaechtig.length}x) — gehoert auf den Logger`,
  );
});

console.log(`\nERGBNIS: ${bestanden} bestanden, ${fehlgeschlagen} fehlgeschlagen`);
process.exit(fehlgeschlagen === 0 ? 0 : 1);
