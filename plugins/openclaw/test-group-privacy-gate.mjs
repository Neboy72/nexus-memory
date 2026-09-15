/**
 * Regressionstest: Group-Privacy-Gate (Astra-R2 critical finding, 08.09.2026)
 *
 * Beweist am ECHTEN gebauten Handler:
 * 1. capture: Turn MIT ctx.groupId wird NICHT gespeichert (skip, kein Qdrant-Call).
 * 2. capture: DM-Turn (kein groupId) speichert wie bisher.
 * 3. recall: MIT groupId → Suche läuft mit access_level="public" (Cap).
 * 4. recall: OHNE groupId → Suche nutzt cfg.accessLevel (private).
 */
import assert from "node:assert";
import { existsSync } from "node:fs";
import { fileURLToPath } from "node:url";

// T1: Pfad relativ zum Test-File (nicht machine-specific hardcoded) → portabel.
const DIST_ENTRY = fileURLToPath(new URL("./dist/index.js", import.meta.url));
if (!existsSync(DIST_ENTRY)) {
  console.error("dist/index.js fehlt — erst `npm run build` im plugins/openclaw-Verzeichnis ausführen.");
  process.exit(1);
}

const handlers = {};
const capturedUpserts = [];
const searches = [];

// H164: hier standen zuvor fakeQdrant/fakeEmbedder — sie wurden nie in mockApi
// gewired. register(api) baut QdrantClient/Embedder intern aus cfg und geht über
// fetch, der fetch-Mock unten ist die echte Quelle. Tote Scaffolds entfernt.
const mockApi = {
  on(event, handler) { handlers[event] = handler; },
  registerTool() {}, registerProvider() {}, registerService() {},
  logger: { info: () => {}, warn: () => {}, error: () => {}, debug: () => {} },
  // register(api) liest die Plugin-Config von api.pluginConfig — nicht als 2. Argument
  pluginConfig: {
    // Nur erlaubte Keys (ALLOWED_KEYS in config.ts): agentId/nexusUrl wären "unknown keys"
    qdrantUrl: "http://localhost:6333",
    collection: "nexus-test-gate",
    autoRecall: true,
    autoCapture: true,
    accessLevel: "private",
    embedding: { provider: "voyage", apiKey: "test" },
  },
};

const mod = await import(DIST_ENTRY);

// Fetch-Mock: Voyage (Fake-Vektoren) + Qdrant (Recorder) — keine Netzwerk-/API-Abhängigkeit
const originalFetch = globalThis.fetch;
globalThis.fetch = async (url, opts) => {
  const u = typeof url === "string" ? url : url.url;
  if (u.includes("voyageai.com")) {
    return {
      ok: true, status: 200,
      json: async () => ({ data: [{ embedding: new Array(1024).fill(0.1) }] }),
    };
  }
  if (u.includes("localhost:6333")) {
    if (u.includes("/points/search") || u.includes("/points/query")) {
      // Filter-Format: { must: [{ key: "access_level", match: { any: [levels] } }] }
      // private sieht ALLES → kein filter-Feld → "no-filter" (legitim, kein Fehler).
      // H163: Ein Parse-Fehler wird als eigener Sentinel "parse-error" markiert,
      // damit er NICHT stillschweigend als legitimes "no-filter" durchläuft.
      let lvl = "no-filter";
      try {
        const body = JSON.parse((opts && opts.body) || "{}");
        if (body.filter) {
          const any = body.filter.must[0].match.any;
          lvl = Array.isArray(any) ? any.join("|") : String(any);
        }
      } catch {
        lvl = "parse-error";
      }
      searches.push({ accessLevel: lvl });
      return {
        ok: true, status: 200,
        json: async () => ({ result: [
          { id: "pub-1", score: 0.7, payload: { text: "PUBLIC", access_level: "public" } },
        ] }),
      };
    }
    if (u.includes("/points") && opts && opts.method === "PUT") {
      capturedUpserts.push({ body: JSON.parse(opts.body) });
      return { ok: true, status: 200, json: async () => ({ result: { status: "ok" } }) };
    }
    return { ok: true, status: 200, json: async () => ({ result: {} }) };
  }
  return originalFetch(url, opts);
};

let failed = 0;
try {
  try {
    await mod.default.register(mockApi, {
      qdrantUrl: "http://localhost:6333",
      collection: "nexus-test-gate",
      agentId: "kiosha-test",
      autoRecall: true,
      autoCapture: true,
      accessLevel: "private",
      embedding: { provider: "voyage", apiKey: "test" },
      nexusUrl: "http://localhost:9121",
    });
  } catch (e) {
    // T2: Register-Fehler NICHT schlucken — die Test-Umgebung ist kaputt und
    // spätere Fehler wären sonst unerklärliche TypeErrors.
    console.error("Plugin-Registrierung fehlgeschlagen — Test-Umgebung kaputt:", e);
    globalThis.fetch = originalFetch; // Mock VOR dem Exit zurückgeben
    process.exit(1);
  }

  const t = (name, fn) => fn().then(() => console.log("PASS ", name)).catch((e) => { failed++; console.log("FAIL ", name, "—", e.message); });

  // 1. capture MIT groupId → kein Upsert
  await t("capture group → skip", async () => {
    const before = capturedUpserts.length;
    await handlers["agent_end"](
      { success: true, messages: [{ role: "user", content: "Gruppengeheimnis: Token abc123" }] },
      { trigger: "user", messageProvider: "telegram", groupId: "-1001234" },
    );
    assert.strictEqual(capturedUpserts.length, before, "Gruppen-Turn darf NICHT gespeichert werden");
  });

  // 2. capture DM → speichert
  await t("capture DM → upsert", async () => {
    const before = capturedUpserts.length;
    await handlers["agent_end"](
      { success: true, messages: [{ role: "user", content: "DM-Nachricht fürs private Gedächtnis" }] },
      { trigger: "user", messageProvider: "telegram", groupId: null },
    );
    assert.ok(capturedUpserts.length > before, "DM-Turn MUSS gespeichert werden");
  });

  // 3. recall MIT groupId → accessLevel=public im Search
  await t("recall group → capped public", async () => {
    searches.length = 0;
    await handlers["before_prompt_build"](
      { prompt: "was weißt du über geheimnisse?" },
      { trigger: "user", groupId: "-1001234" },
    );
    assert.ok(searches.length > 0, "Suche muss stattfinden");
    assert.strictEqual(searches[0].accessLevel, "public", "Gruppen-Recall MUSS auf public gecappt sein");
  });

  // 4. recall DM → cfg-Level (private)
  await t("recall DM → private", async () => {
    searches.length = 0;
    await handlers["before_prompt_build"](
      { prompt: "was weißt du über mein privates gedächtnis?" },
      { trigger: "user", groupId: null },
    );
    assert.ok(searches.length > 0, "Suche muss stattfinden");
    // private sieht ALLES → Qdrant-Client schickt bewusst KEINEN Filter.
    // H163: exakter Sentinel statt startsWith("private"): ein Parse-Fehler
    // erscheint als "parse-error" und fällt hier durch; "private-no-filter"
    // (der alte, zu lockere Treffer) existiert nicht mehr.
    assert.strictEqual(
      searches[0].accessLevel,
      "no-filter",
      "DM-Recall nutzt cfg.accessLevel (private = kein Filter, sieht alles) — got: " + searches[0].accessLevel,
    );
  });
} finally {
  // T3: Mock IMMER restaurieren (läuft vor dem finalen process.exit).
  globalThis.fetch = originalFetch;
}

process.exit(failed ? 1 : 0);
