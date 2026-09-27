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
import { basename, join } from "node:path"
import os from "node:os"
import {
  buildPreToolGateHandler,
  DEFAULT_PLAN_LOCK_PATH,
} from "./hooks/pre-tool-gate.ts"

// 27.09.2026: Der Lock-Pfad ist jetzt Config (`planGate.lockPath`) mit generischem
// Default (`os.tmpdir()`). Der Test benutzt einen EIGENEN Pfad, damit er nie den
// echten Lock einer laufenden Session anfasst oder löscht.
const PLAN_LOCK_PATH = join(os.tmpdir(), "nexus-plan-gate-test.lock")
const PLAN_GATE_ON = { enabled: true, lockPath: PLAN_LOCK_PATH, maxAgeSeconds: 300 }

let failed = 0
const t = (name, fn) =>
  Promise.resolve()
    .then(fn)
    .then(() => console.log("PASS ", name))
    .catch((e) => {
      failed++
      console.log("FAIL ", name, "—", e?.stack ?? String(e))
    })

function makeHandler(planGate = PLAN_GATE_ON) {
  const state = { embed: 0 }
  const handler = buildPreToolGateHandler(
    { embed: async () => { state.embed++; return [0.1, 0.2] } },
    {
      search: async () => [
        { id: "m1", text: "relevantes Memory", score: 0.9, category: "fact" },
      ],
    },
    { accessLevel: "private", planGate },
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
  // 27.09.2026: Meldung nennt jetzt den Vertrag (plan:-Präfix) und den Pfad.
  assert.match(res.blockReason, /PLAN GATE/, "Plan-Begründung erwartet")
  assert.match(res.blockReason, /plan:/, "Vertrag (plan:-Präfix) muss in der Meldung stehen")
  assert.ok(
    res.blockReason.includes(PLAN_LOCK_PATH),
    "Lock-Pfad muss in der Meldung stehen — sonst ist sie nicht handlungsfähig",
  )
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

// ── 27.09.2026: Ebene 3 schaltbar, Ebene 1 unbedingt ────────────────────────

await t("planGate.enabled=false → KEIN Plan-Zwang (Ebene 3 aus)", async () => {
  const { handler } = makeHandler({ enabled: false, maxAgeSeconds: 300 })
  // "openclaw gateway restart" steht in PLAN_REQUIRED_COMMANDS.
  const res = await handler({ toolName: "exec", params: { command: "openclaw gateway restart --safe" } }, {})
  assert.ok(!res || res.block !== true, `ohne planGate darf nicht wegen des Plans geblockt werden: ${JSON.stringify(res)}`)
})

await t("planGate.enabled=false → Guardrails blocken WEITER (Ebene 1 unbedingt)", async () => {
  const { handler } = makeHandler({ enabled: false, maxAgeSeconds: 300 })
  const res = await handler({ toolName: "exec", params: { command: `rm -rf ${os.homedir()}/.hermes` } }, {})
  assert.strictEqual(res.block, true, "Guardrail muss auch mit ausgeschaltetem Plan-Gate blocken")
  assert.match(res.blockReason, /BLOCKED/, "Guardrail-Meldung erwartet")
})

await t("Default-Lock-Pfad ist portabel (os.tmpdir + generischer Dateiname)", async () => {
  assert.strictEqual(DEFAULT_PLAN_LOCK_PATH, join(os.tmpdir(), "nexus-plan-gate.lock"))
  assert.strictEqual(basename(DEFAULT_PLAN_LOCK_PATH), "nexus-plan-gate.lock", "generischer Dateiname erwartet")
  assert.ok(
    !/miosha|think-gate/.test(basename(DEFAULT_PLAN_LOCK_PATH)),
    `Dateiname darf kein Deployment-Literal enthalten: ${DEFAULT_PLAN_LOCK_PATH}`,
  )
})

await t("Quelle: kein hartcodiertes Deployment-Literal mehr (Portabilitäts-Beweis an der Quelle)", async () => {
  // Bewusst an der QUELLE geprüft: os.tmpdir() löst zur Laufzeit legitim auf einen
  // Per-User-Pfad auf (hier ~/.openclaw/tmp) — Portabilität heißt: kein Literal im Code.
  const src = readFileSync(new URL("./hooks/pre-tool-gate.ts", import.meta.url), "utf8")
  assert.ok(!src.includes("miosha-think-gate"), "alter hartcodierter Lock-Pfad muss entfernt sein")
  assert.ok(
    !src.includes('\"/tmp/'),
    "kein hartcodierter /tmp-Literalpfad (existiert auf Windows nicht)",
  )
})

await t("maxAgeSeconds aus Config bestimmt die Gültigkeit", async () => {
  // Frischer Lock (jetzt) + maxAgeSeconds 30 → gültig → KEIN Plan-Block.
  writeFileSync(PLAN_LOCK_PATH, "plan: frischer Testplan\n")
  try {
    const { handler } = makeHandler({ enabled: true, lockPath: PLAN_LOCK_PATH, maxAgeSeconds: 30 })
    const res = await handler({ toolName: "exec", params: { command: "openclaw update" } }, {})
    assert.ok(!res || res.block !== true, `frischer Lock muss gelten: ${JSON.stringify(res)}`)
  } finally {
    try { unlinkSync(PLAN_LOCK_PATH) } catch {}
  }
})

process.exitCode = failed ? 1 : 0
