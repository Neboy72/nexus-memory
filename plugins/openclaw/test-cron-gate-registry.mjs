/**
 * Regressionstest: Cron-Form-Gate unabhängig von thoughtFilter (H136, Welle 13)
 *
 * Vorher hing die Registrierung von buildCronFormGateHandler im
 * `if (cfg.thoughtFilter !== false)`-Block. `thoughtFilter: false` schaltete
 * damit unbeabsichtigt das fail-closed Unattended-Send-Gate ab. Jetzt steht
 * die Registrierung in einem eigenen, unbedingten Block.
 *
 * Ansatz (AST-light, dokumentiert): wir lesen index.ts, finden den
 * `if (cfg.thoughtFilter …`-Block per Klammern-Zählung (jede `{`+1, jede `}`-1,
 * Ende bei depth 0) und prüfen, was INNERHALB bzw. AUSSERHALB dieses
 * Textbereichs steht. Kein Parser nötig — die Registrierung ist eine
 * einzelne, eindeutige Zeile.
 */
import assert from "node:assert"
import fs from "node:fs"

let failed = 0
const t = (name, fn) =>
  Promise.resolve()
    .then(fn)
    .then(() => console.log("PASS ", name))
    .catch((e) => {
      failed++
      console.log("FAIL ", name, "—", e.message)
    })

const src = fs.readFileSync(new URL("./index.ts", import.meta.url), "utf8")

function extractBlock(source, marker) {
  const start = source.indexOf(marker)
  assert.ok(start >= 0, `Marker "${marker}" nicht gefunden`)
  const braceStart = source.indexOf("{", start)
  assert.ok(braceStart >= 0, `Keine öffnende Klammer nach "${marker}"`)
  let depth = 0
  let i = braceStart
  for (; i < source.length; i++) {
    if (source[i] === "{") depth++
    else if (source[i] === "}") {
      depth--
      if (depth === 0) break
    }
  }
  assert.ok(depth === 0, "Klammern-Balance im Block nicht gefunden")
  return { block: source.slice(braceStart, i + 1), end: i + 1 }
}

const { block: thoughtFilterBlock, end } = extractBlock(src, "if (cfg.thoughtFilter")
const after = src.slice(end)

await t("thoughtFilter-Block registriert weiterhin den Thought-Filter", () => {
  assert.match(thoughtFilterBlock, /buildThoughtFilterHandler\(\)/)
})

await t("cron-form-gate steht NICHT im thoughtFilter-Block", () => {
  assert.ok(
    !thoughtFilterBlock.includes("buildCronFormGateHandler"),
    "cron-form-gate darf nicht am thoughtFilter-if hängen",
  )
})

await t("cron-form-gate wird unbedingt danach registriert", () => {
  assert.match(
    after,
    /api\.on\(\s*"message_sending",\s*buildCronFormGateHandler\(\)\s*\)/,
    "eigene, unbedingte api.on-Registrierung erwartet",
  )
})

await t("cron-form-gate hat eine eigene, top-level Registrierung (4 Spaces)", () => {
  const line = after
    .split("\n")
    .find((l) => l.includes("buildCronFormGateHandler()") && l.includes("api.on"))
  assert.ok(line, "api.on-Zeile für cron-form-gate nicht gefunden")
  assert.match(line, /^ {4}api\.on\(/, `Registrierung muss auf Handler-Ebene stehen, war: "${line}"`)
})

await t("Reihenfolge: cron-gate kommt nach dem thoughtFilter-Block, vor autoCapture", () => {
  const cronIdx = src.indexOf("buildCronFormGateHandler()")
  assert.ok(cronIdx > end, "cron-gate muss nach dem thoughtFilter-Block stehen")
  assert.ok(
    cronIdx < src.indexOf("if (cfg.autoCapture)"),
    "cron-gate muss vor dem autoCapture-Block stehen",
  )
})

process.exit(failed ? 1 : 0)
