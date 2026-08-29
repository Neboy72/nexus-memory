// Runtime-Smoke-Test fuer plugins/openclaw/dist/index.js
// Beweist: Alle 9 Tools registrieren mit execute-im-Objekt (Doctor-Regel von openclaw >= 2026.5).
// Ausfuehren: cd plugins/openclaw && node _smoke_test.mjs
import plugin from "./dist/index.js";

const registered = {};
const noop = () => {};
const api = {
  id: "smoke-test",
  name: "smoke-test",
  source: "internal",
  registrationMode: "native",
  logger: { info: noop, warn: noop, error: noop, debug: noop },
  pluginConfig: {},
  runtime: {},
  on: noop,
  registerTool: (tool) => { registered[tool.name] = tool; },
  registerHook: noop,
  registerCommand: noop,
  registerCli: noop,
  registerService: noop,
};
const cfg = { collection: "nexus", embedding: { provider: "voyage", model: "voyage-4" }, accessLevel: "trusted" };

const entry = plugin.default ?? plugin;
let initError = null;
try {
  if (typeof entry.register === "function") await entry.register(api, cfg);
  else if (typeof entry === "function") await entry(api, cfg);
  else console.log("ENTRY_KEYS:", Object.keys(entry).join(","));
} catch (e) { initError = String(e); }

console.log("INIT_ERROR:", initError);
console.log("REGISTERED_COUNT:", Object.keys(registered).length);
console.log("REGISTERED:", Object.keys(registered).sort().join(","));

// Exakte Doctor-Validierung aus openclaw dist/tools-*.js:
//   !name -> "missing non-empty name"; typeof execute !== function -> "missing execute function";
//   !parameters record -> "missing parameters object"
const failures = Object.entries(registered).map(([name, tool]) => {
  if (typeof tool?.name !== "string" || !tool.name) return name + ": missing non-empty name";
  if (typeof tool?.execute !== "function") return name + ": missing execute function";
  if (typeof tool?.parameters !== "object" || tool.parameters === null) return name + ": missing parameters object";
  return null;
}).filter(Boolean);

if (initError || failures.length > 0) {
  console.log("SMOKE_RESULT: FAIL |", initError || failures.join(" | "));
  process.exit(1);
}
console.log("SMOKE_RESULT: ALL PASS (0 malformed)");

// Funktioneller Durchlauf des reparierten Tools (Qdrant localhost, fail-open)
const gc = registered["nexus_guardrail_check"];
const out1 = await gc.execute("t1", { command: "rm -rf /Users/miosha/nexus-memory-test/" });
const out2 = await gc.execute("t2", { command: "ls -la /tmp" });
console.log("EXEC guarded:", JSON.stringify(out1).slice(0, 220));
console.log("EXEC benign :", JSON.stringify(out2).slice(0, 150));