/**
 * Regressionstest: Turn-Kontext ist SITZUNGSGEBUNDEN
 * (ClawHub-Audit 14.11.2026, MCP07-3)
 *
 * Befund: Die Autorisierung eines Turns lag prozessglobal in einer einzigen
 * Variablen. Bei parallelen oder verschachtelten Laeufen (zwei Chats, ein
 * Cron-Lauf neben einem Gespraech) ueberschrieben sich die Merkmale, und ein
 * Werkzeugaufruf in Sitzung A konnte nach der Stufe von Sitzung B beurteilt
 * werden.
 *
 * Regel seit 1.22.4: Das Merkmal liegt in einer Map nach Sitzungsschluessel.
 * `before_tool_call` bindet jeden Werkzeugaufruf an seine Sitzung
 * (`bindToolSession`), das Werkzeug liest ausschliesslich das Merkmal DIESER
 * Sitzung. Fehlt die Bindung, gilt `public` — nie "die zuletzt gesehene Stufe".
 *
 * Geprueft wird:
 *   a) zwei parallele Sitzungen mit unterschiedlicher Stufe stoeren sich nicht
 *   b) ein ungebundener Aufruf erbt NICHTS von einer privaten Sitzung
 *   c) ein abgelaufenes Merkmal kappt nur seine eigene Sitzung
 *   d) ohne Sitzungsschluessel wird kein Merkmal gespeichert
 *   e) Ende-zu-Ende: nexus_search fragt je Aufruf die Stufe SEINER Sitzung ab
 */
import assert from "node:assert"
import { register } from "node:module"
import { mkdtempSync, rmSync } from "node:fs"
import { tmpdir } from "node:os"
import { join } from "node:path"

register("./_typebox-test-loader.mjs", import.meta.url)

const SANDBOX = mkdtempSync(join(tmpdir(), "nexus-turn-sessions-"))
process.env.NEXUS_AGENT_SELFTEST = "1"
process.env.NEXUS_CAPTURE_QUEUE_FILE = join(SANDBOX, "capture-retry-queue.jsonl")

const {
  setTurnContext,
  getTurnContext,
  resetTurnContext,
  bindToolSession,
  sessionKeyForToolCall,
  toolAccessLevel,
  toolCallAccessLevel,
  resolveReadLevel,
  TURN_MARKER_TTL_MS,
} = await import("./lib/turn-context.ts")
const { registerSearchTool } = await import("./tools/search.ts")

let failed = 0
const t = (name, fn) =>
  Promise.resolve()
    .then(fn)
    .then(() => console.log("PASS ", name))
    .catch((e) => {
      failed++
      console.log("FAIL ", name, "—", e?.message ?? String(e))
    })

// ── a) zwei parallele Sitzungen ───────────────────────────────────────────

await t("a) zwei Sitzungen mit unterschiedlicher Stufe stoeren sich nicht", () => {
  resetTurnContext()

  // Sitzung A: Gruppen-Turn, Raum NICHT freigegeben → public.
  setTurnContext(true, "session-a", Date.now(), null, false, true)
  // Sitzung B: Direktchat, Eigentuemer bekannt → cfg-Stufe.
  setTurnContext(false, "session-b", Date.now(), false, false, true)

  bindToolSession("call-a", "session-a")
  bindToolSession("call-b", "session-b")

  assert.strictEqual(toolCallAccessLevel("private", "call-a"), "public", "A bleibt public")
  assert.strictEqual(toolCallAccessLevel("private", "call-b"), "private", "B behaelt die cfg-Stufe")

  // Und in der Gegenprobe: A zuerst gesetzt, B danach — A darf sich nicht
  // veraendert haben (genau das war der prozessglobale Fehler).
  assert.strictEqual(toolCallAccessLevel("private", "call-a"), "public")
  assert.strictEqual(getTurnContext("session-a")?.isGroup, true)
  assert.strictEqual(getTurnContext("session-b")?.isGroup, false)
})

// ── b) ungebundener Aufruf ────────────────────────────────────────────────

await t("b) ungebundener Aufruf erbt nichts von einer privaten Sitzung", () => {
  resetTurnContext()
  setTurnContext(false, "privat", Date.now(), false, false, true)
  bindToolSession("call-privat", "privat")

  assert.strictEqual(toolCallAccessLevel("private", "call-privat"), "private")
  assert.strictEqual(
    toolCallAccessLevel("private", "call-unbekannt"),
    "public",
    "ohne Bindung kein Zugriff auf die zuletzt gesehene Stufe",
  )
  assert.strictEqual(toolCallAccessLevel("private", ""), "public")
  assert.strictEqual(toolCallAccessLevel("private", undefined), "public")
  assert.strictEqual(sessionKeyForToolCall("call-unbekannt"), undefined)
})

// ── c) abgelaufenes Merkmal ───────────────────────────────────────────────

await t("c) abgelaufenes Merkmal kappt nur seine eigene Sitzung", () => {
  resetTurnContext()
  const alt = Date.now() - TURN_MARKER_TTL_MS
  setTurnContext(false, "alt", alt, false, false, true)
  setTurnContext(false, "frisch", Date.now(), false, false, true)
  bindToolSession("call-alt", "alt")
  bindToolSession("call-frisch", "frisch")

  assert.strictEqual(toolCallAccessLevel("private", "call-alt"), "public")
  assert.strictEqual(toolCallAccessLevel("private", "call-frisch"), "private")
  assert.strictEqual(getTurnContext("alt"), null, "abgelaufenes Merkmal ist weg")
  assert.strictEqual(getTurnContext("frisch")?.isGroup, false)
})

// ── d) ohne Sitzungsschluessel ────────────────────────────────────────────

await t("d) ohne Sitzungsschluessel wird kein Merkmal gespeichert", () => {
  resetTurnContext()
  setTurnContext(false, undefined, Date.now(), false, false, true)
  setTurnContext(false, "   ", Date.now(), false, false, true)
  assert.strictEqual(getTurnContext(undefined), null)
  assert.strictEqual(getTurnContext(""), null)
  assert.strictEqual(toolAccessLevel("private"), "public", "ohne Sitzung public")
  assert.strictEqual(toolAccessLevel("private", "   "), "public")
})

// ── e) Ende-zu-Ende mit dem echten Werkzeug ───────────────────────────────

await t("e) nexus_search fragt je Aufruf die Stufe SEINER Sitzung ab", async () => {
  resetTurnContext()

  const seen = []
  let tool = null
  registerSearchTool(
    { registerTool: (tt) => { tool = tt } },
    { embed: async () => [0.1] },
    {
      search: async (_vector, _limit, level) => {
        seen.push(level)
        return []
      },
    },
    { accessLevel: "private" },
  )
  assert.ok(tool, "nexus_search muss registriert sein")

  // Gruppe (nicht freigegeben) und Direktchat laufen parallel.
  setTurnContext(true, "gruppe", Date.now(), null, false, true)
  setTurnContext(false, "direkt", Date.now(), false, false, true)
  bindToolSession("aufruf-gruppe", "gruppe")
  bindToolSession("aufruf-direkt", "direkt")

  await tool.execute("aufruf-gruppe", { query: "irgendetwas" })
  await tool.execute("aufruf-direkt", { query: "irgendetwas" })
  await tool.execute("aufruf-ohne-bindung", { query: "irgendetwas" })

  assert.deepStrictEqual(
    seen,
    ["public", "private", "public"],
    `jeder Aufruf muss seine eigene Stufe sehen, war: ${seen.join(" | ")}`,
  )
})

// ── f) die Regel selbst ───────────────────────────────────────────────────

await t("f) resolveReadLevel: unbekannter Eigentuemer kappt immer auf public", () => {
  assert.strictEqual(resolveReadLevel(true, null, "private", true, false), "public")
  assert.strictEqual(resolveReadLevel(false, false, "private", false, false), "public")
  assert.strictEqual(resolveReadLevel(false, false, "private", false, true), "private")
  assert.strictEqual(resolveReadLevel(true, false, "private", false, true), "public")
  assert.strictEqual(resolveReadLevel(true, false, "private", true, true), "private")
  assert.strictEqual(resolveReadLevel(true, true, "private", true, true), "public")
})

rmSync(SANDBOX, { recursive: true, force: true })
process.exitCode = failed ? 1 : 0
