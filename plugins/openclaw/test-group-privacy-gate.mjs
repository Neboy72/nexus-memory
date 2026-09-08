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

const handlers = {};
const capturedUpserts = [];
const searches = [];

const fakeQdrant = {
  async upsert(id, vector, payload) { capturedUpserts.push({ id, payload }); },
  async search(vec, limit, accessLevel) {
    searches.push({ accessLevel });
    return [{ id: "x", text: "pub", score: 1, access_level: "public", created_at: "" }];
  },
  async scrollPoint() { return null; },
};
const fakeEmbedder = { async embed() { return [0.1, 0.2]; } };
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

const mod = await import("/Users/miosha/nexus-memory/plugins/openclaw/dist/index.js");

// Fetch-Mock: Voyage (Fake-Vektoren) + Qdrant (Recorder) — keine Netzwerk-/API-Abhängigkeit
const realFetch = globalThis.fetch;
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
      // private sieht ALLES → kein filter-Feld → "private" implizit
      let lvl = "private-no-filter";
      try {
        const body = JSON.parse((opts && opts.body) || "{}");
        const any = body.filter.must[0].match.any;
        lvl = Array.isArray(any) ? any.join("|") : String(any);
      } catch {}
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
  return realFetch(url, opts);
};
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
  // Qdrant/Embedder-Fehler ok — Hooks sind trotzdem registriert
}

let failed = 0;
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
  // private sieht ALLES → Qdrant-Client schickt bewusst KEINEN Filter → implizit "private"
  assert.ok(
    searches[0].accessLevel.startsWith("private"),
    "DM-Recall nutzt cfg.accessLevel (private = kein Filter, sieht alles) — got: " + searches[0].accessLevel,
  );
});

process.exit(failed ? 1 : 0);