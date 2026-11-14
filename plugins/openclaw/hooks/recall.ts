import { Embedder } from "../lib/embedder.ts"
import type { QdrantClient, SearchResult } from "../lib/qdrant-client.ts"
import type { NexusConfig } from "../lib/config.ts"
import { ScopeCentroidCache, prefetchFilterScopes } from "../lib/scope-auto.ts"
import { neutralizeContextClose, stripNexusContextBlock } from "../lib/prompt-safety.ts"
import { resolveReadLevel, setTurnContext } from "../lib/turn-context.ts"
import { hasKnownOwner, resolveOwnerStateDir } from "../lib/owner.ts"
import { isRoomApproved, isRoomShared, roomKeyOf } from "../lib/room-state.ts"
import { log } from "../logger.ts"
import { isInteractiveTrigger } from "./trigger.ts"

function formatRelativeTime(isoTimestamp: string): string {
  try {
    const dt = new Date(isoTimestamp)
    // An unparseable timestamp yields an Invalid Date whose getTime() is NaN —
    // every comparison below would be false and it would fall through to
    // "NaN undefined, NaN". Signal "no time" instead.
    if (isNaN(dt.getTime())) return ""
    const now = new Date()
    const seconds = (now.getTime() - dt.getTime()) / 1000
    const minutes = seconds / 60
    const hours = seconds / 3600
    const days = seconds / 86400

    if (minutes < 30) return "just now"
    if (minutes < 60) return `${Math.floor(minutes)}mins ago`
    if (hours < 24) return `${Math.floor(hours)} hrs ago`
    if (days < 7) return `${Math.floor(days)}d ago`

    const month = dt.toLocaleString("en", { month: "short" })
    if (dt.getFullYear() === now.getFullYear()) {
      return `${dt.getDate()} ${month}`
    }
    return `${dt.getDate()} ${month}, ${dt.getFullYear()}`
  } catch {
    return ""
  }
}

// Per-category text caps for the injected recall block. Search tools still
// return the full payload; this only limits what is rendered into the prompt.
const CATEGORY_TEXT_CAP: Record<string, number> = {
  entity: 400,
  preference: 600,
  rule: 800,
  fact: 1200,
  session: 2500,
}
const DEFAULT_TEXT_CAP = 1200
const TOTAL_BLOCK_CAP = 12000

function textCapFor(category?: string): number {
  // Object.hasOwn is not the point here: `category` comes from an unvalidated
  // payload, so a value like "constructor" would resolve to an inherited
  // Object.prototype member. The typeof check treats anything that is not a
  // plain number as "no cap known" and falls back to the default.
  const cap = category ? CATEGORY_TEXT_CAP[category] : undefined
  return typeof cap === "number" ? cap : DEFAULT_TEXT_CAP
}

function truncateAtWordBoundary(text: string, maxLen: number): string {
  if (text.length <= maxLen) return text
  let cut = maxLen
  while (cut > 0 && !/[\s]/.test(text[cut])) cut--
  if (cut <= 0) cut = maxLen
  return text.slice(0, cut) + " [...]"
}

function formatMemories(results: SearchResult[], maxResults: number): string | null {
  if (results.length === 0) return null

  // Per-memory cap first, applied after neutralizing a stored closing tag.
  const memories = results.slice(0, maxResults).map((r) => {
    const cap = textCapFor(r.category)
    const text = neutralizeContextClose(r.text ?? "")
    return { ...r, text: truncateAtWordBoundary(text, cap) }
  })

  const intro =
    "The following is background context from Nexus Memory. Use this context silently to inform your understanding — only reference it when the user's message is directly related to something in these memories."
  const disclaimer =
    "Do not proactively bring up memories. Only use them when the conversation naturally calls for it."

  // Global cap: if the whole block exceeds 12 000 chars, drop the worst-scored
  // hits from the back until it fits. Vector results are already sorted by
  // score descending; graph-boost items are appended last and fall off first.
  let keep = memories.length
  let block = ""
  while (keep > 0) {
    const kept = memories.slice(0, keep)
    const lines = kept.map((r) => {
      const timeStr = r.created_at ? formatRelativeTime(r.created_at) : ""
      const pct = r.score != null ? `[${Math.round(r.score * 100)}%]` : ""
      const prefix = timeStr ? `[${timeStr}]` : ""
      const category = r.category ? `[${r.category}]` : ""
      return `- ${prefix}${category} ${r.text} ${pct}`.trim()
    })

    const section = `## Relevant Memories (with relevance %)\n${lines.join("\n")}`
    block = `<nexus-context>\n${intro}\n\n${section}\n\n${disclaimer}\n</nexus-context>`
    if (block.length <= TOTAL_BLOCK_CAP) break
    keep--
  }

  const dropped = memories.length - keep
  log.info(
    `nexus: recall block ${block.length} chars — kept ${keep}/${memories.length} memories, dropped ${dropped} lowest-scored hits`
  )

  return block
}

// stripInboundMetadata moved to lib/prompt-safety.ts (stripNexusContextBlock)
// so recall.ts and capture.ts share one implementation.

/**
 * Graph-Boost: Fetch 1-hop graph neighbors for the top vector search results.
 *
 * For each of the top `maxBoost` results, reads the point's payload edges
 * and fetches the connected facts' content. Returns formatted strings
 * prefixed with [graph:<relation>] so the agent can distinguish graph-
 * boosted results from pure vector hits.
 *
 * Failures are logged and silently skipped — vector results alone are
 * always returned without the graph boost.
 */
async function graphBoost(
  qdrantClient: QdrantClient,
  topResults: SearchResult[],
  maxBoost: number = 3,
  accessLevel: string = "public",
): Promise<string[]> {
  const boosted: string[] = []
  const seenIds = new Set<string>()
  // Payload cache: the same target id is fetched at most once per call, so
  // the graph_traverse-style double fetches share a single lookup.
  const payloadById = new Map<string, Record<string, unknown> | null>()

  const fetchPayload = async (
    id: string,
  ): Promise<Record<string, unknown> | null> => {
    if (payloadById.has(id)) return payloadById.get(id) ?? null
    const point = await qdrantClient.scrollPoint(id)
    const payload = point
      ? ((point.payload ?? {}) as Record<string, unknown>)
      : null
    payloadById.set(id, payload)
    return payload
  }

  try {
    const roots: string[] = []
    for (const r of topResults.slice(0, maxBoost)) {
      const pid = r.id
      if (!pid || seenIds.has(pid)) continue
      seenIds.add(pid)
      roots.push(pid)
    }

    // Root lookups run CONCURRENTLY; one failed lookup must not abort the
    // phase — per-result status is handled below.
    const rootResults = await Promise.allSettled(roots.map((id) => fetchPayload(id)))

    const edges: Array<{ relation: string; targetId: string }> = []
    for (const res of rootResults) {
      if (res.status !== "fulfilled" || !res.value) continue
      const rootEdges = (res.value.edges ?? []) as Array<Record<string, unknown>>
      for (const edge of rootEdges) {
        const edgeStatus = edge.status as string
        if (edgeStatus && edgeStatus !== "active") continue

        const targetId = String(edge.target_fact_id ?? "")
        if (!targetId || seenIds.has(targetId)) continue
        seenIds.add(targetId)
        edges.push({ relation: (edge.relation as string) || "related", targetId })
      }
    }

    const targetResults = await Promise.allSettled(
      edges.map((e) => fetchPayload(e.targetId)),
    )

    const levelOrder = ["public", "trusted", "private"]
    const agentIdx = levelOrder.indexOf(accessLevel)
    for (let i = 0; i < edges.length; i++) {
      const res = targetResults[i]
      if (res.status !== "fulfilled" || !res.value) continue
      const tpPayload = res.value
      // Access-level check: skip memories the agent can't see.
      // Fail-closed: an UNKNOWN (or missing) access_level yields indexOf -1
      // and must be skipped, never treated as public — `-1 > agentIdx` was
      // never true, so the old `|| "public"` made unknown levels visible.
      const tpAccess = tpPayload.access_level as string
      const memIdx = levelOrder.indexOf(tpAccess)
      if (memIdx === -1 || memIdx > agentIdx) continue

      const text = String(tpPayload.content ?? "")
      if (text) {
        boosted.push(`[graph:${edges[i].relation}] ${text.slice(0, 400)}`)
      }
    }
  } catch (err) {
    log.debug("graph boost skipped:", err)
  }

  return boosted
}

export function buildRecallHandler(
  embedder: Embedder,
  qdrantClient: QdrantClient,
  cfg: NexusConfig,
  centroidCache?: ScopeCentroidCache,
) {
  return async (
    event: Record<string, unknown>,
    ctx?: Record<string, unknown>,
  ) => {
    // Group-context privacy cap (Astra-R2 critical finding, 08.09.2026):
    // in group/channel turns the effective access level is capped to "public"
    // regardless of cfg — the agent must never surface private memories into
    // a shared context. Fail-closed: ANY present groupId — including "", a
    // whitespace-only string or 0 — counts as a group turn, mirroring
    // capture.ts. A truthiness check treated those as a DM and skipped the
    // cap, leaking private memories into a shared context. Only a truly
    // ABSENT group (null/undefined) keeps the configured level.
    const rawGroupId = ctx?.groupId
    const isGroupTurn = rawGroupId !== null && rawGroupId !== undefined

    const groupCapActive = isGroupTurn

    // Room observation (09.11.2026, mirrors the Hermes plugin's room rule):
    // `message_received` (hooks/room-observer.ts) records who writes where. A
    // room a stranger has written in stays capped. Since 14.11.2026 (ClawHub
    // audit MCP07-2) that observation can no longer *loosen* anything: a group
    // turn reaches above "public" only when its room is explicitly released in
    // `privateRooms`. Silence in a channel never proves a private audience.
    //
    // The key is derived from the SAME fields on both sides (the
    // conversation/chat id first, the session key only as a fallback) — see
    // lib/room-state.ts: the session key is AGENT-wide in OpenClaw, so keying
    // rooms by it would collapse every chat of one agent into one room.
    const roomKey = roomKeyOf({
      sessionKey: ctx?.sessionKey,
      chatId: ctx?.chatId,
      groupId: rawGroupId,
    })
    const roomShared = isRoomShared(roomKey)
    const roomApproved = isRoomApproved(cfg.privateRooms, roomKey)
    // Owner unknown (no `ownerUserIds`, no owner file) → nothing above "public"
    // is delivered anywhere (ClawHub audit MCP07-1). This has to be a rule of
    // its own: a room nobody has written in yet carries no observation to
    // derive it from.
    const ownerKnown = hasKnownOwner(cfg, resolveOwnerStateDir())

    // One rule for recall and tools (lib/turn-context.ts): owner unknown →
    // public, shared room → public, group turn whose room is not released →
    // public, otherwise the cfg level.
    const effectiveAccessLevel = resolveReadLevel(
      groupCapActive,
      roomShared,
      cfg.accessLevel,
      roomApproved,
      ownerKnown,
    )

    // Turn marker for the TOOLS (ClawHub audit 1.22.0): a registered tool gets
    // no inbound context from the host, so the turn decision is recorded here
    // and read back by nexus_search / nexus_forget / the graph tools
    // (lib/turn-context.ts). Recorded BEFORE the trigger check on purpose — a
    // turn that is not auto-recalled must still refresh the marker, otherwise a
    // stale decision from the previous turn would govern its tools.
    //
    // Since 1.22.4 the marker is stored PER SESSION (ClawHub audit MCP07-3): a
    // turn without a session key records nothing, and its tools then read
    // "public" instead of inheriting whatever turn happened to run last.
    setTurnContext(
      groupCapActive,
      typeof ctx?.sessionKey === "string" ? ctx.sessionKey : undefined,
      Date.now(),
      roomShared,
      roomApproved,
      ownerKnown,
    )

    const trigger = ctx?.trigger as string | undefined
    if (!isInteractiveTrigger(trigger)) {
      return
    }

    const rawPrompt = event.prompt as string | undefined
    if (!rawPrompt || rawPrompt.length < 5) return

    const query = stripNexusContextBlock(rawPrompt)
    if (query.length < 5) return

    log.info(`nexus: before_prompt_build fired — recalling for query (${query.length} chars, accessLevel=${effectiveAccessLevel}${groupCapActive ? `, GROUP-CAP active${roomApproved ? " (room released)" : " (room not released)"}` : ""})`)

    try {
      // Embed the query
      const queryVector = await embedder.embed(query)

      // Search Qdrant with access-level filtering
      const results = await qdrantClient.search(
        queryVector,
        cfg.maxRecallResults,
        effectiveAccessLevel,
      )

      // Scope gating (project/agent areas): auto-recall surfaces only
      // 'default'-scoped memories plus the CURRENTLY allowed area. Allowed
      // set comes from (1) manual cfg.scope override (static, old behavior)
      // or (2) auto-inference from the query itself (self-organizing memory,
      // project rule: full automation — the query steers, no user ever configures).
      // Explicit search (nexus_search tool) is NEVER scope-filtered.
      // Fail-open: centroids empty/ambiguous → no gating at all.
      let allowed: Set<string> | null = null
      if (centroidCache) {
        try {
          const cents = await centroidCache.get()
          allowed = prefetchFilterScopes(queryVector, cents, cfg.scope)
        } catch (err) {
          log.warn("scope_auto: prefetch inference failed — fail-open", err)
        }
      }
      const gated = allowed
        ? results.filter((r) => {
            const s = (r.scope || "default").trim().toLowerCase() || "default"
            return allowed!.has(s)
          })
        : results

      // Graph-boost: add 1-hop neighbors from top 3 vector hits
      const graphItems = (await graphBoost(qdrantClient, gated, 3, effectiveAccessLevel)).slice(0, 5)  // cap to prevent context bloat

      // Merge gated vector results with graph-boosted items
      const allItems: SearchResult[] = [...gated]
      for (const gi of graphItems) {
        allItems.push({
          id: "",
          text: gi,
          // null (not 0): graph-boosted items have no relevance score, and
          // formatMemories skips the "[0%]" badge for `r.score == null`.
          score: null,
          category: "graph",
          source: "graph-boost",
          // A LABEL on a search hit, never a stored value — graphBoost already
          // filtered by effectiveAccessLevel above. It reads "public" only because
          // these items carry no level of their own; labelling them "private" would
          // be just as wrong and could mislead a future filter. Nothing is written here.
          access_level: effectiveAccessLevel,
          created_at: "",
        } as unknown as SearchResult)
      }

      // Budget guard: the cap is cfg.maxRecallResults — NOT plus graphItems,
      // which made the slice a no-op and let graph items exceed the budget.
      const memoryContext = formatMemories(allItems, cfg.maxRecallResults)

      if (!memoryContext) {
        log.info("nexus: no memories to inject")
        return
      }

      log.info(`nexus: injecting context (${allItems.length} raw memories, ${graphItems.length} graph-boosted)`)
      return { prependContext: memoryContext }
    } catch (err) {
      log.error("recall failed", err)
      return
    }
  }
}