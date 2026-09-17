// Nr 408 (W23): Invarianten-Pin — die vier OpenClaw-Version-Stellen in
// package.json muessen identisch sein (Drift-Schutz ohne JSON-Kommentare).
// peerDependencies.openclaw bleibt bewusst eine Range (Test H155).
//
// W40-Härtung: (1) read+parse beider Files in Helper mit klarem File-Label
// statt nackter ENOENT/SyntaxError; (2) Calver-Extraction über ALLE
// Vorkommen (Anchoring gegen Substring-Funde), Unter-/Obergrenze getrennt
// geprüft; (3) Operator-Pin: pluginApi muss exakt die Untergrenze der
// peerRange inklusive ">="-Operator spiegeln; (4) Lock-Root fehlt =>
// actionable Meldung statt "undefined !== 1.19.11".
import { readFileSync } from "node:fs"
import assert from "node:assert/strict"

// OCR-4: \b alone is satisfied at a '.', so a 4-segment version ("2026.5.7.1")
// matched as "2026.5.7" and a version embedded in another number
// ("1.2026.5.7") was extracted as a bare calver. The lookarounds reject a
// leading word/dot character and a trailing ".<digit>" continuation.
const CALVER = /(?<![\w.])\d{4}\.\d+\.\d+(?!\.\d)(?!\.)(?![\w])/g

function loadJson(url, label) {
  try {
    return JSON.parse(readFileSync(url, "utf8"))
  } catch (err) {
    throw new Error(`${label}: ${err instanceof Error ? err.message : String(err)}`)
  }
}

const pkg = loadJson(new URL("./package.json", import.meta.url), "plugins/openclaw/package.json")

const spots = [
  ["peerDependencies.openclaw", pkg.peerDependencies?.openclaw],
  ["openclaw.compat.pluginApi", pkg.openclaw?.compat?.pluginApi],
  ["openclaw.compat.minGatewayVersion", pkg.openclaw?.compat?.minGatewayVersion],
  ["openclaw.build.openclawVersion", pkg.openclaw?.build?.openclawVersion],
]

for (const [k, v] of spots) {
  assert.ok(v, `${k} fehlt in package.json`)
}

// Alle Calver-Vorkommen je Stelle (nicht nur das erste Token):
// eine Range wie ">=2026.5.7 <2027.0.0" traegt Unter- UND Obergrenze —
// beide muessen konsistent bleiben, sonst driftet "<2027.0.0" still weg.
const calvers = new Map(
  spots.map(([k, v]) => [k, [...String(v).matchAll(CALVER)].map((m) => m[0])]),
)
for (const [k, list] of calvers) {
  assert.ok(list.length > 0, `${k} enthaelt keine calver-Version: ${spots.find(([kk]) => kk === k)?.[1]}`)
}

// OCR-5 (bug medium): list[0]/list[1] nahmen die Calver-Range als
// "lower-first" an ("<2027.0.0 >=2026.5.7" machte [0] zur Obergrenze —
// lower falsch, W23-Loop flaggt jeden anderen Spot als Drift, Ordering
// vergleicht upper-vs-upper). Ableitung jetzt über den OPERATOR statt
// der Token-Position: >= (oder ^/~) → Unter-, < → Obergrenze.
const peerRangeStr = spots.find(([k]) => k === "peerDependencies.openclaw")?.[1] ?? ""
const lowerMatch = String(peerRangeStr).match(/(?:\^|~|>=?)\s*(\d{4}\.\d+\.\d+)/)
assert.ok(lowerMatch, `peerRange trägt keine Untergrenze (${peerRangeStr})`)
const lower = lowerMatch[1]
const upperMatch = String(peerRangeStr).match(/<\s*(\d{4}\.\d+\.\d+)/)
// OCR-6 (bug low, L236): the fallback to the second calver token re-introduced
// the positional assumption and returned undefined for malformed uppers
// (`<2026.5`), which silently SKIPPED the empty-range ordering assertion
// below — contrary to the file's fail-loudly policy. If the range carries a
// `<` but no parseable upper, fail with a named assert instead.
if (!upperMatch) {
  assert.ok(
    !/<\s*\d/.test(String(peerRangeStr)),
    `peerRange traegt '<' aber keine gueltige calver-Obergrenze (${peerRangeStr})`,
  )
}
const upper = upperMatch ? upperMatch[1] : calvers.get("peerDependencies.openclaw")[1]
// Untergrenze: identisch ueber alle vier Stellen (die W23-Invariante).
// OCR-6 (bug medium): der Loop verglich noch list[0] positional — dieselbe
// Annahme, die der Operator-Fix oben beseitigte. Upper-first-Ranges
// ("<2027.0.0 >=2026.5.7") faelschten den Drift-Bericht. Jetzt pro Spot:
// jede calver mit vorangestelltem < ist eine Obergrenze, alles andere zählt
// als Untergrenze (>=/^/~ oder plain pin).
for (const [k, list] of calvers) {
  const spotVal = String(spots.find(([kk]) => kk === k)?.[1] ?? "")
  const lowers = []
  for (let i = 0; i < list.length; i++) {
    const before = spotVal.slice(0, spotVal.indexOf(list[i])).match(/<\s*$/)
    if (!before) lowers.push(list[i])
  }
  assert.ok(lowers.length > 0, `${k} trägt keine Untergrenze`)
  assert.strictEqual(lowers[0], lower, `${k} Untergrenze driftet von peerDependencies (${lower})`)
}
console.log("PASS  Nr 408: alle 4 OpenClaw-Version-Stellen auf Basis", lower)

// Operator-Pin: compat.pluginApi muss exakt die Untergrenze MIT Operator
// spiegeln ("gte 2026.5.7" als ">=" + version) — eine Range-vs-Pin-Differenz
// wird so sichtbar statt vom calver-only-Vergleich verschluckt.
const pluginApi = pkg.openclaw.compat.pluginApi
assert.match(pluginApi, /^>=/, "compat.pluginApi muss die peer-Untergrenze als >=-Range tragen")
// OCR-4: plain includes(lower) can never fail here (the W23 loop already
// asserted pluginApi's calver === lower) AND it accepts substrings like
// ">=2026.5.70" for "2026.5.7". Anchor as a whole token instead.
// OCR-6 (bug low, L224): `lower` interpolated into a RegExp unescaped — the
// dots matched any character, so `>=2026x5x7` satisfied the whole-token
// check. Escape the dots so the check pins the exact version token.
const escapedLower = lower.replace(/\./g, "\\.")
assert.ok(
  new RegExp(`^>=${escapedLower}(?:\\s|$)`).test(pluginApi),
  `pluginApi muss die untere peer-Grenze (>=${lower}) exakt tragen`,
)

// Obergrenze der peerRange: muss NACH der Untergrenze liegen — eine
// vertippte/geschrumpfte Upper-Bound (<2026.5.7) wuerde eine leere Range
// bedeuten und ist hier ein harter Fehler. (OCR-5: upper kommt jetzt aus
// der Operator-Ableitung oben statt aus list[1] — Redeclaration entfernt.)
// W40-scan (high): lexikalischer String-Vergleich war falsch ("2026.9.0" >
// "2026.10.0" lexikalisch, aber numerisch kleiner). Segmentweise numerisch
// vergleichen — ein 2-stelliges Minor-Segment (2026.10.0) darf den Test nicht
// fälschlich rot machen.
const segNum = (v) => v.split(".").map((s) => parseInt(s, 10))
if (upper !== undefined) {
  const uSeg = segNum(upper)
  const lSeg = segNum(lower)
  // OCR-4: nested ternary banned — segment loop instead. Pads to the longer
  // version so "2026.10" vs "2026.10.0" compares segment-wise, not by length.
  const width = Math.max(uSeg.length, lSeg.length)
  let greater = false
  for (let i = 0; i < width; i++) {
    const u = uSeg[i] ?? 0
    const l = lSeg[i] ?? 0
    if (u !== l) {
      greater = u > l
      break
    }
  }
  assert.ok(greater, `peer-Range leer (Upper ${upper} <= Lower ${lower})`)
  console.log("PASS  Nr 408: peerRange-Upper-Bound", upper, "> Lower", lower)
}

// Konsistenz package.json <-> package-lock.json (Versions-Sync, drift guard)
const lock = loadJson(new URL("./package-lock.json", import.meta.url), "plugins/openclaw/package-lock.json")
const lockRoot = lock.packages?.[""]
// Fund: fehlender Lock-Root lieferte "undefined !== 1.19.11" ohne Hinweis
assert.ok(lockRoot, "package-lock.json hat keinen packages['']-Root-Eintrag (Format/Generation pruefen)")
assert.strictEqual(lock.version, pkg.version, "package-lock version driftet von package.json")
assert.strictEqual(lockRoot.version, pkg.version, "lockfile root package version driftet")
// Name-Drift (W34-Fund): lock root name muss dem package name entsprechen —
// eine abweichende Lock-Identity waere ein stiller npm-install/CI-Drift.
assert.strictEqual(lock.name, pkg.name, "package-lock root name driftet von package.json name")
assert.strictEqual(lockRoot.name, pkg.name, "lockfile root packages.name driftet")
console.log("PASS  Nr 408: package-lock version+name = package.json =", pkg.version, pkg.name)

// peerRange-Upper-Bound (W34-Fund b): die peerDependency-Range muss
// VOLLSTAENDIG zwischen package.json und der gespiegelten Lock-Angabe
// uebereinstimmen — nicht nur das erste Token.
// W40-scan (medium): still schweigen (if-Guard) verschluckt einen fehlenden
// Mirror — fehlt die Spiegelung, ist das ein harter Fehler.
const peerRange = pkg.peerDependencies.openclaw
assert.ok(lockRoot.peerDependencies?.openclaw, "package-lock root spiegelt peerDependencies.openclaw nicht (Drift oder Format)")
assert.strictEqual(lockRoot.peerDependencies.openclaw, peerRange, "peerRange in package-lock driftet von package.json")
console.log("PASS  Nr 408: package-lock peerRange gespiegelt:", peerRange)