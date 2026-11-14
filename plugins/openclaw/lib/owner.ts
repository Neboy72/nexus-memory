/**
 * Owner identity: who is "you" for the room rule.
 *
 * The room rule (lib/room-state.ts) can only tell the owner apart from a
 * stranger if it knows the owner. This module is the single place that answers
 * "who is the owner", from TWO sources only:
 *
 *   1. configuration — `ownerUserIds`, one entry per platform;
 *   2. an owner file that already exists on disk —
 *      `<stateDir>/nexus/owner.json`, shape `{"version":2,"owners":{…}}`.
 *
 * There used to be a third source: the first HUMAN who wrote to the agent was
 * claimed as the owner, per platform, in any chat. ClawHub's audit of 1.22.3
 * (14.11.2026, MCP07-1) rejected that outright — "first admitted human message
 * can persistently claim platform ownership". The assumption behind it was
 * that OpenClaw's admission rules (`allowFrom`, DM policy, guild allow-lists)
 * already keep strangers away; that assumption is not the plugin's to make,
 * and a stranger who got through once owned the private memories forever.
 *
 * Since 14.11.2026 the plugin therefore never writes this file. It still
 * READS one that exists, so a running installation keeps its owner state
 * instead of silently losing access after an update. Getting an owner is an
 * explicit act: list it in `ownerUserIds`, or place the file yourself. Both
 * are documented in the plugin README, together with the reset (delete the
 * file). The file is never extended, rewritten or repaired by this module.
 *
 * PER PLATFORM: one person has a different id on every platform — the same
 * human is `5763330319` on Telegram and `373816693522432001` on Discord. A
 * single global id taught the room rule that the owner of one platform is a
 * stranger everywhere else, so the file is a map `platform → id`:
 *
 *     {"version":2,"owners":{"telegram":"5763330319","discord":"373816693522432001"}}
 *
 * The platform key is the `channelId` of the hook context (`telegram`,
 * `discord`, …). A `version: 1` file (`ownerUserId`, no platform) cannot be
 * attributed to a platform, so it is discarded and noted once in the log;
 * applying it globally is exactly the bug that change removed.
 *
 * Precedence: configuration always wins over the file.
 *
 * Fail-closed everywhere else: no configuration and no readable file means
 * "owner unknown" — every human message then marks its room shared, and reads
 * stay at "public".
 */

import { readFileSync } from "node:fs"
import { homedir } from "node:os"
import { join } from "node:path"
import { log } from "../logger.ts"
import { normalizeId, normalizeOwnerIds } from "./room-state.ts"

/**
 * A direct chat in a session key: a `direct`/`dm` segment, at the start or
 * after a colon.
 *
 * Diagnostic only since 18:43:48 — see the module header. OpenClaw's session
 * keys for direct chats do not carry the segment (a Telegram DM is
 * `agent:main:main`), so nothing may hinge on this answer.
 *
 * Anchored on the segment so a platform or a group NAME that
 * happens to contain "dm" ("admin:", "dm-archive:group:1") cannot match.
 * Case-insensitive: the SDK lowercases these keys, a foreign host may not.
 */
const DIRECT_SESSION_KEY_PATTERN = /(^|:)(direct|dm):/i

/**
 * A platform key: lowercase slug as the host reports it in `channelId`
 * ("telegram", "discord", "slack"). Anything else — a path, a chat id, an
 * object — is not a platform and never keys an owner (fail-closed).
 */
const PLATFORM_PATTERN = /^[a-z0-9][a-z0-9._-]*$/

/** Anything carrying the config's owner list — the hook passes NexusConfig. */
export type OwnerConfigLike = { ownerUserIds?: unknown } | null | undefined

/** Shape of `<stateDir>/nexus/owner.json` (version 2). */
export type OwnerRecord = {
  version: 2
  /** Platform (`channelId`) → the owner's id on that platform. */
  owners: Record<string, string>
  learnedAt: number
  reason: string
}

/**
 * True when the session key names a one-to-one chat.
 *
 * Diagnostic only: it is kept for tests and log lines. An absent, empty or
 * non-scalar session key yields false.
 */
export function isDirectSessionKey(sessionKey: unknown): boolean {
  const key = normalizeId(sessionKey)
  return key !== "" && DIRECT_SESSION_KEY_PATTERN.test(key)
}

/**
 * A platform key, normalized: trimmed, lowercased, or "" when the host gave
 * nothing usable as a platform.
 */
export function normalizePlatform(raw: unknown): string {
  const value = normalizeId(raw).toLowerCase()
  return PLATFORM_PATTERN.test(value) ? value : ""
}

/**
 * The state directory: explicit argument, then `OPENCLAW_STATE_DIR`, then
 * `~/.openclaw` — the same seat the install script uses.
 */
export function resolveOwnerStateDir(stateDir?: unknown): string {
  const explicit = normalizeId(stateDir)
  if (explicit) return explicit
  const fromEnv = (process.env.OPENCLAW_STATE_DIR ?? "").trim()
  if (fromEnv) return fromEnv
  return join(homedir(), ".openclaw")
}

/** Absolute path of the learned-owner file. */
export function ownerFilePath(stateDir?: unknown): string {
  return join(resolveOwnerStateDir(stateDir), "nexus", "owner.json")
}

/** Paths already warned about — one warning per file per process, not per message. */
const warnedPaths = new Set<string>()

function warnOnce(file: string, err: unknown): void {
  if (warnedPaths.has(file)) return
  warnedPaths.add(file)
  log.warn(
    `owner: could not read ${file} — no owner learned from it, rooms stay capped (${(err as Error)?.message ?? String(err)})`,
  )
}

/** Paths already told about a discarded version-1 record — also once per process. */
const notedPaths = new Set<string>()

function noteDiscardedV1Once(file: string): void {
  if (notedPaths.has(file)) return
  notedPaths.add(file)
  // The id itself is never repeated — the operator only needs to know WHY the
  // file was dropped and what to do instead (there is no automatic re-learning).
  log.warn(
    `owner: ${file} holds a version-1 owner (no platform) and is ignored — set ownerUserIds or rewrite the file as {"version":2,"owners":{"<platform>":"<id>"}}, otherwise reads stay public`,
  )
}

/** The `owners` map of a parsed record, cleaned: platform keys, scalar ids. */
function readOwners(raw: unknown): Record<string, string> {
  const owners: Record<string, string> = {}
  if (raw === null || typeof raw !== "object" || Array.isArray(raw)) return owners
  for (const [key, value] of Object.entries(raw as Record<string, unknown>)) {
    const platform = normalizePlatform(key)
    const id = normalizeId(value)
    if (platform && id) owners[platform] = id
  }
  return owners
}

/**
 * Read the learned owners, or null.
 *
 * A missing file is the normal first-run case (no warning). A version-1 file
 * is discarded with one log line (module header). A file that exists but
 * cannot be read or parsed is reported exactly once and then treated like a
 * missing one — never a throw, never a half-trusted value.
 */
export function readLearnedOwner(stateDir?: unknown): OwnerRecord | null {
  const file = ownerFilePath(stateDir)
  let raw: string
  try {
    raw = readFileSync(file, "utf8")
  } catch (err) {
    // ENOENT = never learned. Anything else (EACCES, EISDIR, …) is a real
    // failure the operator should see once.
    if ((err as NodeJS.ErrnoException)?.code !== "ENOENT") warnOnce(file, err)
    return null
  }

  let parsed: unknown
  try {
    parsed = JSON.parse(raw)
  } catch (err) {
    warnOnce(file, err)
    return null
  }
  if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
    warnOnce(file, new Error("expected an object"))
    return null
  }

  const record = parsed as Record<string, unknown>
  if (record.version === 1 || (record.version === undefined && "ownerUserId" in record)) {
    // No platform → cannot be attributed. Dropped on purpose (module header).
    noteDiscardedV1Once(file)
    return null
  }

  const owners = readOwners(record.owners)
  if (record.version !== 2 || Object.keys(owners).length === 0) {
    warnOnce(file, new Error('expected {"version":2,"owners":{"<platform>":"…"}}'))
    return null
  }

  return {
    version: 2,
    owners,
    learnedAt:
      typeof record.learnedAt === "number" && Number.isFinite(record.learnedAt)
        ? record.learnedAt
        : 0,
    reason: typeof record.reason === "string" ? record.reason : "",
  }
}

/** True when `ownerUserIds` names at least one owner (config wins over the file). */
export function hasConfiguredOwner(cfg: OwnerConfigLike): boolean {
  return normalizeOwnerIds(cfg?.ownerUserIds).size > 0
}

/**
 * True when the owner is known at all — from configuration or from an owner
 * file that already exists.
 *
 * This is the fail-closed switch of the ClawHub audit MCP07-1 fix: with no
 * owner the plugin cannot tell its owner's chat from a stranger's, so READS
 * stay at "public" everywhere. A room that nobody has written in yet has no
 * observation to fall back on — without this flag a never-seen direct chat
 * would still have been served at the configured level.
 */
export function hasKnownOwner(cfg: OwnerConfigLike, stateDir?: unknown): boolean {
  if (hasConfiguredOwner(cfg)) return true
  return readLearnedOwner(stateDir) !== null
}

/**
 * The owner set the room rule compares against: the configured ids if there
 * are any, otherwise the ids of ALL platforms from an existing owner file.
 *
 * All platforms, because a room does not distinguish platforms: the same
 * person writing from Discord is still the owner. When `channelId` is given,
 * that platform's id comes first (it is what `resolveOwnerId` returns), the
 * remaining platforms follow in file order.
 *
 * Empty means "owner unknown" — every human message then marks its room shared.
 */
export function resolveOwnerIds(
  cfg: OwnerConfigLike,
  channelId?: unknown,
  stateDir?: unknown,
): string[] {
  const configured = normalizeOwnerIds(cfg?.ownerUserIds)
  if (configured.size > 0) return [...configured]

  const learned = readLearnedOwner(stateDir)
  if (!learned) return []

  const platform = normalizePlatform(channelId)
  const ids: string[] = []
  const own = platform ? learned.owners[platform] : ""
  if (own) ids.push(own)
  for (const id of Object.values(learned.owners)) {
    if (!ids.includes(id)) ids.push(id)
  }
  return ids
}

/** The primary owner id (first of `resolveOwnerIds`), or null when unknown. */
export function resolveOwnerId(
  cfg: OwnerConfigLike,
  channelId?: unknown,
  stateDir?: unknown,
): string | null {
  return resolveOwnerIds(cfg, channelId, stateDir)[0] ?? null
}

/** Test seam: forget which paths were already warned about or noted. */
export function resetOwnerWarnings(): void {
  warnedPaths.clear()
  notedPaths.clear()
}
