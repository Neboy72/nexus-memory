/**
 * Owner identity: who is "you" for the room rule.
 *
 * Why this exists (12.11.2026): the room rule (lib/room-state.ts) can only
 * tell the owner apart from a stranger if it knows the owner. Until now that
 * knowledge existed exclusively as hand-written configuration
 * (`ownerUserIds`), so a fresh install had no owner at all — every human
 * message marked its room shared, and the owner's own private chat stayed
 * artificially narrow until someone edited a config file.
 *
 * This module learns the owner from the first HUMAN who writes to the agent — in
 * ANY chat, not only in a one-to-one conversation. Rationale: on a fresh install
 * the first human contact IS the owner. This assumes OpenClaw's own admission
 * rules (`allowFrom`, DM policy, guild allow-lists) already keep strangers away —
 * the plugin does not verify that itself, and a stranger who does get through
 * would be learned as the owner. `ownerUserIds` pins the owner explicitly for
 * setups where that assumption does not hold.
 *
 * The earlier "direct chat only" rule (12.11.2026) never fired in practice:
 * OpenClaw names a Telegram direct session `agent:main:main`, which carries no
 * `:dm:`/`:direct:` segment, so no owner was ever learned and the owner's own
 * room kept being capped (observed live, 18:43:48). `isDirectSessionKey` stays
 * as a helper for diagnostics and tests; it no longer gates learning.
 *
 * PER PLATFORM (Auftrag 15): one person has a different id on every platform —
 * the same human is `5763330319` on Telegram and `373816693522432001` on
 * Discord. A single global id therefore taught the room rule that the owner of
 * one platform is a stranger everywhere else, and every Discord channel of the
 * owner was marked "shared (foreign-author)". Owner state is now a map
 * `platform → id`, learned per platform:
 *
 *     {"version":2,"owners":{"telegram":"5763330319","discord":"373816693522432001"}}
 *
 * The platform key is the `channelId` of the hook context (`telegram`,
 * `discord`, …). A platform that has an owner is never overwritten by a later
 * observation, and a message without a platform never learns anything —
 * otherwise the very same cross-platform mistake would come back.
 *
 * The `version: 1` file (`ownerUserId`, no platform) cannot be attributed to a
 * platform, so it is discarded and noted once in the log; the owner writes
 * again and is re-learned per platform. Applying it globally is exactly the
 * bug this change removes.
 *
 * Precedence: configuration always wins. A learned owner is only ever
 * consulted when `ownerUserIds` is empty, and learning never runs when the
 * config already names an owner — an explicit deployment setting must not be
 * overwritten by an observation.
 *
 * Fail-closed everywhere else: an empty sender, a bot sender or an unreadable
 * state file means "no owner learned", and no owner means every human message
 * marks its room shared.
 */

import { mkdirSync, readFileSync, renameSync, writeFileSync } from "node:fs"
import { homedir } from "node:os"
import { dirname, join } from "node:path"
import { log } from "../logger.ts"
import { authorHash, normalizeId, normalizeOwnerIds } from "./room-state.ts"

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

/** Result of one learning attempt — never a throw. */
export type LearnOwnerResult = {
  learned: boolean
  /** The learned id, or null when nothing was written. */
  ownerUserId: string | null
  /** The platform it was learned for, or "" when nothing was written. */
  platform: string
  /** Why it did (or did not) write: "first-human", "no-platform", "bot", … */
  reason: string
}

/**
 * True when the session key names a one-to-one chat.
 *
 * NOT part of the owner decision any more (module header): it is kept for
 * diagnostics and tests. An absent, empty or non-scalar session key yields
 * false.
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
  // file was dropped and that the owner will be re-learned per platform.
  log.info(
    `owner: ${file} holds a version-1 owner (no platform), discarding it — the owner is re-learned per platform on the next message`,
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

/** True when `ownerUserIds` names at least one owner (config wins over learning). */
export function hasConfiguredOwner(cfg: OwnerConfigLike): boolean {
  return normalizeOwnerIds(cfg?.ownerUserIds).size > 0
}

/**
 * The owner set the room rule compares against: the configured ids if there
 * are any, otherwise the learned ids of ALL platforms.
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

/**
 * Write owner.json atomically: temp file in the same directory (so the rename
 * stays on one filesystem), mode 0600, then rename over the target. A reader
 * therefore sees either the old file or the complete new one, never a
 * half-written map.
 */
function writeOwnerFile(stateDir: unknown, owners: Record<string, string>): void {
  const file = ownerFilePath(stateDir)
  mkdirSync(dirname(file), { recursive: true, mode: 0o700 })
  const record: OwnerRecord = {
    version: 2,
    owners,
    learnedAt: Date.now(),
    reason: "first-human",
  }
  const tmp = `${file}.${process.pid}.tmp`
  writeFileSync(tmp, `${JSON.stringify(record)}\n`, { encoding: "utf8", mode: 0o600 })
  renameSync(tmp, file)
}

/**
 * Learn the owner FOR ONE PLATFORM from the first human message there, in
 * whichever chat it arrives.
 *
 * Writes only when ALL of these hold:
 *   - the sender id is present and scalar
 *   - the sender is not a bot (`isBot === true` refuses; unknown counts as
 *     human, the same fail-closed direction as lib/room-state.ts)
 *   - the message names a platform (`channelId`) — without one the id cannot
 *     be attributed, and applying it everywhere is the bug this replaces
 *   - that platform has no owner yet (an existing entry is never rewritten, so
 *     later messages — a second human included — cannot take the seat)
 *
 * Other platforms are carried over untouched, so learning Discord never loses
 * the Telegram owner (and vice versa).
 *
 * The chat type is deliberately NOT part of the decision — see the module
 * header for why "direct chat only" never fired. The config gate
 * ("ownerUserIds is set → never learn") belongs to the caller: this function
 * has no config parameter, so the hook checks `hasConfiguredOwner(cfg)` before
 * calling.
 *
 * Never throws: a write failure (disk full, read-only state dir) leaves the
 * process without an owner, which caps rooms to public — the safe direction.
 */
export function learnOwnerFromMessage(input: {
  senderId?: unknown
  isBot?: boolean | null
  /** The platform of the message — the hook context's `channelId`. */
  channelId?: unknown
  stateDir?: unknown
}): LearnOwnerResult {
  const skip = (reason: string): LearnOwnerResult => ({
    learned: false,
    ownerUserId: null,
    platform: "",
    reason,
  })

  const senderId = normalizeId(input.senderId)
  if (!senderId) return skip("no-sender")
  if (input.isBot === true) return skip("bot")

  const platform = normalizePlatform(input.channelId)
  if (!platform) return skip("no-platform")

  const existing = readLearnedOwner(input.stateDir)
  if (existing?.owners[platform]) return skip("already-known")

  try {
    writeOwnerFile(input.stateDir, { ...(existing?.owners ?? {}), [platform]: senderId })
  } catch (err) {
    log.warn(
      "owner: could not persist the learned owner — the owner stays unknown and rooms stay capped",
      err,
    )
    return skip("write-failed")
  }

  // Exactly one line, and only a hash of the id — never the id itself.
  log.info(
    `owner: learned the owner for ${platform} from the first human message (owner#${authorHash(senderId)})`,
  )
  return { learned: true, ownerUserId: senderId, platform, reason: "first-human" }
}

/** Test seam: forget which paths were already warned about or noted. */
export function resetOwnerWarnings(): void {
  warnedPaths.clear()
  notedPaths.clear()
}
