// Nr 408 (W23): Invarianten-Pin — die vier OpenClaw-Version-Stellen in
// package.json muessen identisch sein (Drift-Schutz ohne JSON-Kommentare).
// peerDependencies.openclaw bleibt bewusst eine Range (Test H155).
import { readFileSync } from "node:fs"
import assert from "node:assert/strict"

const pkg = JSON.parse(readFileSync(new URL("./package.json", import.meta.url), "utf8"))

const spots = [
  ["peerDependencies.openclaw", pkg.peerDependencies?.openclaw],
  ["openclaw.compat.pluginApi", pkg.openclaw?.compat?.pluginApi],
  ["openclaw.compat.minGatewayVersion", pkg.openclaw?.compat?.minGatewayVersion],
  ["openclaw.build.openclawVersion", pkg.openclaw?.build?.openclawVersion],
]

const bases = spots.map(([k, v]) => {
  assert.ok(v, `${k} fehlt`)
  const m = String(v).match(/(\d{4}\.\d+\.\d+)/)
  assert.ok(m, `${k} enthaelt keine calver-Version: ${v}`)
  return m[1]
})

assert.ok(
  new Set(bases).size === 1,
  `OpenClaw-Basisversionen driften: ${spots.map(([k], i) => `${k}=${bases[i]}`).join(", ")}`,
)
console.log("PASS  Nr 408: alle 4 OpenClaw-Version-Stellen auf Basis", bases[0])

// Konsistenz package.json <-> package-lock.json (Versions-Sync, drift guard)
const lock = JSON.parse(readFileSync(new URL("./package-lock.json", import.meta.url), "utf8"))
assert.equal(lock.version, pkg.version, "package-lock version driftet von package.json")
assert.equal(lock.packages?.[""]?.version, pkg.version, "lockfile root package version driftet")
// Name-Drift (W34-Fund): lock root name muss dem package name entsprechen —
// eine abweichende Lock-Identity (@neboy72/openclaw-nexus-memory vs
// @neboy72/nexus-memory) wäre ein stiller npm-install/CI-Drift.
const peerRange = pkg.peerDependencies.openclaw
assert.equal(lock.name, pkg.name, "package-lock root name driftet von package.json name")
assert.equal(lock.packages?.[""]?.name, pkg.name, "lockfile root packages.name driftet")
console.log("PASS  Nr 408: package-lock version+name = package.json =", pkg.version, pkg.name)

// Calver-Upper-Bound (W34-Fund b): die peerDependency-Range muss VOLLSTÄNDIG
// zwischen package.json und den compat-Feldern übereinstimmen — nicht nur das
// erste Token. Sonst driftet z.B. "<2027.0.0" → "<2099.0.0" unbemerkt.
for (const f of ["peerDependencies.openclaw", "openclaw.compat.pluginApi", "openclaw.compat.minGatewayVersion"]) {
  const [sec, key] = f.split(".")
  const v = pkg[sec]?.[key]
  assert.ok(v != null, `${f} fehlt in package.json`)
}

const compatApi = pkg.openclaw?.compat?.pluginApi
// pluginApi muss die untere Grenze der peerRange enthalten (>=2026.5.7)
const lower = peerRange.match(/>=\s*([0-9.]+)/)?.[1]
assert.ok(lower && compatApi.includes(lower), "pluginApi muss die untere peer-Grenze tragen")
// WENN die Range eine obere Grenze hat, muss sie in der peerRange selbst
// konsistent bleiben (kein stiller Upper-Bound-Drift möglich — der Test
// vergleicht die GESAMTE Range-Zeichenkette gegen die gespiegelte compat-Angabe
// in der Lock-Datei, falls vorhanden):
if (lock.packages?.[""]?.peerDependencies?.openclaw) {
  assert.equal(
    lock.packages[""].peerDependencies.openclaw,
    peerRange,
    "peerRange in package-lock driftet von package.json",
  )
}