/**
 * Room state: is this chat readable by anyone besides its owner?
 *
 * Why this exists (09.11.2026): the previous gate decided purely on
 * `ctx.groupId` — ANY present group id capped the turn to "public". That is
 * safe but wrong in the other direction: the owner's own Discord channel is
 * `groupId`-bearing and stayed artificially narrow, so private memories never
 * surfaced there.
 *
 * The rule (mirrors the Hermes plugin's room rule — same observation, not a
 * channel list): a room counts as SHARED once a human other than the owner has
 * written in it, and stays shared for the rest of the process. Bots and the
 * owner's own messages never mark a room. Nothing is configured per channel.
 *
 * Where the observations come from (OpenClaw SDK, dist/plugin-sdk/hook-types):
 *   - `message_received` carries the sender — `event.senderId`, plus a bot
 *     flag only when the channel supplies one.
 *   - `before_prompt_build` carries NO sender (PluginHookAgentContext has
 *     sessionKey/chatId/channelId/senderId-ish fields, but no author of the
 *     inbound message), so the decision cannot be made there — it is looked up.
 *
 * Identifiers (see README + the assignment's mapping question):
 *   - `ctx.groupId` on the agent hook context is the SDK's CONVERSATION id
 *     (`OriginatingTo ?? To ?? From`), set only when the inbound message had a
 *     group subject/channel (`src/hooks/message-hook-mappers.ts`,
 *     `deriveInboundMessageHookContext`). It is NOT the guild/space id (that is
 *     `guildId` ← `GroupSpace`) and NOT the thread id.
 *   - `message_received.threadId` is `MessageThreadId` — the thread/topic
 *     INSIDE a chat, a different identifier space.
 *   - `ctx.sessionKey` is declared on BOTH hook contexts (PluginHookAgentContext
 *     and PluginHookMessageContext), but in OpenClaw it names the AGENT's
 *     session, not the chat: a Telegram direct message arrived as
 *     `agent:main:main` (observed live, 18:43:48). Keying rooms by it collapses
 *     every chat of one agent into a single room, so one stranger anywhere
 *     locks all of them. It is the FALLBACK, never the first choice.
 *   - The chat-scoped ids therefore come first: the conversation/chat id, which
 *     both sides derive from the same originating target (`OriginatingTo ?? To
 *     ?? From` on the message hook, `chatId` on the agent hook). The thread id
 *     is only a last resort — and every key is namespaced, so "42" as a thread
 *     can never be confused with "42" as a chat.
 *
 * The state is per process and in memory only: nothing is written to disk, so
 * there is no state file to protect. Only a truncated SHA-256 of an author id
 * is kept — never a name, never the raw id.
 */

import { createHash } from "node:crypto"

export type RoomState = {
  /** True once a stranger has spoken here; never goes back to false. */
  shared: boolean
  /** Why this verdict was recorded ("foreign-author", "owner", "bot", …). */
  reason: string
  /** Epoch ms of the first observation in this process. */
  firstSeen: number
  /** Hash of the last observed author — diagnostic only, never the id. */
  lastAuthorHash: string
}

/** Upper bound on remembered rooms so a long-lived gateway cannot grow forever. */
export const MAX_ROOM_ENTRIES = 1000

const rooms = new Map<string, RoomState>()

/**
 * A trimmed string identity, or "" when the host gave nothing usable.
 *
 * Containers are rejected rather than stringified: `String({})` would invent
 * the room/user "[object Object]" and silently merge unrelated chats into one.
 */
export function normalizeId(raw: unknown): string {
  if (raw === null || raw === undefined) return ""
  if (typeof raw === "object" || typeof raw === "function") return ""
  return String(raw).trim()
}

/** Non-reversible author reference — the state never holds a name or raw id. */
export function authorHash(authorId: string): string {
  return createHash("sha256").update(authorId, "utf8").digest("hex").slice(0, 16)
}

/**
 * Owner ids (config list or a resolved set) → the set compared against.
 *
 * The caller resolves WHAT counts as the owner — since 12.11.2026 that is
 * `lib/owner.ts`: the configured `ownerUserIds` if present, otherwise the id
 * learned from the first direct chat. This module never reads config or disk
 * itself; it just compares the ids it is handed.
 */
export function normalizeOwnerIds(raw: unknown): Set<string> {
  const values = Array.isArray(raw) ? raw : raw === null || raw === undefined ? [] : [raw]
  const owners = new Set<string>()
  for (const value of values) {
    const id = normalizeId(value)
    if (id) owners.add(id)
  }
  return owners
}

export type RoomKeyFields = {
  /** `message_received` ctx: the conversation the message arrived in. */
  conversationId?: unknown
  /** Agent hook ctx: resolved conversation target (mirrors conversationId). */
  chatId?: unknown
  /** Fallback: the group id (itself the conversation id). */
  groupId?: unknown
  /** Canonical session key — AGENT-wide, so only a fallback (see header). */
  sessionKey?: unknown
  /** `message_received`: the thread INSIDE a chat — last resort only. */
  threadId?: unknown
}

/**
 * Name the room an observation belongs to.
 *
 * Order: the conversation/chat id first (chat-scoped), then the session key
 * (AGENT-scoped in OpenClaw — see the header), then the thread id.
 *
 * Prefixed by identifier space on purpose: the same number can be a thread id
 * and a chat id, and treating those as the same room would let a stranger in
 * one thread mark an unrelated chat (or fail to mark this one).
 *
 * `channelId` is deliberately NOT used: on the message hook it is the
 * platform/provider (`telegram`, `discord`), so using it would collapse every
 * chat of a platform into one "room".
 */
export function roomKeyOf(fields: RoomKeyFields): string | null {
  const conversation =
    normalizeId(fields.conversationId) ||
    normalizeId(fields.chatId) ||
    normalizeId(fields.groupId)
  if (conversation) return `chat:${conversation}`

  const sessionKey = normalizeId(fields.sessionKey)
  if (sessionKey) return `session:${sessionKey}`

  return null
}

function remember(
  roomKey: string,
  shared: boolean,
  reason: string,
  authorId: string,
  now: number,
): RoomState {
  const existing = rooms.get(roomKey)
  // A shared room stays shared: the owner writing there later (or a bot) is
  // not evidence that the strangers went away.
  if (existing) {
    if (shared && !existing.shared) {
      existing.shared = true
      existing.reason = reason
    }
    existing.lastAuthorHash = authorHash(authorId)
    return existing
  }
  const state: RoomState = {
    shared,
    reason,
    firstSeen: now,
    lastAuthorHash: authorHash(authorId),
  }
  if (rooms.size >= MAX_ROOM_ENTRIES) {
    // Drop the oldest entry: bounded memory beats unbounded bookkeeping. A
    // forgotten shared room falls back to "unknown" = public on the next turn,
    // which is the fail-closed direction.
    const oldest = rooms.keys().next()
    if (!oldest.done) rooms.delete(oldest.value)
  }
  rooms.set(roomKey, state)
  return state
}

/**
 * Record one observed author in one room.
 *
 * - empty `authorId`          → nothing to learn, nothing recorded
 * - `isBot === true`          → a bot is no foreign reader
 * - author in the owner set   → that is the owner
 * - anything else             → a stranger has spoken: the room is shared
 *
 * `ownerUserIds` is the owner set as RESOLVED by the caller (config, else the
 * learned owner from lib/owner.ts). With NO owner known every human author is
 * a stranger (fail-closed) — see the README section on `ownerUserIds`.
 *
 * Returns the room state, or null when the call carried nothing to record.
 */
export function observeAuthor(
  roomKey: string | null | undefined,
  authorId: unknown,
  isBot?: boolean | null,
  ownerUserIds?: unknown,
  now: number = Date.now(),
): RoomState | null {
  const key = normalizeId(roomKey)
  if (!key) return null
  const author = normalizeId(authorId)
  if (!author) return null

  if (isBot === true) return remember(key, false, "bot", author, now)

  const owners = normalizeOwnerIds(ownerUserIds)
  if (owners.has(author)) return remember(key, false, "owner", author, now)

  return remember(
    key,
    true,
    owners.size > 0 ? "foreign-author" : "unknown-owner",
    author,
    now,
  )
}

/**
 * The verdict for a room: true = shared, false = observed and not shared,
 * null = never observed (unknown → the caller falls back to "public").
 */
export function isRoomShared(roomKey: string | null | undefined): boolean | null {
  const key = normalizeId(roomKey)
  if (!key) return null
  const state = rooms.get(key)
  return state ? state.shared : null
}

/** Diagnostic view of one room. */
export function getRoomState(roomKey: string | null | undefined): RoomState | null {
  const key = normalizeId(roomKey)
  return key ? rooms.get(key) ?? null : null
}

/** Test seam: forget every observed room. */
export function resetRoomState(): void {
  rooms.clear()
}
