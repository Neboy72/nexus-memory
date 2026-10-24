/**
 * Regressionstest: Recall-Einblendung begrenzt pro Sorte und insgesamt.
 *
 * Beweist am ECHTEN gebauten Handler:
 * 1. Ein session-Eintrag mit 50 000 Zeichen wird auf 2500 Zeichen gekuerzt.
 * 2. Ein fact-Eintrag mit 3 000 Zeichen wird auf 1200 Zeichen gekuerzt.
 * 3. Abschneiden an der Wortgrenze, Markierung " [...]" am Ende.
 * 4. Mehrere Eintraege zusammen ueberschreiten den Gesamtdeckel 12 000 nicht.
 * 5. Die schlechtest bewerteten Treffer fallen weg.
 */
import assert from "node:assert";
import { existsSync } from "node:fs";
import { fileURLToPath } from "node:url";

const DIST_ENTRY = fileURLToPath(new URL("./dist/index.js", import.meta.url));
if (!existsSync(DIST_ENTRY)) {
  console.error("dist/index.js fehlt — erst `npm run build` ausfuehren.");
  process.exit(1);
}

const QDRANT_BASE = "http://localhost:6333";
const EP_SEARCH = "/points/search";

const handlers = {};
const searches = [];
let testMode = "default";

function makeWordBlob(n, marker = "") {
  // Deterministische Wortfolge, damit der Test die exakte Kuerzung pruefen kann.
  const words = marker ? [marker] : [];
  let i = 0;
  while (words.join(" ").length < n) {
    words.push(`token${i}`);
    i++;
  }
  return words.join(" ");
}

const SESSION_BLOB = makeWordBlob(50000, "session-marker");
const FACT_BLOBS = Array.from({ length: 10 }, (_, i) =>
  makeWordBlob(3000, `fact-marker-${i + 1}`)
);

const mockApi = {
  on(event, handler) { handlers[event] = handler; },
  registerTool() {}, registerProvider() {}, registerService() {},
  registerMemoryCapability() {},
  logger: { info: () => {}, warn: () => {}, error: () => {}, debug: () => {} },
  pluginConfig: {
    qdrantUrl: QDRANT_BASE,
    collection: "nexus-test-caps",
    autoRecall: true,
    autoCapture: false,
    accessLevel: "private",
    embedding: { provider: "openai", apiKey: "test" },
  },
};

const mod = await import(DIST_ENTRY);

const EMBED_RESPONSE = { data: [{ embedding: new Array(1536).fill(0.1) }] };
const originalFetch = globalThis.fetch;
globalThis.fetch = async (url, opts) => {
  const u = typeof url === "string" ? url : url.url;
  if (u.includes("openai.com")) {
    return {
      ok: true, status: 200,
      text: async () => JSON.stringify(EMBED_RESPONSE),
      json: async () => EMBED_RESPONSE,
    };
  }
  if (u.includes(QDRANT_BASE)) {
    if (u.includes(EP_SEARCH)) {
      searches.push({ url: u });
      if (testMode === "proto") {
        return {
          ok: true, status: 200,
          json: async () => ({
            result: [{
              id: "proto-1",
              score: 0.9,
              payload: {
                text: makeWordBlob(3000, "proto-marker"),
                category: "constructor",
                access_level: "private",
                created_at: new Date().toISOString(),
              },
            }],
          }),
        };
      }
      const baseScore = 0.95;
      const results = [
        {
          id: "session-1",
          score: baseScore,
          payload: {
            text: SESSION_BLOB,
            category: "session",
            access_level: "private",
            created_at: new Date(Date.now() - 1000).toISOString(),
          },
        },
        ...FACT_BLOBS.map((blob, i) => ({
          id: `fact-${i + 1}`,
          score: baseScore - ((i + 1) * 0.02),
          payload: {
            text: blob,
            category: "fact",
            access_level: "private",
            created_at: new Date(Date.now() - 2000 - i * 1000).toISOString(),
          },
        })),
      ];
      return { ok: true, status: 200, json: async () => ({ result: results }) };
    }
    return { ok: true, status: 200, json: async () => ({ result: {} }) };
  }
  if (u.includes("api.github.com")) {
    return { ok: true, status: 200, json: async () => ({ updateAvailable: false }) };
  }
  throw new Error(`unexpected fetch in test: ${u}`);
};

function extractTextPart(line, category) {
  const prefixRe = new RegExp(`^- \\[[^\\]]+\\]\\[${category}\\] `);
  return line.replace(prefixRe, "").replace(/ \[\d+%\]$/, "").trim();
}

let failed = 0;
try {
  try {
    await mod.default.register(mockApi);
  } catch (e) {
    console.error("Plugin-Registrierung fehlgeschlagen:", e);
    globalThis.fetch = originalFetch;
    process.exit(1);
  }

  const t = async (name, fn) => {
    try {
      await Promise.race([
        fn(),
        new Promise((_, rej) => setTimeout(() => rej(new Error("timeout: 10s")), 10000)),
      ]);
      console.log("PASS ", name);
    } catch (e) {
      failed++;
      console.log("FAIL ", name, "—", (e && e.stack) || String(e));
    }
  };

  await t("session 50k -> gekuerzt auf <=2500 mit Wortgrenze + [...]", async () => {
    searches.length = 0;
    const res = await handlers["before_prompt_build"](
      { prompt: "erinnere dich an session-1" },
      { trigger: "user" },
    );
    assert.ok(res && res.prependContext, "prependContext muss gesetzt sein");
    const ctx = res.prependContext;
    assert.ok(ctx.length <= 12000, `Gesamtblock ${ctx.length} > 12000`);
    const sessionLine = ctx.split("\n").find((l) => l.includes("[session]") && l.includes("session-marker"));
    assert.ok(sessionLine, "session-Zeile muss im Block stehen");
    const textPart = extractTextPart(sessionLine, "session");
    assert.ok(textPart.endsWith(" [...]"), `session-Text endet nicht mit Markierung: ${textPart.slice(-20)}`);
    const rawTruncated = textPart.slice(0, -6); // ohne " [...]"
    assert.ok(rawTruncated.length <= 2500, `session-Rumpf ${rawTruncated.length} > 2500`);
    // Wortgrenze: das Zeichen hinter dem Schnitt ist Whitespace (im Original).
    const cutPos = rawTruncated.length;
    assert.ok(/\s/.test(SESSION_BLOB[cutPos]) || rawTruncated.length === 2500,
      `session nicht an Wortgrenze geschnitten, naechstes Zeichen: ${JSON.stringify(SESSION_BLOB[cutPos])}`);
    assert.ok(!ctx.includes(SESSION_BLOB.slice(2600, 2700)),
      "session-Inhalt jenseits der Kuerzung darf nicht im Block stehen");
  });

  await t("fact 3k -> gekuerzt auf <=1200 mit Wortgrenze + [...]", async () => {
    searches.length = 0;
    const res = await handlers["before_prompt_build"](
      { prompt: "erinnere dich an fact-1" },
      { trigger: "user" },
    );
    assert.ok(res && res.prependContext, "prependContext muss gesetzt sein");
    const ctx = res.prependContext;
    const factLine = ctx.split("\n").find((l) => l.includes("[fact]") && l.includes("fact-marker-1"));
    assert.ok(factLine, "fact-Zeile muss im Block stehen");
    const textPart = extractTextPart(factLine, "fact");
    assert.ok(textPart.endsWith(" [...]"), `fact-Text endet nicht mit Markierung: ${textPart.slice(-20)}`);
    const rawTruncated = textPart.slice(0, -6);
    assert.ok(rawTruncated.length <= 1200, `fact-Rumpf ${rawTruncated.length} > 1200`);
    const cutPos = rawTruncated.length;
    assert.ok(/\s/.test(FACT_BLOBS[0][cutPos]) || rawTruncated.length === 1200,
      `fact nicht an Wortgrenze geschnitten, naechstes Zeichen: ${JSON.stringify(FACT_BLOBS[0][cutPos])}`);
  });

  await t("Gesamtdeckel 12000 haelt, schlechteste Treffer fallen weg", async () => {
    searches.length = 0;
    const res = await handlers["before_prompt_build"](
      { prompt: "ueberblik" },
      { trigger: "user" },
    );
    assert.ok(res && res.prependContext, "prependContext muss gesetzt sein");
    const ctx = res.prependContext;
    assert.ok(ctx.length <= 12000, `Gesamtblock ${ctx.length} > 12000`);
    // 10 Facts * 1200 + Session 2500 = 14 500 > 12 000 (inkl. Overhead).
    // Es bleiben Session + die besten Facts uebrig; die schwaechsten fallen weg.
    assert.ok(ctx.includes("session-marker"), "bester session-Treffer muss bleiben");
    assert.ok(ctx.includes("fact-marker-1"), "bester fact-Treffer muss bleiben");
    assert.ok(ctx.includes("fact-marker-2"), "zweitbester fact-Treffer muss bleiben");
    assert.ok(!ctx.includes("fact-marker-10"), "schlechtester Treffer fact-10 muss wegfallen");
    assert.ok(!ctx.includes("fact-marker-9"), "zweitschlechtester Treffer fact-9 muss wegfallen");
  });

  await t("unbekannte Sorte (geerbter Name) kuerzt auf Standard, ohne Text zu verlieren", async () => {
    // Gutachter-Befund 11.10.: CATEGORY_TEXT_CAP[category] traf bei einer Sorte wie
    // "constructor" ein geerbtes Object.prototype-Mitglied — der Deckel wurde dann zu einer
    // Funktion und der Eintrag auf " [...]" zusammengestrichen. Der Eintrag darf NIE leer werden.
    testMode = "proto";
    const res = await handlers["before_prompt_build"](
      { prompt: "erinnere dich an proto-1" },
      { trigger: "user" },
    );
    testMode = "default";
    assert.ok(res && res.prependContext, "prependContext muss gesetzt sein");
    const ctx = res.prependContext;
    const line = ctx.split("\n").find((l) => l.includes("proto-marker"));
    assert.ok(line, "der Eintrag mit unbekannter Sorte muss im Block stehen");
    const textPart = extractTextPart(line, "constructor");
    assert.ok(textPart.includes("proto-marker"),
      `Text darf nicht verloren gehen, war: ${JSON.stringify(textPart.slice(0, 40))}`);
    assert.ok(textPart.endsWith(" [...]"), "Standard-Deckel muss greifen (Markierung fehlt)");
    const rumpf = textPart.slice(0, -6);
    assert.ok(rumpf.length <= 1200, `Rumpf ${rumpf.length} > 1200`);
  });
} finally {
  globalThis.fetch = originalFetch;
  process.exitCode = failed ? 1 : 0;
}
