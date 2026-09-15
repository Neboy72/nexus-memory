/**
 * Regressionstest: expliziter scope wird validiert (H139, Welle 13)
 *
 * Vorher: `let scope = regex.test(explicit) ? explicit : ""` — ein explizit
 * übergebener, ungültiger scope ("My Project", "team_a", > 40 Zeichen) landete
 * still in default/auto-inferred, während der Caller "Stored: …" sah. Jetzt:
 * explizit + ungültig = harter Fehler (kein embed/upsert); nur aus cfg
 * stammende ungültige scopes bleiben fail-open — und die Erfolgsantwort nennt
 * immer den EFFEKTIVEN scope/category/access_level.
 */
import assert from "node:assert"
import { register } from "node:module"

register("./_typebox-test-loader.mjs", import.meta.url)
const { registerStoreTool } = await import("./tools/store.ts")

let failed = 0
const t = (name, fn) =>
  Promise.resolve()
    .then(fn)
    .then(() => console.log("PASS ", name))
    .catch((e) => {
      failed++
      console.log("FAIL ", name, "—", e.message)
    })

function makeTool(cfg = { accessLevel: "public" }) {
  let tool
  const touched = { embed: 0, upsert: 0, payload: null }
  const api = { registerTool: (tt) => { tool = tt } }
  registerStoreTool(
    api,
    { embed: async () => { touched.embed++; return [0.1, 0.2] } },
    { upsert: async (_id, _vec, payload) => { touched.upsert++; touched.payload = payload } },
    cfg,
  )
  return { tool, touched }
}

await t("explizit ungültig ('My Project') → Fehler, kein embed/upsert", async () => {
  const { tool, touched } = makeTool()
  const res = await tool.execute("id", { text: "x", scope: "My Project" })
  const text = res.content[0].text
  assert.match(text, /invalid scope/i, text)
  assert.match(text, /My Project/, "der abgelehnte Wert muss genannt werden")
  assert.strictEqual(touched.embed, 0, "embedder darf nicht berührt werden")
  assert.strictEqual(touched.upsert, 0, "qdrant darf nicht berührt werden")
})

await t("explizit ungültig ('team_a') → Fehler", async () => {
  const { tool, touched } = makeTool()
  const res = await tool.execute("id", { text: "x", scope: "team_a" })
  assert.match(res.content[0].text, /invalid scope/i)
  assert.strictEqual(touched.upsert, 0)
})

await t("explizit ungültig (> 40 Zeichen) → Fehler", async () => {
  const { tool, touched } = makeTool()
  const res = await tool.execute("id", { text: "x", scope: "a".repeat(41) })
  assert.match(res.content[0].text, /invalid scope/i)
  assert.strictEqual(touched.upsert, 0)
})

await t("explizit gültig → gespeichert mit genau diesem scope", async () => {
  const { tool, touched } = makeTool()
  const res = await tool.execute("id", { text: "x", scope: "proj-alpha" })
  assert.match(res.content[0].text, /Stored/i)
  assert.strictEqual(touched.upsert, 1)
  assert.strictEqual(touched.payload.scope, "proj-alpha")
  assert.match(res.content[0].text, /scope: proj-alpha/, "effektiver scope wird ge-echot")
})

await t("explizit leer ('') → gilt als 'nicht übergeben' (kein Fehler)", async () => {
  const { tool, touched } = makeTool()
  const res = await tool.execute("id", { text: "x", scope: "   " })
  assert.match(res.content[0].text, /Stored/i, "leerer scope ist 'omitted', kein Fehler")
  assert.strictEqual(touched.payload.scope, "default")
})

await t("implizit (cfg) ungültig → fail-open auf default + Echo", async () => {
  const { tool, touched } = makeTool({ accessLevel: "public", scope: "My Project" })
  const res = await tool.execute("id", { text: "x" })
  const text = res.content[0].text
  assert.match(text, /Stored/i, "cfg-scope bleibt fail-open (kein Fehler)")
  assert.strictEqual(touched.upsert, 1)
  assert.strictEqual(touched.payload.scope, "default")
  assert.match(text, /scope: default/, "der effektive scope muss sichtbar sein")
})

await t("implizit (cfg) gültig → dieser scope + vollständiges Echo", async () => {
  const { tool, touched } = makeTool({ accessLevel: "trusted", scope: "team-x" })
  const res = await tool.execute("id", { text: "x", category: "rule" })
  const text = res.content[0].text
  assert.strictEqual(touched.payload.scope, "team-x")
  assert.match(text, /scope: team-x/)
  assert.match(text, /category: rule/)
  assert.match(text, /access_level: trusted/)
})

process.exit(failed ? 1 : 0)
