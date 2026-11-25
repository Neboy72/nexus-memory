/**
 * Regressionstest: geteilter Raum LEHNT einen zu hohen Schreibpegel AB
 * (24.11.2026, ClawHub-Befund "suspicious" gegen 1.22.68)
 *
 * Befund: `tools/store.ts` senkte in einem nachweislich geteilten Raum die
 * angeforderte Stufe `trusted`/`private` still auf `public` und antwortete
 * trotzdem "Stored: …". ClawHub liest das als Absenkung der Vertraulichkeit:
 * der Aufrufer bekommt einen offeneren Eintrag, als er verlangt hat, und
 * erfaehrt es nicht.
 *
 * Regel seit 0.22.69: in einem geteilten Raum ist `public` die Obergrenze. Wer
 * dort `trusted` oder `private` verlangt, bekommt eine Absage (`isError: true`,
 * maschinenlesbarer Text) und es wird NICHTS geschrieben. Ein Aufruf ohne
 * gebundenen Raum, mit unbekanntem Raum oder mit veraltetem Merkmal bleibt
 * unveraendert — nur ein beweisbar geteilter Raum lehnt ab. Die Leseseite ist
 * nicht Teil dieses Tests.
 *
 * Geprueft wird:
 *   a) private in geteiltem Raum → Absage mit Grund, Obergrenze, Ausweg; kein Schreiben
 *   b) public in geteiltem Raum → geht durch
 *   c) private ohne Raum → geht durch
 *   d) trusted in geteiltem Raum → Absage
 *   e) veraltetes Merkmal → Absage (Raum nicht belegbar, room_unknown)
 *   f) unbekannter Raum (shared === null) → Absage (room_unknown)
 *   g) beobachteter eigener Raum (shared === false) → geht durch
 */
import assert from "node:assert"
import { register } from "node:module"

register("./_typebox-test-loader.mjs", import.meta.url)
const { registerStoreTool } = await import("./tools/store.ts")
const { setTurnContext, bindToolSession, resetTurnContext, TURN_MARKER_TTL_MS } = await import(
  "./lib/turn-context.ts"
)

let failed = 0
const t = (name, fn) =>
  Promise.resolve()
    .then(fn)
    .then(() => console.log("PASS ", name))
    .catch((e) => {
      failed++
      console.log("FAIL ", name, "—", e?.message ?? String(e))
    })

const TEXT = "Der Verteiler steht im Flur."

function makeStoreTool() {
  let tool = null
  const touched = { embed: 0, upsert: 0, payload: null }
  registerStoreTool(
    { registerTool: (tt) => { tool = tt } },
    {
      embed: async () => {
        touched.embed++
        return [0.1, 0.2]
      },
    },
    {
      upsert: async (_id, _vec, payload) => {
        touched.upsert++
        touched.payload = payload
        return undefined
      },
    },
    { accessLevel: "public" },
  )
  assert.ok(tool, "nexus_store muss registriert sein")
  return { tool, touched }
}

function answerText(res) {
  return (res?.content ?? []).map((c) => c?.text ?? "").join("\n")
}

// ── a) private in geteiltem Raum ───────────────────────────────────────────

await t("a) private im geteilten Raum: Absage, kein Schreibvorgang", async () => {
  resetTurnContext()
  setTurnContext(true, "gruppe", Date.now(), true, false, true)
  bindToolSession("aufruf-a", "gruppe")

  const { tool, touched } = makeStoreTool()
  const res = await tool.execute("aufruf-a", { text: TEXT, access_level: "private" })

  assert.strictEqual(res?.isError, true, "die Absage muss isError:true tragen")
  const text = answerText(res)
  assert.match(text, /shared room/, "die Absage muss den Raum als Grund nennen")
  assert.match(text, /max_level: public/, "die Absage muss die Obergrenze maschinenlesbar nennen")
  assert.match(text, /requested_level: private/, "die Absage muss die angeforderte Stufe nennen")
  assert.match(text, /shared-room-write-rejected/, "die Absage braucht einen stabilen Code")
  assert.match(text, /nothing was written/, "die Absage muss sagen, dass nichts gespeichert wurde")
  assert.strictEqual(touched.upsert, 0, "abgelehnt heißt: kein Upsert")
  assert.strictEqual(touched.embed, 0, "abgelehnt heißt: auch keine Einbettung")
})

// ── b) public in geteiltem Raum ────────────────────────────────────────────

await t("b) public im geteilten Raum: geht durch", async () => {
  resetTurnContext()
  setTurnContext(true, "gruppe", Date.now(), true, false, true)
  bindToolSession("aufruf-b", "gruppe")

  const { tool, touched } = makeStoreTool()
  const res = await tool.execute("aufruf-b", { text: TEXT, access_level: "public" })

  assert.notStrictEqual(res?.isError, true, `unerwartete Absage: ${answerText(res)}`)
  assert.strictEqual(touched.upsert, 1, "public muss geschrieben werden")
  assert.strictEqual(touched.payload?.access_level, "public")
})

// ── c) private ohne Raum ───────────────────────────────────────────────────

await t("c) private ohne gebundenen Raum: geht durch", async () => {
  resetTurnContext()

  const { tool, touched } = makeStoreTool()
  const res = await tool.execute("aufruf-ohne-bindung", { text: TEXT, access_level: "private" })

  assert.notStrictEqual(res?.isError, true, `unerwartete Absage: ${answerText(res)}`)
  assert.strictEqual(touched.payload?.access_level, "private", "ohne Raum bleibt die Stufe")
})

// ── d) trusted in geteiltem Raum ───────────────────────────────────────────

await t("d) trusted im geteilten Raum: Absage", async () => {
  resetTurnContext()
  setTurnContext(false, "gruppe", Date.now(), true, false, true)
  bindToolSession("aufruf-d", "gruppe")

  const { tool, touched } = makeStoreTool()
  const res = await tool.execute("aufruf-d", { text: TEXT, access_level: "trusted" })

  assert.strictEqual(res?.isError, true, "trusted liegt ueber public und wird abgelehnt")
  assert.match(answerText(res), /requested_level: trusted/)
  assert.strictEqual(touched.upsert, 0)
})

// ── e) veraltetes Merkmal ──────────────────────────────────────────────────

await t("e) veraltetes Merkmal: Absage (Raum nicht belegbar)", async () => {
  resetTurnContext()
  const alt = Date.now() - TURN_MARKER_TTL_MS - 1000
  setTurnContext(true, "alt", alt, true, false, true)
  bindToolSession("aufruf-e", "alt")

  const { tool, touched } = makeStoreTool()
  const res = await tool.execute("aufruf-e", { text: TEXT, access_level: "private" })

  assert.strictEqual(res?.isError, true, "ohne frisches Merkmal darf ein privater Schreibvorgang nicht durch")
  assert.match(answerText(res), /room_unknown/, "der Grund muss als room_unknown benannt sein")
  assert.strictEqual(touched.upsert, 0)
})

// ── f) unbekannter Raum ────────────────────────────────────────────────────

await t("f) unbekannter Raum (shared === null): Absage", async () => {
  resetTurnContext()
  setTurnContext(false, "unbekannt", Date.now(), null, false, true)
  bindToolSession("aufruf-f", "unbekannt")

  const { tool, touched } = makeStoreTool()
  const res = await tool.execute("aufruf-f", { text: TEXT, access_level: "private" })

  assert.strictEqual(res?.isError, true, "ohne frisches Merkmal darf ein privater Schreibvorgang nicht durch")
  assert.match(answerText(res), /room_unknown/, "der Grund muss als room_unknown benannt sein")
  assert.strictEqual(touched.upsert, 0)
})

// ── g) beobachteter eigener Raum ───────────────────────────────────────────

await t("g) beobachteter eigener Raum (shared === false): geht durch", async () => {
  resetTurnContext()
  setTurnContext(false, "eigen", Date.now(), false, false, true)
  bindToolSession("aufruf-g", "eigen")

  const { tool, touched } = makeStoreTool()
  const res = await tool.execute("aufruf-g", { text: TEXT, access_level: "private" })

  assert.notStrictEqual(res?.isError, true, `unerwartete Absage: ${answerText(res)}`)
  assert.strictEqual(touched.payload?.access_level, "private")
})

process.exitCode = failed ? 1 : 0
