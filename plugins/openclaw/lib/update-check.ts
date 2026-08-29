/**
 * Update check for the OpenClaw Nexus Memory plugin (roadmap 4.8-adjacent,
 * v0.13.1 - parity with Hermes plugin `_check_nexus_update` and the MCP
 * server update check).
 *
 * Behavior:
 *  - Fetches the latest release tag from the GitHub API (10s timeout).
 *  - Compares against the installed plugin version (semver, no deps).
 *  - Fail-open: any error results in "no update" - never breaks recall.
 *  - Cached for 24h so we hit GitHub at most once a day per gateway.
 *  - The prompt nudge is once-per-lifetime (until gateway restart).
 */

import fs from "node:fs"
import path from "node:path"
import { fileURLToPath } from "node:url"

const REPO = "Neboy72/nexus-memory"
const CACHE_FILE = path.join(
  process.env.HOME || "/tmp",
  ".nexus-memory",
  "update-check-cache.json",
)
const CACHE_TTL_MS = 24 * 60 * 60 * 1000

function readInstalledVersion(): string {
  // package.json next to this module (dist/../package.json at runtime)
  try {
    const here = path.dirname(fileURLToPath(import.meta.url))
    const candidates = [
      path.join(here, "..", "package.json"),
      path.join(here, "..", "..", "package.json"),
    ]
    for (const p of candidates) {
      if (fs.existsSync(p)) {
        const pkg = JSON.parse(fs.readFileSync(p, "utf8"))
        if (typeof pkg.version === "string") return pkg.version
      }
    }
  } catch {
    /* fail-open */
  }
  return "0.0.0"
}

/** Semver compare without dependencies. Returns true when remote > local. */
export function isNewerVersion(remote: string, local: string): boolean {
  const parse = (v: string) =>
    v
      .replace(/^v/, "")
      .split(".")
      .map((n) => parseInt(n, 10) || 0)
  const [rMajor, rMinor, rPatch] = parse(remote)
  const [lMajor, lMinor, lPatch] = parse(local)
  if (rMajor !== lMajor) return rMajor > lMajor
  if (rMinor !== lMinor) return rMinor > lMinor
  return rPatch > lPatch
}

interface CachedCheck {
  checkedAt: number
  latest: string
  url: string
}

function readCache(): CachedCheck | null {
  try {
    const raw = fs.readFileSync(CACHE_FILE, "utf8")
    const cached = JSON.parse(raw) as CachedCheck
    if (Date.now() - cached.checkedAt < CACHE_TTL_MS) return cached
  } catch {
    /* no cache / expired */
  }
  return null
}

function writeCache(entry: CachedCheck): void {
  try {
    fs.mkdirSync(path.dirname(CACHE_FILE), { recursive: true })
    fs.writeFileSync(CACHE_FILE, JSON.stringify(entry))
  } catch {
    /* cache is best-effort */
  }
}

async function fetchLatest(): Promise<CachedCheck> {
  const res = await fetch(`https://api.github.com/repos/${REPO}/releases/latest`, {
    headers: { Accept: "application/vnd.github.v3+json", "User-Agent": "openclaw-nexus-memory" },
    signal: AbortSignal.timeout(10_000),
  })
  const data = (await res.json()) as { tag_name?: string; html_url?: string }
  return {
    checkedAt: Date.now(),
    latest: (data.tag_name ?? "").replace(/^v/, ""),
    url: data.html_url ?? "",
  }
}

export interface UpdateCheckResult {
  available: boolean
  latest: string
  url: string
}

/** Fire-and-forget update check. Resolves once (fresh or from cache). */
export async function checkForUpdate(): Promise<UpdateCheckResult> {
  const local = readInstalledVersion()
  let entry = readCache()
  if (!entry) {
    try {
      entry = await fetchLatest()
      writeCache(entry)
    } catch {
      // fail-open: network error -> report nothing available
      return { available: false, latest: local, url: "" }
    }
  }
  const available = isNewerVersion(entry.latest, local)
  return { available, latest: entry.latest, url: entry.url ?? `https://github.com/${REPO}/releases` }
}

/** Prompt nudge lines (once per process lifetime, tracked by caller). */
export function buildUpdateNudgeLines(result: UpdateCheckResult, nudged: boolean): {
  lines: string[]
  nudged: boolean
} {
  if (!result.available || nudged) return { lines: [], nudged }
  return {
    lines: [
      "",
      `📦 Nexus Memory update available: v${result.latest}. Tell your user about this update and ask if they want to upgrade.`,
    ],
    nudged: true,
  }
}