import type { NexusConfig } from "../lib/config.ts"
import { log } from "../logger.ts"
import { isRoomShared, observeAuthor, roomKeyOf } from "../lib/room-state.ts"
import {
  hasConfiguredOwner,
  learnOwnerFromMessage,
  resolveOwnerIds,
  resolveOwnerStateDir,
} from "../lib/owner.ts"

/**
 * `message_received` — remember who writes in which room.
 *
 * Why this hook: `before_prompt_build` (where the recall decision is made)
 * carries NO author — PluginHookAgentContext has no sender of the inbound
 * message. `message_received` does (`event.senderId`, `ctx.senderId`), so the
 * observation is made here and the verdict is read back later by
 * `hooks/recall.ts` and by the tools via `lib/turn-context.ts`.
 *
 * Bot detection: the SDK's PluginHookMessageReceivedEvent has no bot flag
 * today (only `before_agent_run` exposes `senderIsOwner`). Every field that a
 * channel or a future SDK might use is read defensively — when none is
 * present the author counts as human, which is the fail-closed direction: an
 * unrecognised author can only make a room MORE private, never less.
 *
 * Owner learning (lib/owner.ts): the first HUMAN who writes — in any chat —
 * becomes the owner OF THAT PLATFORM, persisted as
 * `<stateDir>/nexus/owner.json`. The platform is the context's `channelId`
 * (`telegram`, `discord`, …) — one human has a different id per platform, so a
 * single global id made the owner a stranger everywhere else (Auftrag 15). The
 * chat type is not consulted (see the module header of lib/owner.ts for why
 * the old "direct chat only" rule never fired). It happens BEFORE the
 * observation on purpose, so that same message already counts as the owner and
 * its room is not marked shared. A configured `ownerUserIds` always wins and
 * suppresses learning.
 *
 * The handler never throws and never blocks: it returns void, and the host is
 * free to ignore it.
 */

function readString(source: Record<string, unknown> | undefined, key: string): string {
  if (!source) return ""
  const value = source[key]
  if (typeof value === "string") return value.trim()
  if (typeof value === "number" && Number.isFinite(value)) return String(value)
  return ""
}

/** Any truthy-ish bot signal the host or channel may attach — never guessed. */
function readBotFlag(event: Record<string, unknown>): boolean | undefined {
  const metadata = (event.metadata ?? {}) as Record<string, unknown>
  const sender = (event.sender ?? {}) as Record<string, unknown>
  const metadataSender = (metadata.sender ?? {}) as Record<string, unknown>

  for (const value of [
    event.isBot,
    event.senderIsBot,
    event.fromBot,
    sender.isBot,
    metadata.isBot,
    metadata.senderIsBot,
    metadata.fromBot,
    metadataSender.isBot,
  ]) {
    if (typeof value === "boolean") return value
    if (typeof value === "number" && Number.isFinite(value)) return value !== 0
    if (typeof value === "string") {
      const text = value.trim().toLowerCase()
      if (text === "true" || text === "1" || text === "yes") return true
      if (text === "false" || text === "0" || text === "no") return false
    }
  }
  return undefined
}

/** The author of an inbound message: event first, then the hook context. */
function readAuthorId(
  event: Record<string, unknown>,
  ctx: Record<string, unknown> | undefined,
): string {
  const sender = (event.sender ?? {}) as Record<string, unknown>
  const metadata = (event.metadata ?? {}) as Record<string, unknown>
  return (
    readString(event, "senderId") ||
    readString(ctx, "senderId") ||
    readString(sender, "id") ||
    readString(metadata, "senderId")
  )
}

export function buildMessageReceivedHandler(cfg: NexusConfig) {
  return (event: Record<string, unknown>, ctx?: Record<string, unknown>) => {
    try {
      const roomKey = roomKeyOf({
        sessionKey: ctx?.sessionKey ?? event.sessionKey,
        chatId: ctx?.chatId,
        groupId: ctx?.groupId,
        threadId: event.threadId,
      })
      if (!roomKey) return

      const authorId = readAuthorId(event, ctx)
      const isBot = readBotFlag(event)
      // The platform of this message: `channelId` is the provider slug
      // ("telegram", "discord"), NOT a chat id (see lib/room-state.ts).
      const channelId = readString(ctx, "channelId") || readString(event, "channelId")

      // Learn the owner BEFORE observing this author (see the module header):
      // only while the config names no owner — `ownerUserIds` must never be
      // overwritten by an observation. Within the learned state the platform
      // decides: a platform that already has an owner is never re-learned.
      const stateDir = resolveOwnerStateDir()
      if (!hasConfiguredOwner(cfg)) {
        learnOwnerFromMessage({ senderId: authorId, isBot, channelId, stateDir })
      }

      // The owner set the room rule compares against: config, else the learned
      // ids of every platform, else empty (owner unknown → any human marks the
      // room shared).
      const ownerIds = resolveOwnerIds(cfg, channelId, stateDir)

      const before = isRoomShared(roomKey)
      const state = observeAuthor(roomKey, authorId, isBot, ownerIds)
      if (state && before !== true && state.shared) {
        log.info(
          `room-state: ${roomKey} marked shared (${state.reason}) — reads there are capped to public`,
        )
      }
    } catch (err) {
      // Fail-open for the turn, fail-closed for privacy: a room we could not
      // observe stays "unknown", and unknown already reads as public.
      log.warn("room-state: observation failed — room stays unknown (public)", err)
    }
  }
}
