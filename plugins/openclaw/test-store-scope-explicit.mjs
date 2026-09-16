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
      console.log("FAIL ", name, "—", e?.stack ?? String(e))
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

// W39/F1+F6: payload-Lesezugriff nur mit upsert-Beweis — sonst klare AssertionError statt
// TypeError 'Cannot read properties of null'.
function lastPayload(touched) {
  assert.ok(touched.upsert >= 1, `upsert wurde nie aufgerufen (upsert=${touched.upsert}) — kein payload vorhanden`)
  return touched.payload
}

await t("explizit ungültig ('My Project') → Fehler, kein embed/upsert", async () => {
  const { tool, touched } = makeTool()
  const res = await tool.execute("id", { text: "x", scope: "My Project" })
  const text = res.content[0].text
  assert.match(text, /invalid scope/i, text)
  assert.match(text, /My Project/, "der abgelehnte Wert muss genannt werden")
  assert.strictEqual(touched.embed, 0, "embedder darf nicht berührt werden")
  assert.strictEqual(touched.upsert, 0, "qdrant darf nicht berührt werden")
  // W39/F8: maschinen-lesbarer Fehler-Contract (isError), nicht nur der Text
  assert.strictEqual(res.isError, true, "hard-fail muss isError:true tragen")
})

await t("explizit ungültig ('team_a') → Fehler", async () => {
  const { tool, touched } = makeTool()
  const res = await tool.execute("id", { text: "x", scope: "team_a" })
  assert.match(res.content[0].text, /invalid scope/i)
  assert.strictEqual(touched.embed, 0, "W39/F3: kein embed auf Fehlerpfad")
  assert.strictEqual(touched.upsert, 0)
  assert.strictEqual(res.isError, true, "W39/F8: isError:true")
})

await t("explizit ungültig (> 40 Zeichen) → Fehler", async () => {
  const { tool, touched } = makeTool()
  const res = await tool.execute("id", { text: "x", scope: "a".repeat(41) })
  assert.match(res.content[0].text, /invalid scope/i)
  assert.strictEqual(touched.embed, 0, "W39/F3: kein embed auf Fehlerpfad")
  assert.strictEqual(touched.upsert, 0)
  assert.strictEqual(res.isError, true, "W39/F8: isError:true")
})

// W39/F2+F9: Grenzfälle derselben Validierungs-Logik — exakt 40 Zeichen ist GÜLTIG,
// 39 ebenfalls, whitespace-gemischt wird getrimmt.
await t("Grenze: exakt 40 Zeichen → gültig (komplementär zu 41 → reject)", async () => {
  const { tool, touched } = makeTool()
  const res = await tool.execute("id", { text: "x", scope: "a".repeat(40) })
  assert.match(res.content[0].text, /Stored/i, "40 Zeichen ist die Obergrenze und muss durchlassen")
  assert.strictEqual(touched.upsert, 1)
  assert.strictEqual(lastPayload(touched).scope, "a".repeat(40))
})

await t("Grenze: 39 Zeichen → gültig", async () => {
  const { tool, touched } = makeTool()
  const res = await tool.execute("id", { text: "x", scope: "b".repeat(39) })
  assert.match(res.content[0].text, /Stored/i)
  assert.strictEqual(touched.upsert, 1)
})

await t("Grenze: gültiges Muster mit Whitespace wird getrimmt und akzeptiert", async () => {
  const { tool, touched } = makeTool()
  const res = await tool.execute("id", { text: "x", scope: "  proj-alpha  " })
  assert.match(res.content[0].text, /Stored/i)
  assert.strictEqual(touched.upsert, 1)
  assert.strictEqual(lastPayload(touched).scope, "proj-alpha", "getrimmter Wert wird gespeichert")
})

await t("explizit gültig → gespeichert mit genau diesem scope", async () => {
  const { tool, touched } = makeTool()
  const res = await tool.execute("id", { text: "x", scope: "proj-alpha" })
  assert.match(res.content[0].text, /Stored/i)
  assert.strictEqual(touched.upsert, 1)
  assert.strictEqual(touched.payload.scope, "proj-alpha")
  assert.match(res.content[0].text, /scope: proj-alpha/, "effektiver scope wird ge-echot")
})

await t("explizit leer ('' UND '   ') → gilt als 'nicht übergeben' (kein Fehler)", async () => {
  const { tool, touched } = makeTool()
  // W39/F4: JEDER Fall setzt eigenen Zustand — auch der echte Leerstring '' (bisher
  // nur whitespace, was still vom .trim() abhing).
  const empty = await tool.execute("id", { text: "x", scope: "" })
  assert.match(empty.content[0].text, /Stored/i, "'' ist 'omitted', kein Fehler")
  assert.strictEqual(touched.upsert, 1)
  assert.strictEqual(lastPayload(touched).scope, "default")
  const ws = await tool.execute("id2", { text: "x", scope: "   " })
  assert.match(ws.content[0].text, /Stored/i, "'   ' ist 'omitted', kein Fehler")
  assert.strictEqual(touched.upsert, 2)
  assert.strictEqual(lastPayload(touched).scope, "default")
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

process.exitCode = failed ? 1 : 0
