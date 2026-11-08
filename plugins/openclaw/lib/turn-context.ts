/**
 * Turn context marker (ClawHub audit 1.22.0, 07.11.2026).
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
 * The fix: `before_prompt_build` records the group decision for the current
 * turn here, once, and every reading tool resolves its effective level from
 * it. Fail-closed contract:
 *
 *   - no marker                -> "public" (never the configured level)
 *   - marker older than 120 s  -> "public"
 *   - room observed as shared  -> "public"
 *   - marker says group turn and the room was NOT observed as private -> "public"
 *   - direct chat, no stranger -> the configured level (unchanged behaviour)
 *
 * Since 09.11.2026 the marker also carries the room verdict from
 * `lib/room-state.ts`: a group turn whose room is known to be private (only
 * the owner and bots have written there) may use the configured level. That is
 * the only way this can ever be looser than the old "any groupId = public";
 * every unclear case still resolves to "public".
 *
 * The module state is per process, not per session: nothing in a tool call
 * identifies the session. That is why a marker that cannot be tied to the
 * CURRENT turn (too old) is discarded rather than trusted.
 */

/** How long a turn marker may be used after it was recorded. */
export const TURN_MARKER_TTL_MS = 120_000

export type TurnMarker = {
  isGroup: boolean
  /** Diagnostic only — no tool call carries a session, so it takes no part in the decision. */
  sessionKey?: string
  /**
   * Room verdict from `lib/room-state.ts` at the time of the turn:
   * true = a stranger has written here, false = observed and private,
   * null = never observed (unknown).
   */
  shared: boolean | null
  /** Epoch ms of the `before_prompt_build` call that recorded this marker. */
  at: number
}

let marker: TurnMarker | null = null

/**
 * Record the turn context. Called once per `before_prompt_build` turn.
 *
 * `isGroup` must already be the fail-closed verdict of the caller (any present
 * groupId — including "" — counts as a group). `shared` is the room verdict;
 * omitting it means "unknown", which keeps the old behaviour for callers that
 * do not observe rooms.
 */
export function setTurnContext(
  isGroup: boolean,
  sessionKey?: string,
  at: number = Date.now(),
  shared: boolean | null = null,
): void {
  marker =
    sessionKey === undefined
      ? { isGroup, shared, at }
      : { isGroup, sessionKey, shared, at }
}

/**
 * The current marker, or null when there is none or it is stale.
 *
 * A non-finite timestamp (corrupt marker) yields `now - at = NaN`, the
 * comparison is false and the marker is discarded — fail-closed like the
 * absent case.
 */
export function getTurnContext(now: number = Date.now()): TurnMarker | null {
  if (marker === null) return null
  if (!(now - marker.at < TURN_MARKER_TTL_MS)) return null
  return marker
}

/**
 * The ONE rule that maps (group turn, room verdict) to a read level.
 *
 * Used by the tools (via `toolAccessLevel`) and by `hooks/recall.ts`, so the
 * two can never drift apart:
 *
 *   room shared                    -> "public"
 *   group turn, room NOT private   -> "public"  (unknown counts as not private)
 *   otherwise                      -> the configured level
 */
export function resolveReadLevel(
  isGroupTurn: boolean,
  roomShared: boolean | null,
  cfgAccessLevel: string,
): string {
  if (roomShared === true) return "public"
  if (isGroupTurn && roomShared !== false) return "public"
  return cfgAccessLevel
}

/**
 * The room's access level for READING in THIS turn.
 *
 * In a group turn — and whenever no fresh marker exists — this is always
 * "public": too little is better than a leak. That fail-closed stance is right
 * for reads (the content is shown to someone) but wrong for writes; see
 * `writeAccessLevel` below, which is the only helper a write may use.
 */
export function toolAccessLevel(
  cfgAccessLevel: string,
  now: number = Date.now(),
): string {
  const turn = getTurnContext(now)
  if (turn === null) return "public"
  return resolveReadLevel(turn.isGroup, turn.shared, cfgAccessLevel)
}

/**
 * The level a WRITE may carry — the counterpart of `toolAccessLevel`.
 *
 * A write is capped only when the room is PROVEN shared (`turn.shared === true`).
 * With no marker, a stale marker, or a room whose verdict is unknown, the
 * requested level comes back UNCHANGED.
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
  now: number = Date.now(),
): string {
  const turn = getTurnContext(now)
  if (turn === null) return requested
  if (turn.shared !== true) return requested
  return "public"
}

/** Test seam: drop any recorded marker. */
export function resetTurnContext(): void {
  marker = null
}
