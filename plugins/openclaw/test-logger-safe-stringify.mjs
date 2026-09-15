/**
 * Regressionstest: logger safeStringify (H9, Welle 12)
 *
 * debugRequest/debugResponse riefen JSON.stringify roh auf: Zyklen/BigInt
 * warfen, Payloads (inkl. Secrets) wurden unbefiltert gedumpt. Jetzt:
 * safeStringify mit Zyklus-/BigInt-Schutz und Secret-Redaktion.
 */
import assert from "node:assert"
import { safeStringify } from "./logger.ts"

let failed = 0
const t = (name, fn) =>
  Promise.resolve()
    .then(fn)
    .then(() => console.log("PASS ", name))
    .catch((e) => {
      failed++
      console.log("FAIL ", name, "—", e.message)
    })

await t("zirkuläres Objekt → kein Wurf, enthält '[circular]'", () => {
  const o = { name: "x" }
  o.self = o
  let out
  assert.doesNotThrow(() => { out = safeStringify(o) })
  assert.match(out, /\[circular\]/)
})

await t("BigInt → String-Repräsentation, kein Wurf", () => {
  const out = safeStringify({ n: 12345678901234567890n })
  assert.match(out, /12345678901234567890/)
})

await t("Secret-Keys werden redigiert", () => {
  const out = safeStringify({ apiKey: "sk-xyz", nested: { authorization: "Bearer abc" } })
  assert.match(out, /\[redacted\]/)
  assert.ok(!out.includes("sk-xyz"), "apiKey-Wert darf nicht im Log stehen")
  assert.ok(!out.includes("Bearer abc"), "authorization darf nicht im Log stehen")
})

await t("normale Payload bleibt 1:1", () => {
  const payload = { text: "hallo", count: 3, list: [1, 2], ok: true }
  assert.deepStrictEqual(JSON.parse(safeStringify(payload)), payload)
})

await t("undefined → definierter String, kein Wurf", () => {
  assert.strictEqual(safeStringify(undefined), "undefined")
})

await t("Funktion/ unstringifiable → Fallback statt Wurf", () => {
  // JSON.stringify(function) → undefined; darf nicht werfen
  assert.doesNotThrow(() => safeStringify(() => {}))
})

process.exit(failed ? 1 : 0)
