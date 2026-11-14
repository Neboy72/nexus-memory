/**
 * Turn context marker, SESSION-BOUND (ClawHub audit 14.11.2026, MCP07-3).
 *
 * Why this exists: a plugin HOOK receives the inbound context (`ctx.groupId`),
 * but a registered TOOL does not. In this SDK a tool runs as an `AnyAgentTool`
 * — `execute(toolCallId, params, signal?, onUpdate?)` — and the plugin tool
 * context (`OpenClawPluginToolContext`: sessionKey/sessionId/messageChannel/
 * deliveryContext) carries no group id either. The 3-argument
 * `execute(…, ctx: ExtensionContext)` belongs to the EXTENSION api, not to
 * `registerTool`.
 *
 * Consequence before this module: `hooks/recall.ts` and `hooks/capture.ts`
 * enforced the group gate, but `nexus_search`, `nexus_forget` and the graph
 * tools only knew `cfg.accessLevel` — an agent in a group chat could read (and
 * delete) private memories through a tool call.
 *
 * What the audit changed (1.22.4): the marker used to be ONE process-global
 * variable. Two turns running at the same time — two sessions, a nested run, a
 * cron pass next to a chat — overwrote each other, so a tool call in session A
 * could be judged by session B's turn. The marker is now a MAP keyed by the
 * session key, and every reader must present the session it belongs to:
 *
 *   - a tool call is bound to its session by `before_tool_call`
 *     (`pre-tool-gate.ts` → `bindToolSession(toolCallId, ctx.sessionKey)`),
 *     and resolves its level from THAT session's marker only;
 *   - no session key, no binding, an unknown session, or a stale entry
 *     (older than `TURN_MARKER_TTL_MS`)  -> "public", never "the level that
 *     was seen last". There is no fallback to another session's verdict.
 *
 * Fail-closed contract (unchanged in direction, stricter in binding):
 *
 *   - no marker for THIS session        -> "public"
 *   - marker older than 120 s           -> "public"
 *   - room observed as shared           -> "public"
 *   - group turn, room NOT approved     -> "public"
 *   - direct chat, no stranger          -> the configured level
 *
 * Since 14.11.2026 a group turn is only ever allowed above "public" when its
 * room is EXPLICITLY released in `privateRooms` (config, default empty). The
 * old rule inferred privacy from "no stranger has written here yet" — silent
 * readers, members who joined later, foreign bots and threads were invisible
 * to it, so silence was never evidence. `lib/room-state.ts` still records who
 * writes where (diagnostics + the stranger verdict), but it no longer decides
 * on its own that a room may receive private memories.
 */

/** How long a turn marker may be used after it was recorded. */
export const TURN_MARKER_TTL_MS = 120_000

/** Upper bound on remembered sessions, so a long-lived gateway cannot grow forever. */
export const MAX_TURN_ENTRIES = 1000

export type TurnMarker = {
  isGroup: boolean
  /**
   * Room verdict from `lib/room-state.ts` at the time of the turn:
   * true = a stranger has written here, false = observed and private,
   * null = never observed (unknown).
   */
  shared: boolean | null
  /** True when this room is explicitly released in `privateRooms`. */
  approved: boolean
  /**
   * True when the plugin knows who its owner is (ClawHub audit MCP07-1).
   * False means every human author is a stranger, so nothing above "public"
   * may be delivered anywhere.
   */
  ownerKnown: boolean
  /** Epoch ms of the `before_prompt_build` call that recorded this marker. */
  at: number
}

/** Session key → that session's marker. Never a single process-global slot. */
const markers = new Map<string, TurnMarker>()

/** Tool-call id → the session the call belongs to (set by `before_tool_call`). */
const toolSessions = new Map<string, { sessionKey: string; at: number }>()

/** A trimmed, non-empty key, or "" when the host gave nothing usable. */
function normalizeKey(raw: unknown): string {
  if (typeof raw !== "string") return ""
  return raw.trim()
}

/** Drop stale entries; called on every write so the maps stay bounded. */
function prune(now: number): void {
  for (const [key, value] of markers) {
    if (!(now - value.at < TURN_MARKER_TTL_MS)) markers.delete(key)
  }
  for (const [key, value] of toolSessions) {
    if (!(now - value.at < TURN_MARKER_TTL_MS)) toolSessions.delete(key)
  }
  // Hard cap: the oldest insertion goes first. A forgotten session falls back to
  // "unknown" = public on the next turn, which is the fail-closed direction.
  while (markers.size > MAX_TURN_ENTRIES) {
    const oldest = markers.keys().next()
    if (oldest.done) break
    markers.delete(oldest.value)
  }
  while (toolSessions.size > MAX_TURN_ENTRIES) {
    const oldest = toolSessions.keys().next()
    if (oldest.done) break
    toolSessions.delete(oldest.value)
  }
}

/**
 * Record the turn context for ONE session. Called once per
 * `before_prompt_build` turn.
 *
 * A missing or empty session key records NOTHING: a marker no reader can be
 * bound to is exactly the process-global marker this replaced. Those turns are
 * "public" for every tool.
 *
 * `isGroup` must already be the fail-closed verdict of the caller (any present
 * groupId — including "" — counts as a group). `shared` is the room verdict;
 * omitting it means "unknown". `approved` is the `privateRooms` verdict.
 */
export function setTurnContext(
  isGroup: boolean,
  sessionKey?: string,
  at: number = Date.now(),
  shared: boolean | null = null,
  approved: boolean = false,
  ownerKnown: boolean = true,
): void {
  const key = normalizeKey(sessionKey)
  if (!key) return
  markers.set(key, { isGroup, shared, approved: approved === true, ownerKnown: ownerKnown === true, at })
  prune(at)
}

/**
 * The marker of ONE session, or null when there is none, the key is missing,
 * or the entry is stale.
 *
 * A non-finite timestamp (corrupt marker) yields `now - at = NaN`, the
 * comparison is false and the marker is discarded — fail-closed like the
 * absent case.
 */
export function getTurnContext(
  sessionKey?: string,
  now: number = Date.now(),
): TurnMarker | null {
  const key = normalizeKey(sessionKey)
  if (!key) return null
  const marker = markers.get(key)
  if (!marker) return null
  if (!(now - marker.at < TURN_MARKER_TTL_MS)) {
    markers.delete(key)
    return null
  }
  return marker
}

/**
 * Tie one tool call to the session it runs in. Called by `before_tool_call`,
 * which is the only place that knows both the call id and the session key.
 *
 * A missing call id or session key records nothing — the call then reads
 * "public".
 */
export function bindToolSession(toolCallId: unknown, sessionKey: unknown): void {
  const callId = normalizeKey(toolCallId)
  const session = normalizeKey(sessionKey)
  if (!callId || !session) return
  const now = Date.now()
  toolSessions.set(callId, { sessionKey: session, at: now })
  prune(now)
}

/** The session a tool call was bound to, or undefined (fail-closed). */
export function sessionKeyForToolCall(
  toolCallId: unknown,
  now: number = Date.now(),
): string | undefined {
  const callId = normalizeKey(toolCallId)
  if (!callId) return undefined
  const binding = toolSessions.get(callId)
  if (!binding) return undefined
  if (!(now - binding.at < TURN_MARKER_TTL_MS)) {
    toolSessions.delete(callId)
    return undefined
  }
  return binding.sessionKey
}

/**
 * The ONE rule that maps (group turn, room verdict, room release) to a level.
 *
 * Used by the tools (via `toolAccessLevel`) and by the hooks, so the two can
 * never drift apart:
 *
 *   owner unknown                      -> "public"
 *   room shared                        -> "public"
 *   group turn, room NOT released      -> "public"  (unknown counts as not released)
 *   otherwise                          -> the configured level
 */
export function resolveReadLevel(
  isGroupTurn: boolean,
  roomShared: boolean | null,
  cfgAccessLevel: string,
  roomApproved: boolean = false,
  ownerKnown: boolean = true,
): string {
  if (ownerKnown !== true) return "public"
  if (roomShared === true) return "public"
  if (isGroupTurn && roomApproved !== true) return "public"
  return cfgAccessLevel
}

/**
 * The room's access level for READING in THIS session.
 *
 * Without a session key, without a marker for it, and in a group turn whose
 * room is not released, this is always "public": too little is better than a
 * leak. That fail-closed stance is right for reads (the content is shown to
 * someone) but wrong for writes; see `writeAccessLevel` below, which is the
 * only helper a write may use.
 */
export function toolAccessLevel(
  cfgAccessLevel: string,
  sessionKey?: string,
  now: number = Date.now(),
): string {
  const turn = getTurnContext(sessionKey, now)
  if (turn === null) return "public"
  return resolveReadLevel(
    turn.isGroup,
    turn.shared,
    cfgAccessLevel,
    turn.approved,
    turn.ownerKnown,
  )
}

/**
 * `toolAccessLevel` for a TOOL call: resolves the session from the binding
 * `before_tool_call` recorded for this very call id, then applies the same
 * rule. An unbound call has no session and therefore reads "public".
 */
export function toolCallAccessLevel(
  cfgAccessLevel: string,
  toolCallId: unknown,
  now: number = Date.now(),
): string {
  return toolAccessLevel(cfgAccessLevel, sessionKeyForToolCall(toolCallId, now), now)
}

/**
 * The level a WRITE may carry — the counterpart of `toolAccessLevel`.
 *
 * A write is capped only when the room is PROVEN shared (`turn.shared === true`).
 * With no marker, a stale marker, or a room whose verdict is unknown, the
 * requested level comes back UNCHANGED. The room RELEASE is deliberately not
 * consulted: it governs delivery (reads), not what the owner may archive.
 *
 * Why not fail-closed like reads: capping a write changes what is stored in the
 * user's own store. An unknown room would silently turn a private note into a
 * public one — that harms the user's archive instead of protecting it, and it
 * would hit every long work turn, cron run and tool chain that writes more than
 * `TURN_MARKER_TTL_MS` after the last user message. Reads can be narrowed
 * afterwards; a record that was written too openly cannot be un-leaked. So:
 * cap only on evidence, never on doubt. Like the read cap this never rejects —
 * it only lowers the level.
 */
export function writeAccessLevel(
  requested: string,
  sessionKey?: string,
  now: number = Date.now(),
): string {
  const turn = getTurnContext(sessionKey, now)
  if (turn === null) return requested
  if (turn.shared !== true) return requested
  return "public"
}

/** `writeAccessLevel` for a TOOL call, bound through its call id. */
export function toolCallWriteAccessLevel(
  requested: string,
  toolCallId: unknown,
  now: number = Date.now(),
): string {
  return writeAccessLevel(requested, sessionKeyForToolCall(toolCallId, now), now)
}

/** Test seam: drop every recorded marker and tool binding. */
export function resetTurnContext(): void {
  markers.clear()
  toolSessions.clear()
}
