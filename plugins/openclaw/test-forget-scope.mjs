/**
 * Regressionstest: Vergessen per ID respektiert den Bereich (scope).
 *
 * Anlass (ClawHub-Audit 20.11.2026, MCP07): Sichtbarkeit hat zwei unabhängige
 * Achsen, access_level und scope. Der Suchweg trägt immer einen scope-Filter,
 * der direkte Löschweg prüfte nur die Stufe. Wer eine ID kannte, konnte damit
 * einen Eintrag aus einem fremden Verbund löschen.
 *
 * Geprüft wird am echten Tool (registerForgetTool) mit gestubbtem
 * Qdrant-Client: fremder Bereich wird abgewiesen und NICHT gelöscht, shared,
 * eigener Bereich und Altbestand ohne scope-Feld bleiben löschbar.
 */
import assert from "node:assert"
import { register } from "node:module"

register("./_typebox-test-loader.mjs", import.meta.url)
const { registerForgetTool } = await import("./tools/forget.ts")
const { setTurnContext, bindToolSession } = await import("./lib/turn-context.ts")

const SESSION = "forget-scope"
const BUNDLE = "meinbereich"

setTurnContext(false, SESSION, Date.now(), false, false, true)
bindToolSession("call-1", SESSION)

let failed = 0
const t = async (name, fn) => {
  try {
    await Promise.race([
      Promise.resolve().then(fn),
      new Promise((_, rej) => setTimeout(() => rej(new Error("timeout: test hing 10s")), 10000)),
    ])
    console.log("PASS ", name)
  } catch (e) {
    failed++
    console.log("FAIL ", name, "—", (e && e.stack) || String(e))
  }
}

/** Tool mit einem Punkt im Speicher; protokolliert, ob gelöscht wurde. */
function makeTool(payload) {
  let tool
  const deleted = []
  const api = {
    registerTool: (tt) => {
      if (!tt || typeof tt.execute !== "function") {
        throw new Error("registerTool erhielt kein ausfuehrbares Tool")
      }
      tool = tt
    },
  }
  const qdrantClient = {
    scrollPointStrict: async (id) => ({ id, payload }),
    delete: async (id) => {
      deleted.push(id)
    },
  }
  registerForgetTool(
    api,
    { embed: async () => [0.1, 0.2] },
    qdrantClient,
    { collection: "nexus", accessLevel: "private", scope: BUNDLE },
    "nexus_forget",
  )
  if (!tool) throw new Error("registerForgetTool hat KEIN Tool registriert")
  return { tool, deleted }
}

const call = async (tool, id) => {
  const res = await tool.execute("call-1", { memoryId: id })
  return res
}

const text = (res) => {
  const block = res?.content?.[0]
  if (!block || typeof block.text !== "string") {
    throw new Error(`unerwartetes Tool-Output-Shape: ${JSON.stringify(res).slice(0, 200)}`)
  }
  return block.text
}

await t("fremder Bereich: Loeschen wird abgewiesen und nicht ausgefuehrt", async () => {
  const { tool, deleted } = makeTool({ text: "geheim", access_level: "private", scope: "fremd" })
  const res = await call(tool, "id-fremd")
  assert.strictEqual(res?.isError, true, `erwartet Fehler, bekam ${JSON.stringify(res).slice(0, 200)}`)
  assert.match(text(res), /outside your scope/)
  assert.deepStrictEqual(deleted, [], "fremder Eintrag darf nicht geloescht werden")
})

await t("shared-Bereich (default) bleibt loeschbar", async () => {
  const { tool, deleted } = makeTool({ text: "geteilt", access_level: "private", scope: "default" })
  const res = await call(tool, "id-default")
  assert.notStrictEqual(res?.isError, true, text(res))
  assert.deepStrictEqual(deleted, ["id-default"])
})

await t("eigener Bereich bleibt loeschbar", async () => {
  const { tool, deleted } = makeTool({ text: "meins", access_level: "private", scope: BUNDLE })
  const res = await call(tool, "id-eigen")
  assert.notStrictEqual(res?.isError, true, text(res))
  assert.deepStrictEqual(deleted, ["id-eigen"])
})

await t("Altbestand ohne scope-Feld bleibt loeschbar", async () => {
  const { tool, deleted } = makeTool({ text: "alt", access_level: "private" })
  const res = await call(tool, "id-alt")
  assert.notStrictEqual(res?.isError, true, text(res))
  assert.deepStrictEqual(deleted, ["id-alt"])
})

await t("Bereich in Grossschreibung/Leerzeichen wird normalisiert", async () => {
  const { tool, deleted } = makeTool({ text: "meins", access_level: "private", scope: "  MEINBEREICH " })
  const res = await call(tool, "id-gross")
  assert.notStrictEqual(res?.isError, true, text(res))
  assert.deepStrictEqual(deleted, ["id-gross"])
})

await t("Stufenpruefung bleibt davor bestehen (public darf private nicht loeschen)", async () => {
  const { tool, deleted } = makeTool({ text: "privat", access_level: "private", scope: "default" })
  // Ein Aufruf ohne Session-Bindung liest "public".
  const res = await tool.execute("ungebunden", { memoryId: "id-privat" })
  assert.strictEqual(res?.isError, true, JSON.stringify(res).slice(0, 200))
  assert.match(text(res), /outside your access level/)
  assert.deepStrictEqual(deleted, [])
})

if (failed > 0) {
  console.log(`\n${failed} Test(s) fehlgeschlagen`)
  process.exit(1)
}
console.log("\nalle Tests gruen")
