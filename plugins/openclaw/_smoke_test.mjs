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
  // Nr 364 (W21): register() wirft jetzt, wenn WEDER die native Memory-Capability
  // NOCH eine Fallback-API etwas registriert. Der Fake-Host muss die Capability
  // also annehmen, sonst bricht register() vor den Tool-Registrierungen ab.
  registerMemoryCapability: noop,
};
const cfg = { collection: "nexus", embedding: { provider: "voyage", model: "voyage-4" }, accessLevel: "trusted" };

const entry = plugin.default ?? plugin;
// W34-4: register(api) reads the config from api.pluginConfig (single-arg
// contract, matches index.ts). The old second-argument call left api.pluginConfig
// unset, so register() ran against defaults instead of the smoke cfg.
api.pluginConfig = cfg;
let initError = null;
try {
  if (typeof entry.register === "function") await entry.register(api);
  else if (typeof entry === "function") await entry(api);
  else {
    initError = `no register()/callable entry — keys: ${Object.keys(entry ?? {}).join(",")}`;
  }
} catch (e) { initError = String(e); }

console.log("INIT_ERROR:", initError);
const registeredCount = Object.keys(registered).length;
console.log("REGISTERED_COUNT:", registeredCount);
console.log("REGISTERED:", Object.keys(registered).sort().join(","));

// The manifest contract promises the tool set — read it from openclaw.plugin.json
// (contracts.tools) instead of mirroring a magic number that can drift.
import { readFileSync as _rf } from "node:fs";
const EXPECTED_TOOLS = (() => {
  try {
    const manifest = JSON.parse(_rf(new URL("./openclaw.plugin.json", import.meta.url), "utf8"));
    const names = (manifest.contracts?.tools ?? []).map((t) => (typeof t === "string" ? t : t?.name)).filter(Boolean);
    return names.length > 0 ? names : null;
  } catch { return null; }
})();
const EXPECTED_TOOL_COUNT = EXPECTED_TOOLS?.length ?? 9;
if (registeredCount !== EXPECTED_TOOL_COUNT) {
  console.log(`SMOKE_RESULT: FAIL | ${registeredCount} tools registered, manifest expects exactly ${EXPECTED_TOOL_COUNT}${EXPECTED_TOOLS ? "" : " (manifest unreadable — fallback)"}`);
  process.exit(1);
}
if (EXPECTED_TOOLS) {
  const missing = EXPECTED_TOOLS.filter((n) => !registered[n]);
  if (missing.length > 0) {
    console.log(`SMOKE_RESULT: FAIL | manifest tools missing: ${missing.join(", ")}`);
    process.exit(1);
  }
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
    if (!text && !(out && typeof out === "object" && ("verdict" in out || "blocked" in out))) {
      return { verdict: undefined, blocked: true, reason: "unparseable tool output shape (fail-closed)" };
    }
    const parsed = text ? JSON.parse(text) : (out ?? {});
    const verdict = parsed.verdict ?? (parsed.blocked === true ? "block" : undefined);
    if (verdict === undefined) {
      return { verdict: undefined, blocked: true, reason: "no verdict in tool output (fail-closed)" };
    }
    return { verdict, blocked: parsed.blocked === true || verdict === "block", reason: parsed.reason };
  } catch {
    return { verdict: undefined, blocked: true, reason: "tool output not parseable as JSON (fail-closed)" };
  }
}

let out1, out2;
try {
  out1 = await gc.execute("t1", { command: "rm -rf /tmp/nexus-smoke-nonexistent/" });
  out2 = await gc.execute("t2", { command: "ls -la /tmp" });
} catch (e) {
  console.log(`SMOKE_RESULT: FAIL | guardrail execute threw (expected fail-open/fail-closed verdict): ${e?.stack ?? e}`);
  process.exit(1);
}
console.log("EXEC guarded:", JSON.stringify(out1).slice(0, 220));
console.log("EXEC benign :", JSON.stringify(out2).slice(0, 150));

// Design seit v0.5.0 (416d6fc): Block NUR bei passender Protection-Rule auf das Ziel,
// sonst fail-open ("unprotected target"). Der Block-Pfad (geschützte Ziele, Store
// nicht erreichbar -> fail-closed, Re-Check) ist in test-guardrail-*.mjs mit Mocks
// abgedeckt und braucht hier nicht reproduziert zu werden. WICHTIG (W39): Diese
// Erwartung gilt NUR bei erreichbarem, korrekt geseedetem Qdrant (deterministisch).
// Ohne Qdrant fail-closed der rm-Call -> verdict 'block' — das ist PRODUKTIONS-
// korrekt, aber environmental, nicht ein Produktionsfehler. Deshalb: bei fail-closed-
// Muster (blocked mit 'unavailable'/'fail-closed'-Reason) SMOKE als SKIP-Durchlauf
// werten statt FAIL, damit CI-Umgebungen ohne Store nicht rot schlagen.
const res1 = guardrailVerdict(out1);
const res2 = guardrailVerdict(out2);
if (res1.verdict !== "allow" || !String(res1.reason ?? "").includes("unprotected target")) {
  console.log(`SMOKE_RESULT: FAIL | ungeschütztes rm -rf muss fail-open allow mit 'unprotected target' sein (verdict=${res1.verdict}, reason=${res1.reason ?? "?"})`);
  process.exit(1);
}
if (res2.blocked) {
  console.log(`SMOKE_RESULT: FAIL | benign 'ls -la /tmp' was blocked (verdict=${res2.verdict})`);
  process.exit(1);
}
console.log(`SMOKE_RESULT: FUNCTIONAL PASS (guarded=${res1.verdict}, benign=${res2.verdict})`);