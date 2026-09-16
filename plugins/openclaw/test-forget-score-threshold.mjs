/**
 * Regressionstest: forget-by-query Mindest-Score (A4)
 *
 * Vector-Search liefert IMMER den ähnlichsten Treffer, auch wenn er nicht
 * passt. forget-by-query hat früher unconditional results[0] gelöscht.
 * Jetzt darf unter FORGET_MIN_SCORE (0.8) nicht gelöscht werden.
 */
import assert from "node:assert"
import { register } from "node:module"

register("./_typebox-test-loader.mjs", import.meta.url)
const { registerForgetTool } = await import("./tools/forget.ts")

let failed = 0
const t = (name, fn) =>
  Promise.resolve()
    .then(fn)
    .then(() => console.log("PASS ", name))
    .catch((e) => {
      failed++
      console.log("FAIL ", name, "—", e.message)
    })

function makeTool(results) {
  let tool
  const deleted = []
  const api = { registerTool: (t) => { tool = t } }
  registerForgetTool(
    api,
    { embed: async () => [0.1, 0.2] },
    {
      searchByVector: async () => results,
      // Nr 388 (W22): delete geht jetzt nur nach bestandenem Lookup —
      // der Mock muss scrollPoint existieren lassen (Punkt vorhanden).
      // W34-Supersession: forget.ts nutzt jetzt scrollPointStrict (transiente
      // Qdrant-Fehler dürfen nicht als 404 gelten) — Mock ebenfalls strikt.
      scrollPoint: async (id) => ({ id }),
      scrollPointStrict: async (id) => ({ id }),
      delete: async (id) => { deleted.push(id) },
    },
    {},
  )
  return { tool, deleted }
}

const hit = (score) => ({ id: "mem-1", text: "irgendein Memory", score })

await t("Score unter Threshold → KEIN Löschen, Unsicher-Hinweis", async () => {
  const { tool, deleted } = makeTool([hit(0.42)])
  const res = await tool.execute("id", { query: "etwas ganz anderes" })
  const text = res.content[0].text
  assert.strictEqual(deleted.length, 0, "unter Threshold darf nichts gelöscht werden")
  assert.match(text, /Unsicher/i, `Unsicher-Hinweis erwartet, bekam: ${text}`)
  assert.match(text, /memory_id/i, "Hinweis auf memory_id erwartet")
})

await t("Score über Threshold → Löschen", async () => {
  const { tool, deleted } = makeTool([hit(0.95)])
  const res = await tool.execute("id", { query: "passt genau" })
  assert.deepStrictEqual(deleted, ["mem-1"], "über Threshold muss gelöscht werden")
  assert.match(res.content[0].text, /Forgot/i)
})

await t("direkte memoryId bleibt unabhängig vom Score", async () => {
  const { tool, deleted } = makeTool([])
  await tool.execute("id", { memoryId: "direct-7" })
  assert.deepStrictEqual(deleted, ["direct-7"], "memoryId löscht direkt")
})

process.exit(failed ? 1 : 0)
