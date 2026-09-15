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

/**
 * Semver compare without dependencies. Returns true when remote > local.
 *
 * ClawHub package versions are offset from the GitHub release line: package
 * major = GitHub major + 1 (package 1.18.7 ships GitHub 0.18.7). While the
 * GitHub release is still on 0.x, the local package major is reduced by 1
 * before comparing, so local 1.18.7 vs remote 0.18.7 compares as equal (no
 * false update nudge) and a real 0.x bump such as 0.19.0 is still detected.
 * Once GitHub reaches 1.x the offset no longer applies and a plain semver
 * comparison is used.
 */
export function isNewerVersion(remote: string, local: string): boolean {
  // A component that is not a whole number (e.g. "1.2.x", "1.2.beta") makes
  // the WHOLE comparison fail-open (false) instead of being masked to 0 —
  // `parseInt(...) || 0` turned "1.x.0" into "1.0.0" and could report an
  // update that does not exist (or hide a real one). Missing trailing
  // components are padded with 0 (unchanged behaviour).
  const parse = (v: string): number[] | null => {
    const segments = v.replace(/^v/, "").split(".")
    const out: number[] = []
    for (const segment of segments) {
      const n = Number(segment)
      if (!Number.isInteger(n)) return null
      out.push(n)
    }
    while (out.length < 3) out.push(0)
    return out
  }
  const remoteParts = parse(remote)
  const localParts = parse(local)
  if (remoteParts === null || localParts === null) return false
  const [rMajor, rMinor, rPatch] = remoteParts
  const [lMajor0, lMinor, lPatch] = localParts
  let lMajor = lMajor0
  if (rMajor === 0 && lMajor > 0) {
    // Package major = GitHub major + 1 while GitHub is on 0.x.
    lMajor -= 1
  }
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
    const cached = JSON.parse(raw) as Partial<CachedCheck>
    // Shape check, not just TTL: a malformed cache (e.g. an object where a
    // string is expected) must be ignored, not handed to isNewerVersion.
    if (
      typeof cached.checkedAt === "number" &&
      typeof cached.latest === "string" &&
      typeof cached.url === "string" &&
      Date.now() - cached.checkedAt < CACHE_TTL_MS
    ) {
      return cached as CachedCheck
    }
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
  // A 403 (rate limit) or 404 must NOT be cached as "latest: ''". Throw so
  // checkForUpdate reports "no update" without poisoning the 24h cache.
  if (!res.ok) throw new Error(`GitHub API ${res.status}`)
  const data = (await res.json()) as { tag_name?: string; html_url?: string }
  const rawTag = (data.tag_name ?? "").replace(/^v/, "")
  // Only accept a semver-looking tag; anything else becomes "" so a garbage
  // tag can never be compared/cached as a version.
  const latest = /^[0-9]+\.[0-9]+\.[0-9]+/.test(rawTag) ? rawTag : ""
  return {
    checkedAt: Date.now(),
    latest,
    url: data.html_url ?? "",
  }
}

export interface UpdateCheckResult {
  available: boolean
  latest: string
  url: string
}

// In-flight coalescing: while a check is running every caller shares the same
// promise, so N concurrent calls still cause exactly ONE GitHub fetch (and
// exactly one cache write).
let pendingCheck: Promise<UpdateCheckResult> | null = null

/** Fire-and-forget update check. Resolves once (fresh or from cache). */
export async function checkForUpdate(): Promise<UpdateCheckResult> {
  if (pendingCheck) return pendingCheck
  pendingCheck = checkForUpdateOnce().finally(() => {
    pendingCheck = null
  })
  return pendingCheck
}

async function checkForUpdateOnce(): Promise<UpdateCheckResult> {
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
  // Fail-open contract: ANY problem — including a malformed cached value —
  // must yield "no update", never throw. (entry.latest is a validated string
  // via readCache, but keep the guard local so the compare can never escape.)
  let available = false
  try {
    available = isNewerVersion(entry.latest, local)
  } catch {
    available = false
  }
  return {
    available,
    latest: typeof entry.latest === "string" ? entry.latest : local,
    url: entry.url && entry.url.length > 0 ? entry.url : `https://github.com/${REPO}/releases`,
  }
}

/**
 * Strip everything but `v`/digits/`.`/`-`/`+` and spaces from a version tag
 * before it is interpolated into a prompt line. The tag comes from a remote
 * (GitHub) response, so an attacker-influenced value must never be able to
 * carry instructions or markup into the nudge.
 */
export function sanitizeVersionTag(v: string): string {
  return v.trim().replace(/[^v0-9.\-+ ]/g, "")
}

/** Prompt nudge lines (once per process lifetime, tracked by caller). */
export function buildUpdateNudgeLines(result: UpdateCheckResult, nudged: boolean): {
  lines: string[]
  nudged: boolean
} {
  if (!result.available || nudged) return { lines: [], nudged }
  const safeTag = sanitizeVersionTag(result.latest)
  // Nothing version-like survived sanitising → no nudge line at all rather
  // than interpolating an empty/garbage tag.
  if (safeTag.length === 0) return { lines: [], nudged }
  return {
    lines: [
      "",
      `📦 Nexus Memory update available: v${safeTag}. Tell your user about this update and ask if they want to upgrade.`,
    ],
    nudged: true,
  }
}