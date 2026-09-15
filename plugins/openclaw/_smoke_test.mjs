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
const registeredCount = Object.keys(registered).length;
console.log("REGISTERED_COUNT:", registeredCount);
console.log("REGISTERED:", Object.keys(registered).sort().join(","));

// The manifest contract promises 9 tools — a lower count means a tool
// silently failed to register (e.g. missing dep) and the run is invalid.
const EXPECTED_TOOL_COUNT = 9;
if (registeredCount < EXPECTED_TOOL_COUNT) {
  console.log(`SMOKE_RESULT: FAIL | only ${registeredCount} tools registered, expected >= ${EXPECTED_TOOL_COUNT}`);
  process.exit(1);
}

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
if (!gc || typeof gc.execute !== "function") {
  console.log("SMOKE_RESULT: FAIL | tool 'nexus_guardrail_check' not registered or has no execute()");
  process.exit(1);
}

// Tools return {content:[{type:"text",text: JSON.stringify(result)}]}; normalise
// that to the guardrail verdict so the assertions below test something real.
function guardrailVerdict(out) {
  try {
    const text = out?.content?.[0]?.text;
    const parsed = text ? JSON.parse(text) : (out ?? {});
    const verdict = parsed.verdict ?? (parsed.blocked === true ? "block" : undefined);
    return { verdict, blocked: parsed.blocked === true || verdict === "block" };
  } catch {
    return { verdict: undefined, blocked: false };
  }
}

const out1 = await gc.execute("t1", { command: "rm -rf /tmp/nexus-smoke-nonexistent/" });
const out2 = await gc.execute("t2", { command: "ls -la /tmp" });
console.log("EXEC guarded:", JSON.stringify(out1).slice(0, 220));
console.log("EXEC benign :", JSON.stringify(out2).slice(0, 150));

const res1 = guardrailVerdict(out1);
const res2 = guardrailVerdict(out2);
if (!res1.blocked) {
  console.log(`SMOKE_RESULT: FAIL | destructive 'rm -rf' was not blocked (verdict=${res1.verdict})`);
  process.exit(1);
}
if (res2.blocked) {
  console.log(`SMOKE_RESULT: FAIL | benign 'ls -la /tmp' was blocked (verdict=${res2.verdict})`);
  process.exit(1);
}
console.log(`SMOKE_RESULT: FUNCTIONAL PASS (guarded=${res1.verdict}, benign=${res2.verdict})`);