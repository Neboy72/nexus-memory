// Nr 408 (W23): Invarianten-Pin — die vier OpenClaw-Version-Stellen in
// package.json muessen identisch sein (Drift-Schutz ohne JSON-Kommentare).
// peerDependencies.openclaw bleibt bewusst eine Range (Test H155).
//
// W40-Härtung: (1) read+parse beider Files in Helper mit klarem File-Label
// statt nackter ENOENT/SyntaxError; (2) Calver-Extraction über ALLE
// Vorkommen (Anchoring gegen Substring-Funde), Unter-/Obergrenze getrennt
// geprüft; (3) Operator-Pin: pluginApi muss exakt die Unter-Crenze der
// peerRange inklusive ">="-Operator spiegeln; (4) Lock-Root fehlt =>
// actionable Meldung statt "undefined !== 1.19.11".
import { readFileSync } from "node:fs"
import assert from "node:assert/strict"

const CALVER = /\b\d{4}\.\d+\.\d+\b/g

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

const lower = calvers.get("peerDependencies.openclaw")[0]
// Untergrenze: identisch ueber alle vier Stellen (die W23-Invariante)
for (const [k, list] of calvers) {
  assert.strictEqual(list[0], lower, `${k} Unter-Crenze driftet von peerDependencies (${lower})`)
}
console.log("PASS  Nr 408: alle 4 OpenClaw-Version-Stellen auf Basis", lower)

// Operator-Pin: compat.pluginApi muss exakt die Unter-Crenze MIT Operator
// spiegeln ("gte 2026.5.7" als ">=" + version) — eine Range-vs-Pin-Differenz
// wird so sichtbar statt vom calver-only-Vergleich verschluckt.
const pluginApi = pkg.openclaw.compat.pluginApi
assert.match(pluginApi, /^>=/, "compat.pluginApi muss die peer-Untergrenze als >=-Range tragen")
assert.ok(pluginApi.includes(lower), "pluginApi muss die untere peer-Grenze tragen")

// Obergrenze der peerRange (wenn vorhanden): muss NACH der Untergrenze
// liegen — eine vertippte/geschrumpfte Upper-Bound (<2026.5.7) wuerde eine
// leere Range bedeuten und ist hier ein harter Fehler.
// W40-scan (high): lexikalischer String-Vergleich war falsch ("2026.9.0" >
// "2026.10.0" lexikalisch, aber numerisch kleiner). Segmentweise numerisch
// vergleichen — ein 2-stelliges Minor-Segment (2026.10.0) darf den Test nicht
// fälschlich rot machen.
const upper = calvers.get("peerDependencies.openclaw")[1]
const segNum = (v) => v.split(".").map((s) => parseInt(s, 10))
if (upper !== undefined) {
  const [uA, uB, uC] = segNum(upper)
  const [lA, lB, lC] = segNum(lower)
  const greater =
    uA !== lA ? uA > lA : uB !== lB ? uB > lB : uC > lC
  assert.ok(greater, `peer-Range leer (Upper ${upper} <= Lower ${lower})`)
  console.log("PASS  Nr 408: peerRange-Upper-Bound", upper, "> Lower", lower)
}

// Konsistenz package.json <-> package-lock.json (Versions-Sync, drift guard)
const lock = loadJson(new URL("./package-lock.json", import.meta.url), "plugins/openclaw/package-lock.json")
const lockRoot = lock.packages?.[""]
// Fund: fehlender Lock-Root lieferte "undefined !== 1.19.11" ohne Hinweis
assert.ok(lockRoot, "package-lock.json hat keinen packages['']-Root-Eintrag (Format/Generation pruefen)")
assert.equal(lock.version, pkg.version, "package-lock version driftet von package.json")
assert.equal(lockRoot.version, pkg.version, "lockfile root package version driftet")
// Name-Drift (W34-Fund): lock root name muss dem package name entsprechen —
// eine abweichende Lock-Identity waere ein stiller npm-install/CI-Drift.
assert.equal(lock.name, pkg.name, "package-lock root name driftet von package.json name")
assert.equal(lockRoot.name, pkg.name, "lockfile root packages.name driftet")
console.log("PASS  Nr 408: package-lock version+name = package.json =", pkg.version, pkg.name)

// peerRange-Upper-Bound (W34-Fund b): die peerDependency-Range muss
// VOLLSTAENDIG zwischen package.json und der gespiegelten Lock-Angabe
// uebereinstimmen — nicht nur das erste Token.
// W40-scan (medium): still schweigen (if-Guard) verschluckt einen fehlenden
// Mirror — fehlt die Spiegelung, ist das ein harter Fehler.
const peerRange = pkg.peerDependencies.openclaw
assert.ok(lockRoot.peerDependencies?.openclaw, "package-lock root spiegelt peerDependencies.openclaw nicht (Drift oder Format)")
assert.equal(lockRoot.peerDependencies.openclaw, peerRange, "peerRange in package-lock driftet von package.json")
console.log("PASS  Nr 408: package-lock peerRange gespiegelt:", peerRange)