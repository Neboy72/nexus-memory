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
console.log("PASS  Nr 408: package-lock version = package.json version =", pkg.version)