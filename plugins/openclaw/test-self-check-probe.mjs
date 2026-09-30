/**
 * Regressionstest: Fehlalarm der Nexus-Selbstpruefung (30.09.2026)
 *
 * Produktionsfehler: `probeQdrant` machte EINEN Versuch mit 5s-Frist; riss
 * diese beim Gateway-Start (Last/Build) einmal, schrieb die Sonde `ok:false`
 * und der Agent meldete dem Operator einen Ausfall, obwohl Qdrant gesund war.
 *
 * Bewiesen wird das VERHALTEN (kein Quelltext-Lesen, kein Regex auf Dateien):
 *  A: erster Versuch Frist-Abriss, zweiter gelingt → ok:true, KEINE Warnung
 *  B: beide Versuche scheitern → ok:false + URL/Ursache, Warnung entsteht
 *  C: Frist-Konstante ≥ 15000 ms und genau 2 Versuche
 *  D: Erfolg im ersten Versuch → genau EIN Netzaufruf (keine Verdopplung)
 *  E: HTTP-Fehlerstatus wird wie ein Fehler behandelt, aber erst nach dem
 *     Wiederholungsversuch gewarnt (der Server ANTWORTET ja)
 *
 * Netzaufruf ist via optionalem `fetchFn`-Parameter injizierbar; der Aufrufer
 * bildet exakt die Fire-and-forget-Kette aus index.ts nach (probeQdrant →
 * writeSelfCheck). Exit 0 = alle Cases PASS, Exit 1 = mindestens ein FAIL.
 */
import test from "node:test"
import assert from "node:assert/strict"
import fs from "node:fs"
import os from "node:os"
import path from "node:path"
import {
  probeQdrant,
  SELF_CHECK_PROBE_TIMEOUT_MS,
  SELF_CHECK_PROBE_ATTEMPTS,
  SELF_CHECK_PROBE_PAUSE_MS,
} from "./index.ts"
import { buildSelfCheckWarning, resetSelfCheckForTest, writeSelfCheck } from "./lib/self-check.ts"
import { initLogger } from "./logger.ts"

// Realer Write in ein Temp-Verzeichnis statt ins Home — kein Seiteneffekt.
process.env.NEXUS_DATA_DIR = fs.mkdtempSync(path.join(os.tmpdir(), "nexus-probe-"))
process.env.NEXUS_AGENT_ID = "openclaw-probe-test"
initLogger({ info() {}, warn() {}, error() {}, debug() {} }, false)

const FIX = "start Qdrant or point qdrantUrl at your instance in the OpenClaw plugin config"
const URL = "http://localhost:6333"
const COLLECTIONS = `${URL}/collections`

/** Exactly the error shape AbortSignal.timeout produces. */
function timeoutError() {
  const e = new Error("The operation was aborted due to timeout")
  e.name = "TimeoutError"
  return e
}

/** Mirrors the caller chain in index.ts: probe → writeSelfCheck, then read the warning. */
async function runCaller(fetchFn) {
  resetSelfCheckForTest()
  const result = await probeQdrant(URL, fetchFn)
  writeSelfCheck(result.ok, result.ok ? "" : result.reason, FIX)
  return { result, warning: buildSelfCheckWarning() }
}

test("A: erster Versuch Frist-Abriss, zweiter gelingt → ok:true und KEINE Warnung", async () => {
  let calls = 0
  const fetchFn = async () => {
    calls++
    if (calls === 1) throw timeoutError()
    return { ok: true, status: 200 }
  }
  const { result, warning } = await runCaller(fetchFn)
  assert.equal(calls, 2, "ein Wiederholungsversuch muss stattfinden")
  assert.equal(result.ok, true, "ein einzelner Frist-Ausrutscher darf kein Ausfall sein")
  assert.equal(result.reason, "", "gesunder Ausgang trägt keinen Grund")
  assert.deepEqual(warning, [], "KEINE Warnung im Fehlalarm-Fall")
})

test("B: beide Versuche scheitern → ok:false mit URL/Ursache, Warnung entsteht", async () => {
  let calls = 0
  const fetchFn = async () => {
    calls++
    throw timeoutError()
  }
  const { result, warning } = await runCaller(fetchFn)
  assert.equal(calls, 2, "genau 2 Versuche vor der Warnung")
  assert.equal(result.ok, false, "echter Ausfall muss weiterhin warnen")
  assert.ok(
    result.reason.includes(`Qdrant at ${COLLECTIONS} is unreachable (`),
    `Format mit URL muss erhalten bleiben: ${result.reason}`,
  )
  assert.ok(result.reason.includes("timeout"), "Ursache muss genannt sein")
  assert.ok(result.reason.includes("2 attempts"), "Fristfehler muss die Versuchszahl nennen")
  assert.ok(warning.some((l) => l.includes("NOT WORKING")), "Warnung muss entstehen")
  assert.ok(warning.some((l) => l.includes("is unreachable")), "Cause muss in der Warnung stehen")
})

test("C: Frist ≥ 15000 ms und genau 2 Versuche", () => {
  assert.ok(
    SELF_CHECK_PROBE_TIMEOUT_MS >= 15000,
    `Frist muss mind. 15000 ms sein, war ${SELF_CHECK_PROBE_TIMEOUT_MS}`,
  )
  assert.equal(SELF_CHECK_PROBE_ATTEMPTS, 2, "genau 2 Versuche (Erstversuch + 1 Wiederholung)")
  assert.ok(SELF_CHECK_PROBE_PAUSE_MS > 0, "Pause zwischen den Versuchen muss > 0 sein")
  assert.ok(SELF_CHECK_PROBE_PAUSE_MS < SELF_CHECK_PROBE_TIMEOUT_MS, "Pause kürzer als die Frist")
})

test("D: Erfolg im ersten Versuch → genau EIN Netzaufruf", async () => {
  let calls = 0
  const fetchFn = async () => {
    calls++
    return { ok: true, status: 200 }
  }
  const { result, warning } = await runCaller(fetchFn)
  assert.equal(calls, 1, "Normalfall darf NICHT verdoppelt werden")
  assert.equal(result.ok, true)
  assert.deepEqual(warning, [])
})

test("E: HTTP-Fehlerstatus (503) gilt als Fehler, aber Warnung erst nach dem Retry", async () => {
  let calls = 0
  const failing = async () => {
    calls++
    return { ok: false, status: 503 }
  }
  const first = await runCaller(failing)
  assert.equal(calls, 2, "auch ein HTTP-Fehler wird wiederholt")
  assert.equal(first.result.ok, false)
  assert.ok(
    first.result.reason.includes(`Qdrant at ${COLLECTIONS} responded HTTP 503`),
    `HTTP-Ursache muss erhalten bleiben: ${first.result.reason}`,
  )
  assert.ok(first.warning.some((l) => l.includes("NOT WORKING")))

  // Erholt sich der Server beim Wiederholungsversuch, gibt es keine Warnung.
  let calls2 = 0
  const recovers = async () => {
    calls2++
    return calls2 === 1 ? { ok: false, status: 503 } : { ok: true, status: 200 }
  }
  const second = await runCaller(recovers)
  assert.equal(second.result.ok, true, "Antwortet der Server beim Retry, kein Alarm")
  assert.deepEqual(second.warning, [])
})

// Zustand sauber hinterlassen.
resetSelfCheckForTest()
