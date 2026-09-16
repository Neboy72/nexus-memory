/**
 * Regressionstest: forget-by-query Mindest-Score (A4)
 *
 * Vector-Search liefert IMMER den ähnlichsten Treffer, auch wenn er nicht
 * passt. forget-by-query hat früher unconditional results[0] gelöscht.
 * Jetzt darf unter FORGET_MIN_SCORE (0.8) nicht gelöscht werden.
 */
import assert from "node:assert"
import { readFileSync } from "node:fs"
import { register } from "node:module"

register("./_typebox-test-loader.mjs", import.meta.url)
const { initLogger } = await import("./logger.ts")
// Logger-Sichtbarkeit: ohne Backend ist log.error ein NOOP und echte
// Fehler verschwinden hinter "Operation failed." — hier sichtbar machen.
initLogger(
  {
    info: (m) => console.error("[info]", m),
    warn: (m) => console.error("[warn]", m),
    error: (m, ...a) => console.error("[error]", m, ...a),
    debug: () => {},
  },
  true,
)
const forget = await import("./tools/forget.ts")
const { registerForgetTool } = forget

// Fund 6: Threshold aus der Produktion ableiten, statt hartcodiert —
// ohne Export den Quelltext parsen (test-only, keine API-Änderung nötig).
// Ein Constant-Drift oder ein < → <= Flip bricht diesen Test sofort.
const src = readFileSync(new URL("./tools/forget.ts", import.meta.url), "utf8")
const threshSrc = Number(src.match(/FORGET_MIN_SCORE\s*=\s*([\d.]+)/)?.[1])
const cmpSrc = src.match(/target\.score\s*(<|<=)\s*FORGET_MIN_SCORE/)?.[1]
assert.ok(Number.isFinite(threshSrc), "FORGET_MIN_SCORE fehlt/driftet in tools/forget.ts")
assert.strictEqual(cmpSrc, "<", "Vergleich muss strikt < sein (score === Threshold löschbar)")
const THRESH = threshSrc

let failed = 0
const t = (name, fn) =>
  Promise.resolve()
    .then(fn)
    .then(() => console.log("PASS  ", name))
    .catch((e) => {
      failed++
      console.log("FAIL  ", name, "—", e.message)
    })

function text(res) {
  return res?.content?.[0]?.text ?? ""
}

function makeTool(results, overrides = {}) {
  let tool
  const deleted = []
  const calls = { embed: [], searchByVector: [], scrollPointStrict: [], delete: [] }
  const api = { registerTool: (t) => { tool = t } }
  registerForgetTool(
    api,
    {
      // Fund 4: Mock nimmt Inputs auf — Regressionen an Vector/Query landen hier
      embed: async (query) => {
        calls.embed.push(query)
        return [0.1, 0.2]
      },
    },
    {
      searchByVector: async (vector, limit, accessLevel) => {
        calls.searchByVector.push({ vector, limit, accessLevel })
        return results
      },
      // Nr 388 (W22): delete geht jetzt nur nach bestandenem Lookup —
      // der Mock muss scrollPoint existieren lassen (Punkt vorhanden).
      // W34-Supersession: forget.ts nutzt jetzt scrollPointStrict (transiente
      // Qdrant-Fehler dürfen nicht als 404 gelten) — Mock ebenfalls strikt.
      scrollPoint: async (id) => ({ id }),
      scrollPointStrict: async (id) => {
        calls.scrollPointStrict.push(id)
        if (overrides.scrollPointStrictThrow) throw new Error("qdrant 500")
        return overrides.scrollPointNull ? null : { id }
      },
      delete: async (id) => {
        calls.delete.push(id) // Versuch tracken
        if (overrides.deleteThrows) throw new Error("delete boom")
        deleted.push(id) // nur erfolgreiche Löschungen
      },
    },
    // 4. Argument: cfg (mit accessLevel) — Signatur api, embedder, qdrant, cfg
    { accessLevel: "trusted" },
  )
  return { tool, deleted, calls }
}

const hit = (score) => ({ id: "mem-1", text: "irgendein Memory", score })

await t("Score unter Threshold → KEIN Löschen, Unsicher-Hinweis", async () => {
  const { tool, deleted, calls } = makeTool([hit(THRESH - 0.05)])
  const res = await tool.execute("id", { query: "etwas ganz anderes" })
  assert.strictEqual(deleted.length, 0, "unter Threshold darf nichts gelöscht werden")
  assert.match(text(res), /Unsicher/i, `Unsicher-Hinweis erwartet, bekam: ${text(res)}`)
  assert.match(text(res), /memory_id/i, "Hinweis auf memory_id erwartet")
  // Fund 4: query muss bis zur Suche durchgereicht werden
  assert.deepStrictEqual(calls.embed, ["etwas ganz anderes"], "embed bekommt den Query-Text")
  assert.strictEqual(calls.searchByVector[0].limit, 5, "Suchlimit 5")
  assert.strictEqual(calls.searchByVector[0].accessLevel, "trusted", "accessLevel aus cfg")
})

await t("Score über Threshold → Löschen", async () => {
  const { tool, deleted, calls } = makeTool([hit(THRESH + 0.05)])
  const res = await tool.execute("id", { query: "passt genau" })
  assert.deepStrictEqual(deleted, ["mem-1"], "über Threshold muss gelöscht werden")
  assert.match(text(res), /Forgot/i)
  assert.deepStrictEqual(calls.delete, ["mem-1"])
})

await t("Grenzfall: Score exakt am Threshold muss löschen (< vs <=)", async () => {
  // Fund 1+6: FORGET_MIN_SCORE ist bewusst strikt (<, nicht <=) — genau das
  // hier pinnt es: bei score === Threshold muss gelöscht werden.
  const { tool, deleted } = makeTool([hit(THRESH)])
  const res = await tool.execute("id", { query: "exakt am Limit" })
  assert.deepStrictEqual(deleted, ["mem-1"], "score === Threshold muss löschbar sein (strict <)")
  assert.match(text(res), /Forgot/i)
})

await t("leeres Suchergebnis → No-matching-Hinweis, kein Löschversuch", async () => {
  // Fund 2: dedicated results.length === 0 branch — eine Regression, die
  // results[0].score liest, muss hier abstürzen statt zu 'löschen'.
  const { tool, deleted } = makeTool([])
  const res = await tool.execute("id", { query: "nichts gefunden" })
  assert.deepStrictEqual(deleted, [], "leeres Ergebnis: kein delete")
  assert.match(text(res), /No matching memory/i)
})

await t("Lookup-Fehler (scrollPointStrict wirft) → fail-closed, kein Delete", async () => {
  // Fund 3: transiente Qdrant-Fehler dürfen NICHT als 404 gelten
  // (W34-Supersession-Kontext). Delete darf in diesem Pfad nie laufen.
  const { tool, deleted } = makeTool([], { scrollPointStrictThrow: true })
  const res = await tool.execute("id", { memoryId: "mem-1" })
  assert.deepStrictEqual(deleted, [], "Lookup-Fehler: fail-closed, kein delete")
  assert.match(text(res), /lookup failed/i)
})

await t("scrollPoint null → 404-Zweig, kein Delete", async () => {
  const { tool, deleted } = makeTool([], { scrollPointNull: true })
  const res = await tool.execute("id", { memoryId: "mem-1" })
  assert.deepStrictEqual(deleted, [], "null-Lookup: kein delete")
  assert.match(text(res), /not found/i)
})

await t("delete wirft → isError-Branch, kein Teilerfolg", async () => {
  const { tool, deleted } = makeTool([hit(THRESH + 0.1)], { deleteThrows: true })
  const res = await tool.execute("id", { memoryId: "mem-1" })
  assert.strictEqual(res.isError, true, "delete-Fehler muss isError tragen")
  assert.deepStrictEqual(deleted, [])
})

await t("beides gesetzt (memoryId+query) → klare Fehlermeldung", async () => {
  const { tool, deleted } = makeTool([hit(0.9)])
  const res = await tool.execute("id", { memoryId: "mem-1", query: "x" })
  assert.strictEqual(res.isError, true)
  assert.match(text(res), /either memoryId OR query/i)
  assert.deepStrictEqual(deleted, [])
})

await t("registerForgetTool registriert das Tool (kein undefined-Tool)", async () => {
  // Fund 5: falls registerForgetTool irgendwann nicht mehr registriert,
  // muss die Meldung das klar benennen statt undefined-Dereference.
  let tool
  const api = { registerTool: (t) => { tool = t } }
  registerForgetTool(api, { embed: async () => [0.1, 0.2] }, {}, {}, "nexus_forget")
  assert.ok(tool, "registerForgetTool muss ein Tool registrieren")
  assert.strictEqual(tool.name, "nexus_forget")
})

// Fund 8: Wording-Assertions nur auf stabile Produktionstexte, keine
// Locale-Abhängigkeiten (Unsicher/Forgot sind Produktionstexte)

// Fund 9: process.exitCode statt process.exit — gepufferte Ausgaben
// (CI-Pipes) werden nicht mehr abgeschnitten, der Event-Loop darf leeren.
process.exitCode = failed ? 1 : 0