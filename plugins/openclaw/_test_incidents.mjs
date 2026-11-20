// Vorfall-Meldung des OpenClaw-Plugins (0.22.62) — offline, ohne Dienst.
//
//   npm run build && node _test_incidents.mjs
//
// Getestet wird der gebaute Bundle gegen einen gefälschten Host und ein
// gefälschtes `fetch`. Beweisziel: ein gescheiterter Werkzeugaufruf landet
// genau einmal bei POST /events, ein erfolgreicher gar nicht, und der
// Schalter schaltet wirklich ab.
import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import os from "node:os";
import path from "node:path";

const dataDir = mkdtempSync(path.join(os.tmpdir(), "nexus-incidents-"));
process.env.NEXUS_DATA_DIR = dataDir;

const calls = [];
const jsonResponse = (payload, status = 200) => ({
  ok: status >= 200 && status < 300,
  status,
  headers: new Map(),
  json: async () => payload,
  text: async () => JSON.stringify(payload),
});

const originalFetch = globalThis.fetch;
globalThis.fetch = async (url, init = {}) => {
  const target = String(url);
  const body = init.body ? JSON.parse(String(init.body)) : undefined;
  if (target.endsWith("/enroll")) {
    calls.push({ kind: "enroll" });
    return jsonResponse({ key: "test-agent-key" });
  }
  if (target.endsWith("/events")) {
    calls.push({ kind: "events", body, headers: init.headers ?? {} });
    return jsonResponse({ stored: 1, received: 1 });
  }
  return jsonResponse({});
};

const { default: plugin } = await import("./dist/index.js");
const entry = plugin.default ?? plugin;

function startPlugin(pluginConfig) {
  const hooks = new Map();
  const api = {
    id: "incident-test",
    name: "incident-test",
    source: "internal",
    registrationMode: "native",
    logger: { info() {}, warn() {}, error() {}, debug() {} },
    pluginConfig,
    runtime: {},
    on(name, handler) {
      const list = hooks.get(name) ?? [];
      list.push(handler);
      hooks.set(name, list);
    },
    registerTool() {},
    registerHook() {},
    registerCommand() {},
    registerCli() {},
    registerService() {},
    registerMemoryCapability() {},
  };
  return { api, hooks };
}

const baseConfig = {
  collection: "nexus",
  embedding: { provider: "nexus" },
  accessLevel: "private",
  writeGate: { url: "http://127.0.0.1:9122", key: "test-installation-key" },
};

function events() {
  return calls.filter((call) => call.kind === "events");
}

const cases = [];
const reportPath = [];

async function withPlugin(overrides, run) {
  calls.length = 0;
  const { api, hooks } = startPlugin({ ...baseConfig, ...overrides });
  await entry.register(api);
  await run({ api, hooks });
}

cases.push([
  "der Hook wird registriert, wenn der Schalter an ist",
  async () => {
    await withPlugin({}, async ({ hooks }) => {
      assert.ok((hooks.get("after_tool_call") ?? []).length >= 1, "after_tool_call registriert");
    });
  },
]);

cases.push([
  "ein gescheiterter Aufruf geht genau einmal nach /events",
  async () => {
    await withPlugin({}, async ({ hooks }) => {
      const handler = hooks.get("after_tool_call")[0];
      await handler(
        { toolName: "exec", result: { exitCode: 127, output: "bash: timeout: command not found" } },
        { sessionKey: "session-a" },
      );
      const posted = events();
      assert.equal(posted.length, 1, "eine Meldung");
      assert.equal(posted[0].body.tool, "exec");
      assert.equal(posted[0].body.ok, false);
      assert.equal(posted[0].body.session, "session-a");
      assert.ok(posted[0].body.error.includes("command not found"), posted[0].body.error);
      assert.equal(posted[0].headers["x-nexus-service-key"], "test-agent-key", "Agentenschlüssel");
    });
  },
]);

cases.push([
  "ein erfolgreicher Aufruf sendet nichts",
  async () => {
    await withPlugin({}, async ({ hooks }) => {
      const handler = hooks.get("after_tool_call")[0];
      await handler({ toolName: "exec", result: { exitCode: 0, output: "ok" } }, { sessionKey: "s" });
      assert.equal(events().length, 0);
    });
  },
]);

cases.push([
  "ein error-Text zählt auch ohne Ergebnis-Objekt",
  async () => {
    await withPlugin({}, async ({ hooks }) => {
      const handler = hooks.get("after_tool_call")[0];
      await handler({ toolName: "read", error: "ENOENT: no such file or directory" }, { sessionKey: "s" });
      const posted = events();
      assert.equal(posted.length, 1);
      assert.ok(posted[0].body.error.includes("ENOENT"));
    });
  },
]);

cases.push([
  "ohne Werkzeugnamen keine Meldung",
  async () => {
    await withPlugin({}, async ({ hooks }) => {
      const handler = hooks.get("after_tool_call")[0];
      await handler({ result: { isError: true, output: "boom" } }, { sessionKey: "s" });
      assert.equal(events().length, 0);
    });
  },
]);

cases.push([
  "der Schalter aus registriert gar keinen Hook",
  async () => {
    await withPlugin({ reportIncidents: false }, async ({ hooks }) => {
      assert.equal(hooks.get("after_tool_call"), undefined, "kein Hook");
    });
  },
]);

cases.push([
  "eine Sitzung flutet das Journal nicht",
  async () => {
    await withPlugin({}, async ({ hooks }) => {
      const handler = hooks.get("after_tool_call")[0];
      for (let index = 0; index < 55; index += 1) {
        await handler({ toolName: "exec", result: { isError: true, output: `boom ${index}` } }, {
          sessionKey: "loud",
        });
      }
      assert.equal(events().length, 50, "bei 50 ist Schluss");
    });
  },
]);

let failed = 0;
for (const [title, run] of cases) {
  try {
    await run();
    console.log(`ok   ${title}`);
  } catch (error) {
    failed += 1;
    console.log(`FAIL ${title}`);
    console.log(`     ${error.message}`);
  }
}

globalThis.fetch = originalFetch;
rmSync(dataDir, { recursive: true, force: true });

if (failed > 0) {
  console.log(`\n${failed} Fall/Fälle fehlgeschlagen`);
  process.exitCode = 1;
} else if (reportPath.length > 0) {
  console.log(reportPath.join("\n"));
}
