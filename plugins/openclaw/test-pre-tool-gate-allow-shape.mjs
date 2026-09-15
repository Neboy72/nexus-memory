/**
 * Regressionstest: before_tool_call Return-Shape (H135, Welle 13)
 *
 * Der letzte Zweig von buildPreToolGateHandler gab
 * `{ params, _nexusRecallContext }` zurück. before_tool_call kennt aber nur
 * zwei gültige Shapes: `{}` (allow) und `{ block, blockReason }` (deny) — das
 * Framework injizierte weder params noch den Kontext, und das params-Echo
 * konnte Args korrumpieren. Jetzt: allow → `{}`; der Kontext bleibt auf den
 * Block-Pfaden (blockReason) erhalten.
 */
import assert from "node:assert"
import { existsSync } from "node:fs"
import { buildPreToolGateHandler } from "./hooks/pre-tool-gate.ts"

const PLAN_LOCK_PATH = "/tmp/miosha-think-gate.lock"

let failed = 0
const t = (name, fn) =>
  Promise.resolve()
    .then(fn)
    .then(() => console.log("PASS ", name))
    .catch((e) => {
      failed++
      console.log("FAIL ", name, "—", e.message)
    })

function makeHandler() {
  const state = { embed: 0 }
  const handler = buildPreToolGateHandler(
    { embed: async () => { state.embed++; return [0.1, 0.2] } },
    {
      search: async () => [
        { id: "m1", text: "relevantes Memory", score: 0.9, category: "fact" },
      ],
    },
    { accessLevel: "private" },
  )
  return { handler, state }
}

// ── Allow-Pfad: Kontext wird berechnet, aber NICHT als params-Echo geliefert ─

await t("allow-Pfad gibt {} zurück — kein params-Echo, kein _nexusRecallContext", async () => {
  const { handler, state } = makeHandler()
  // "browser" ist kein Plan-Trigger, "qdrant" triggert Pre-Action-Recall.
  const res = await handler({ toolName: "browser", params: { command: "qdrant status" } }, {})
  assert.deepStrictEqual(res, {}, `allow muss genau {} liefern, bekam: ${JSON.stringify(res)}`)
  assert.ok(!("params" in res), "params darf nicht zurückgegeben werden")
  assert.ok(!("_nexusRecallContext" in res), "_nexusRecallContext darf nicht zurückgegeben werden")
  assert.strictEqual(state.embed, 1, "Recall-Kontext wurde berechnet (embed lief)")
})

await t("allow-Pfad ohne Recall-Keyword gibt ebenfalls {} zurück", async () => {
  const { handler } = makeHandler()
  const res = await handler({ toolName: "read", params: { path: "/tmp/x" } }, {})
  assert.deepStrictEqual(res, {})
})

// ── Block-Pfad: Kontext bleibt in blockReason ───────────────────────────────

await t("block-Pfad (Plan-Zwang) enthält recallContext in blockReason", async () => {
  if (existsSync(PLAN_LOCK_PATH)) {
    // Aktiver Plan-Lock der Maschine → hasValidPlan() wäre true und der
    // Plan-Zwang-Block würde nicht greifen. Der Block-Pfad ist dann nicht
    // bestimmbar; wir überspringen NUR diese Zusicherung (Umgebungszustand,
    // kein Test-Schwächung im Normalfall: Lock ist normalerweise abwesend).
    console.log("SKIP  block-Pfad — aktiver Plan-Lock", PLAN_LOCK_PATH)
    return
  }
  const { handler, state } = makeHandler()
  const res = await handler({ toolName: "exec", params: { command: "rm -rf /tmp/irgendwas" } }, {})
  assert.strictEqual(res.block, true, "ohne Plan-Lock muss geblockt werden")
  assert.match(res.blockReason, /VORBEREITUNG-GATE/, "Plan-Begründung erwartet")
  assert.match(
    res.blockReason,
    /VORBEREITUNG-GATE \(pre-action recall\)/,
    "recallContext muss weiterhin im blockReason hängen",
  )
  assert.strictEqual(state.embed, 1)
})

await t("Guardrail-Block liefert {block, blockReason} (Shape unverändert)", async () => {
  const { handler } = makeHandler()
  const res = await handler({ toolName: "exec", params: { command: "rm -rf /Users/miosha/.hermes" } }, {})
  assert.strictEqual(res.block, true)
  assert.ok(typeof res.blockReason === "string" && res.blockReason.length > 0)
  assert.deepStrictEqual(Object.keys(res).sort(), ["block", "blockReason"])
})

process.exit(failed ? 1 : 0)
