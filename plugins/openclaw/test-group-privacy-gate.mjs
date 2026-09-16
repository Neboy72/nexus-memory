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

// Nr 412: Qdrant-URL + Pfadfragmente zentral, damit pluginConfig und fetch-Mock
// nicht auseinanderdriften (der Mock muss denselben Pfad sehen wie der echte Client).
const QDRANT_BASE = "http://localhost:6333";
const EP_SEARCH = "/points/search";
// Fund W36 (low): EP_QUERY wird vom Plugin aktuell nie produziert (search() und
// searchByVector() POSTen beide .../points/search). Der Arm bleibt als Kontrakt-Wache:
// wenn qdrant-client.ts kuenftig auf /points/query migriert, greift der Mock dort
// bereits und der Test zeigt sofort, ob der Filter-Mittransport noch klappt.
const EP_QUERY = "/points/query";
const EP_POINTS = "/points";

const handlers = {};
const capturedUpserts = [];
const searches = [];

// H164: hier standen zuvor fakeQdrant/fakeEmbedder — sie wurden nie in mockApi
// gewired. register(api) baut QdrantClient/Embedder intern aus cfg und geht über
// fetch, der fetch-Mock unten ist die echte Quelle. Tote Scaffolds entfernt.
const mockApi = {
  on(event, handler) { handlers[event] = handler; },
  registerTool() {}, registerProvider() {}, registerService() {},
  registerMemoryCapability() {}, // Nr 364: register() fail-loud wenn nichts registriert wird
  logger: { info: () => {}, warn: () => {}, error: () => {}, debug: () => {} },
  // register(api) liest die Plugin-Config von api.pluginConfig — nicht als 2. Argument
  pluginConfig: {
    // Nur erlaubte Keys (ALLOWED_KEYS in config.ts): agentId/nexusUrl wären "unknown keys"
    qdrantUrl: QDRANT_BASE,
    collection: "nexus-test-gate",
    autoRecall: true,
    autoCapture: true,
    accessLevel: "private",
    embedding: { provider: "voyage", apiKey: "test" },
  },
};

// Fund W36 (medium): DM-Case erreicht die echte drainQueue() in capture.ts — die
// liest/rewrites ~/.openclaw/workspace/data/capture-retry-queue.jsonl (homedir-basiert,
// kein Test-Override). Isolation: Queue-File in Sandbox umleiten BEVOR dist lädt.
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
const SANDBOX_DIR = mkdtempSync(join(tmpdir(), "nexus-privacy-gate-"));
process.env.NEXUS_CAPTURE_QUEUE_FILE = join(SANDBOX_DIR, "capture-retry-queue.jsonl");

const mod = await import(DIST_ENTRY);

// Fetch-Mock: Voyage (Fake-Vektoren) + Qdrant (Recorder) — keine Netzwerk-/API-Abhängigkeit
const EMBED_RESPONSE = { data: [{ embedding: new Array(1024).fill(0.1) }] }; // Fund W36 (low): einmal gebaut
const originalFetch = globalThis.fetch;
globalThis.fetch = async (url, opts) => {
  const u = typeof url === "string" ? url : url.url;
  if (u.includes("voyageai.com")) {
    return {
      ok: true, status: 200,
      // Nr 379: parseEmbeddingResponse liest resp.text() zuerst — der Mock
      // muss das Response-Contract erfuellen, sonst TypeError statt Embedding.
      text: async () => JSON.stringify(EMBED_RESPONSE),
      json: async () => EMBED_RESPONSE,
    };
  }
  if (u.includes(QDRANT_BASE)) {
    // Fund W36 (medium): Routing nach laengstem/praezisestem Pfad ZUERST —
    // "/points" waere ein Prefix von "/points/search"; Reihenfolge ist Kontrakt.
    if (u.includes(EP_SEARCH) || u.includes(EP_QUERY)) {
      // Filter-Format: { must: [{ key: "access_level", match: { any: [levels] } }] }
      // private sieht ALLES → kein filter-Feld → "no-filter" (legitim, kein Fehler).
      // H163: Ein Parse-Fehler wird als eigener Sentinel "parse-error" markiert,
      // damit er NICHT stillschweigend als legitimes "no-filter" durchläuft.
      let lvl = "no-filter";
      try {
        const body = JSON.parse((opts && opts.body) || "{}");
        // Fund W36 (medium): robust — fehlender/veraenderter Filter-Shape darf nicht
        // als TypeError durchschlagen, sondern als eigener Sentinel sichtbar sein.
        const any = body?.filter?.must?.[0]?.match?.any;
        if (any !== undefined) lvl = Array.isArray(any) ? any.join("|") : String(any);
        else if (body?.filter) lvl = "filter-without-any";
      } catch (e) {
        lvl = e instanceof SyntaxError ? "parse-error" : "extract-error";
      }
      searches.push({ accessLevel: lvl });
      return {
        ok: true, status: 200,
        json: async () => ({ result: [
          { id: "pub-1", score: 0.7, payload: { text: "PUBLIC", access_level: "public" } },
        ] }),
      };
    }
    if (u.includes(EP_POINTS) && opts && opts.method === "PUT") {
      // Fund W36 (low): guarded parse — malformed Body = klarer Testfehler, keine opaque SyntaxError-Kette.
      let body;
      try {
        body = JSON.parse(opts.body ?? "{}");
      } catch (e) {
        throw new Error("Qdrant-Upsert-Body ist kein gueltiges JSON: " + e.message);
      }
      capturedUpserts.push({ body });
      return { ok: true, status: 200, json: async () => ({ result: { status: "ok" } }) };
    }
    return { ok: true, status: 200, json: async () => ({ result: {} }) };
  }
  // Fund W36 (medium): hermetisch — kein Fallthrough zum echten fetch. register()
  // feuert checkForUpdate() gegen api.github.com; ein Fallthrough macht den Test
  // netzwerk-abhaengig und schreibt moeglicherweise Update-State.
  if (u.includes("api.github.com")) {
    return { ok: true, status: 200, json: async () => ({ updateAvailable: false }) };
  }
  throw new Error(`unexpected fetch in test: ${u}`);
};

let failed = 0;
try {
  try {
    await mod.default.register(mockApi);
  } catch (e) {
    // T2: Register-Fehler NICHT schlucken — die Test-Umgebung ist kaputt und
    // spätere Fehler wären sonst unerklärliche TypeErrors.
    console.error("Plugin-Registrierung fehlgeschlagen — Test-Umgebung kaputt:", e);
    globalThis.fetch = originalFetch; // Mock VOR dem Exit zurückgeben
    process.exit(1);
  }

  const t = async (name, fn) => {
  try {
    await Promise.race([
      fn(),
      new Promise((_, rej) => setTimeout(() => rej(new Error("timeout: test hing 10s")), 10000)),
    ]);
    console.log("PASS ", name);
  } catch (e) {
    failed++;
    console.log("FAIL ", name, "—", (e && e.stack) || String(e));
  }
};

  // 1. capture MIT groupId → kein Upsert
  await t("capture group → skip", async () => {
    const before = capturedUpserts.length;
    await handlers["agent_end"](
      { success: true, messages: [{ role: "user", content: "Gruppengeheimnis: Token abc123" }] },
      { trigger: "user", messageProvider: "telegram", groupId: "-1001234" },
    );
    assert.strictEqual(capturedUpserts.length, before, "Gruppen-Turn darf NICHT gespeichert werden");
  });

  // Fund W36 (medium): capture mit LEEREM groupId → fail-closed wie Gruppe (skip)
  await t("capture blank groupId → skip (fail-closed)", async () => {
    const before = capturedUpserts.length;
    await handlers["agent_end"](
      { success: true, messages: [{ role: "user", content: "Leer-Gruppe: Token xyz789" }] },
      { trigger: "user", messageProvider: "telegram", groupId: "" },
    );
    assert.strictEqual(capturedUpserts.length, before, "blank groupId MUSS wie Gruppe behandelt werden (skip)");
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
    // Fund W36 (medium): ALLE Searches pruefen — ein zweiter uncapped Call (graph-boost,
    // Fallback, retry) duerfte nicht durchrutschen.
    for (const s of searches) {
      assert.strictEqual(s.accessLevel, "public", "JEDE Gruppen-Suche MUSS auf public gecappt sein — got: " + s.accessLevel);
    }
  });

  // Nr (W34-Fund): recall mit LEEREM groupId → capped public (fail-closed)
  await t("recall blank groupId → capped public", async () => {
    searches.length = 0;
    await handlers["before_prompt_build"](
      { prompt: "was weißt du über geheimnisse?" },
      { trigger: "user", groupId: "" },
    );
    assert.ok(searches.length > 0, "kein Search-Call — Cap fehlt komplett?");
    for (const s of searches) {
      assert.strictEqual(
        s.accessLevel,
        "public",
        "blank groupId: JEDE Suche muss capped sein — got: " + s.accessLevel,
      );
    }
  });

  // 4. recall DM → cfg-Level (private).
  // Fund W36 (medium): Der Sentinel "filter-without-any" (im Mock, F6-Fix) macht die
  // DM-Assertion jetzt unterscheidend: ein Regression-Filter (leerer must) liefert
  // NICHT mehr "no-filter", sondern den Sentinel und failt hier. "Filter bewusst
  // weggelassen" (private sieht alles) bleibt der einzige Weg zu no-filter.
  await t("recall DM → private", async () => {
    searches.length = 0;
    await handlers["before_prompt_build"](
      { prompt: "was weißt du über mein privates gedächtnis?" },
      { trigger: "user", groupId: null },
    );
    assert.ok(searches.length > 0, "Suche muss stattfinden");
    for (const s of searches) {
      // Fund W36 (medium): auch hier ALLE — keine zweite uncapped Suche durchrutschen lassen.
      assert.strictEqual(
        s.accessLevel,
        "no-filter",
        "DM-Recall (private = kein Filter) muss fuer ALLE Calls gelten — got: " + s.accessLevel,
      );
    }
  });
} finally {
  // T3: Mock IMMER restaurieren.
  globalThis.fetch = originalFetch;
  // Fund W36 (low): exitCode statt process.exit — Hard-Exit kappt gepufferte
  // stdout-Ausgabe (PASS/FAIL) bei gepipestem stdout. Natürliches Ende flushes.
  process.exitCode = failed ? 1 : 0;
  rmSync(SANDBOX_DIR, { recursive: true, force: true });
}
