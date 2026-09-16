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
const pending = []
const t = (name, fn) => {
  // OCR-5 (maintainability low): the promise was fire-and-forget while the
  // module ended in a bare process.exit() — a genuinely async case could
  // finish after the exit decision (wrong exit code, unflushed stdout).
  const p = Promise.resolve()
    .then(fn)
    .then(() => console.log("PASS ", name))
    .catch((e) => {
      failed++
      console.log("FAIL ", name, "—", e.message, "\n", e.stack)
    })
  pending.push(p)
}

const src = fs.readFileSync(new URL("./index.ts", import.meta.url), "utf8")

// W38 (medium): Braces in String-/Template-Literalen und Kommentaren desynchronisieren
// die Zählung — daher zählt extractBlock auf einer maskierten Kopie (Strings/Comments
// → Spaces), aber schneidet den ORIGINAL-Text aus (Positionen bleiben identisch).
function maskBraces(s) {
  let out = s.split("")
  let i = 0
  while (i < s.length) {
    const c = s[i]
    if (c === "/" && s[i + 1] === "/") { while (i < s.length && s[i] !== "\n") { out[i] = " "; i++ } continue }
    if (c === "/" && s[i + 1] === "*") { out[i] = out[i + 1] = " "; i += 2; while (i < s.length && !(s[i] === "*" && s[i + 1] === "/")) { if (s[i] !== "\n") out[i] = " "; i++ } out[i] = out[i + 1] = " "; i += 2; continue }
    if (c === "\"" || c === "'" || c === "`") {
      const quote = c
      out[i] = " "; i++
      while (i < s.length && s[i] !== quote) { if (s[i] === "\\") { out[i] = " "; i++ } if (i < s.length && s[i] !== "\n") out[i] = " "; i++ }
      if (i < s.length) { out[i] = " "; i++ }
      continue
    }
    i++
  }
  return out.join("")
}

function extractBlock(source, marker) {
  const masked = maskBraces(source)
  const start = masked.indexOf(marker)
  assert.ok(start >= 0, `Marker "${marker}" nicht gefunden (umbenannt/entfernt)`)
  const braceStart = masked.indexOf("{", start)
  assert.ok(braceStart >= 0, `Keine öffnende Klammer nach "${marker}"`)
  let depth = 0
  let i = braceStart
  for (; i < masked.length; i++) {
    if (masked[i] === "{") depth++
    else if (masked[i] === "}") {
      depth--
      if (depth === 0) break
    }
  }
  assert.ok(depth === 0, "Klammern-Balance im Block nicht gefunden (ungewöhnliche Syntax)")
  return { block: source.slice(braceStart, i + 1), end: i + 1, masked }
}

// OCR-5 (bug medium): extractBlock ran at module top level — a renamed
// marker or unbalanced braces threw an UNHANDLED rejection (stack trace,
// no test name, whole suite dead) instead of a clean FAIL line. The
// extraction now runs inside the harness so a broken precondition prints
// FAIL with its test name and the rest of the suite still reports.
let thoughtFilterBlock, end, masked
await t("extractBlock-Sanity: Marker + Klammern-Balance gefunden (top-level-Guard)", () => {
  const r = extractBlock(src, "if (cfg.thoughtFilter")
  thoughtFilterBlock = r.block
  end = r.end
  masked = r.masked
  assert.ok(thoughtFilterBlock.length > 0 && end > 0)
})

await t("thoughtFilter-Block registriert weiterhin den Thought-Filter", () => {
  assert.match(thoughtFilterBlock, /buildThoughtFilterHandler\(\)/)
})

await t("cron-form-gate steht NICHT im thoughtFilter-Block", () => {
  assert.ok(
    !thoughtFilterBlock.includes("buildCronFormGateHandler"),
    "cron-form-gate darf nicht am thoughtFilter-if hängen",
  )
})

// OCR-4 (final): Reihenfolge bleibt Filter-erst/Gate-zuletzt — der
// Tausch-Entwurf wurde durch Kette-Beweise (/tmp/chain-probe2.mjs) als
// fail-open widerlegt und zurückgebaut. Der Test-Vertrag bleibt der
// Original-Kontrakt: Gate nach thoughtFilter-Block, vor autoCapture.
await t("cron-form-gate wird unbedingt registriert (eigenständiger Block)", () => {
  assert.match(
    src,
    /api\.on\(\s*"message_sending",\s*buildCronFormGateHandler\(\)\s*\)/,
    "eigene, unbedingte api.on-Registrierung erwartet",
  )
})

await t("cron-form-gate hat eine eigene, Handler-Ebenen-Registrierung (indent, multi-line-tolerant)", () => {
  // W38 (medium): exakt 4 Spaces bricht bei kosmetischem Reformat — statt dessen:
  // die Registrierung muss als ANWEISUNG mit geringem, einheitlichem indent stehen
  // (nicht tiefer verschachtelt), egal ob sie über eine oder mehrere Zeilen geht.
  // OCR-5 (bug high, /tmp/z285-proof.mjs): das alte Pattern (lazy [\s\S]*? über
  // beliebige Zeilen) matchte ab der ERSTEN api.on-Zeile im File (Zeile 138,
  // before_prompt_build) und vermaß deren Einrückung statt der Gate-Zeile.
  // Jetzt: einzeilige Registrierung, keine Zeilensprünge im Pattern.
  const m = masked.match(/^([ \t]*)api\.on\([^\n]*buildCronFormGateHandler\(\)[^\n]*\)/m)
  assert.ok(m, "unbedingte api.on(...buildCronFormGateHandler...)-Registrierung nicht gefunden")
  const indent = m[1]
  assert.ok(indent.length <= 8, `Registrierung muss auf Handler-Ebene stehen (indent ${indent.length}), war: "${indent.length} spaces"`)
  // OCR-5 (bug medium): der zweite assert prüfte ein magic 200-char-Fenster
  // NACH dem Match (fremde Zeilen) mit regex.test() — die Meldung
  // interpolierte den statischen String statt des Befunds. Das Pattern ist
  // jetzt EINZEILIG ([^\n]* statt [\s\S]*?): Verschachtelung zeigt sich
  // UNMITTELBAR im indent von m[1], der separate Fenster-Assert ist
  // redundant und wurde gestrichen.
})

await t("Reihenfolge: cron-gate kommt nach dem thoughtFilter-Block, vor autoCapture (OCR-4 bewiesen: Gate zuletzt gewinnt Merge)", () => {
  // OCR-5 (bug medium): die Indices mixten zwei Quellen — cronIdx aus dem
  // RAW-src (ein buildCronFormGateHandler()-Mention in String/Kommentar
  // würde gezählt), autoIdx aus der MASKED Kopie, und beide nutzten den
  // ERSTEN Treffer. Jetzt: beide aus `masked`, letzter Treffer (die echte
  // Registrierungszeile) statt erster Mention.
  const cronIdx = masked.lastIndexOf("buildCronFormGateHandler()")
  assert.ok(cronIdx > end, "cron-gate muss NACH dem thoughtFilter-Block stehen (Gate zuletzt = Gate-Urteil gewinnt Replacement-Merge)")
  const autoIdx = masked.indexOf("if (cfg.autoCapture)")
  assert.ok(autoIdx >= 0, `Marker "if (cfg.autoCapture)" nicht gefunden — Index-Check unmöglich (umbenannt?)`)
  assert.ok(
    cronIdx < autoIdx,
    "cron-gate muss vor dem autoCapture-Block stehen",
  )
})

await Promise.all(pending)
process.exitCode = failed ? 1 : 0
