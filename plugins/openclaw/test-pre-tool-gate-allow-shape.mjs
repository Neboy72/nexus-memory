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
import { existsSync, readFileSync, writeFileSync, unlinkSync, utimesSync } from "node:fs"
import os from "node:os"
import { buildPreToolGateHandler } from "./hooks/pre-tool-gate.ts"

// W39/F5: Pfad aus der Handler-Quelle abgeleitet statt dupliziert — ein Drift
// im Handler (z.B. os.tmpdir()) lässt den Test fail-loud schlagen statt still
// den falschen Zweig zu testen.
const handlerSrc = readFileSync(new URL("./hooks/pre-tool-gate.ts", import.meta.url), "utf8")
const _lockMatch = handlerSrc.match(/const PLAN_LOCK_PATH = "([^"]+)"/)
assert.ok(_lockMatch, "PLAN_LOCK_PATH muss in hooks/pre-tool-gate.ts deklariert sein")
const PLAN_LOCK_PATH = _lockMatch[1]

let failed = 0
const t = (name, fn) =>
  Promise.resolve()
    .then(fn)
    .then(() => console.log("PASS ", name))
    .catch((e) => {
      failed++
      console.log("FAIL ", name, "—", e?.stack ?? String(e))
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

await t("allow-Pfad gibt {} zurück — kein params-Echo, kein _nexusRecallContext, params unverändert", async () => {
  const { handler, state } = makeHandler()
  // "browser" ist kein Plan-Trigger, "qdrant" triggert Pre-Action-Recall.
  const params = { command: "qdrant status" }
  const res = await handler({ toolName: "browser", params }, {})
  assert.deepStrictEqual(res, {}, `allow muss genau {} liefern, bekam: ${JSON.stringify(res)}`)
  // F3+F9 (W39): Header verspricht 'das params-Echo konnte Args korrumpieren' —
  // die Datenintegrität wird jetzt bewiesen (deepStrictEqual deckt Extra-Keys ab,
  // hier zusätzlich: kein in-place-Mutieren des Caller-Objekts).
  assert.deepStrictEqual(params, { command: "qdrant status" }, "params darf nicht mutiert werden")
  assert.ok(state.embed >= 1, "Recall-Kontext wurde berechnet (embed lief) — exakte Count-Kopplung bewusst gelockert (W39/F4)")
})

await t("allow-Pfad ohne Recall-Keyword gibt ebenfalls {} zurück", async () => {
  const { handler } = makeHandler()
  const res = await handler({ toolName: "read", params: { path: "/tmp/x" } }, {})
  assert.deepStrictEqual(res, {})
})

// ── Block-Pfad: Kontext bleibt in blockReason ───────────────────────────────

await t("block-Pfad (Plan-Zwang) enthält recallContext in blockReason", async () => {
  // Deterministischer Zustand statt stiller SKIP: Eine evtl. vorhandene,
  // FRISCHE Maschinen-Lock-Datei würde hasValidPlan()=true liefern und den
  // Plan-Zwang nicht greifen lassen. Wir sichern sie und ersetzen sie durch
  // eine ABGELAUFENE (mtime = vor PLAN_MAX_AGE) mit "plan:"-Content — dann
  // ist hasValidPlan() garantiert false und der Block-Pfad läuft IMMER.
  let savedLock = null
  const hadLock = existsSync(PLAN_LOCK_PATH)
  if (hadLock) {
    savedLock = readFileSync(PLAN_LOCK_PATH, "utf8")
  }
  // W39/F7: Skip-Guard prüft GÜLTIGKEIT (fresh plan: content), nicht nur Existenz —
  // eine alte/stale Lock-Datei würde den Test-Zustand ohnehin nicht verfälschen.
  writeFileSync(PLAN_LOCK_PATH, "plan: stale placeholder (test)\n")
  const twoHoursAgo = Date.now() - 2 * 60 * 60 * 1000
  utimesSync(PLAN_LOCK_PATH, twoHoursAgo / 1000, twoHoursAgo / 1000)
  try {
    await assertBlockPath()
  } finally {
    // Maschinen-Zustand exakt restaurieren (Datei weg ODER Original-Content).
    if (hadLock && savedLock !== null) {
      writeFileSync(PLAN_LOCK_PATH, savedLock)
    } else {
      try { unlinkSync(PLAN_LOCK_PATH) } catch {}
    }
  }
})

async function assertBlockPath() {
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
}

await t("Guardrail-Block liefert {block, blockReason} (Shape unverändert)", async () => {
  const { handler } = makeHandler()
  // Homedir-portabel: PROTECTED_PATHS expandiert ~ gegen os.homedir() —
  // hartcodierte Entwickler-Pfade laufen auf CI/anderen Maschinen ins Leere.
  const res = await handler({ toolName: "exec", params: { command: `rm -rf ${os.homedir()}/.hermes` } }, {})
  assert.strictEqual(res.block, true)
  assert.ok(typeof res.blockReason === "string" && res.blockReason.length > 0)
  assert.deepStrictEqual(Object.keys(res).sort(), ["block", "blockReason"])
})

process.exitCode = failed ? 1 : 0
