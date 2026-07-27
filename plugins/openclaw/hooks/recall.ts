import { Embedder } from "../lib/embedder.ts"
import type { QdrantClient, SearchResult } from "../lib/qdrant-client.ts"
import type { NexusConfig } from "../lib/config.ts"
import { log } from "../logger.ts"
import { isInteractiveTrigger } from "./trigger.ts"

function formatRelativeTime(isoTimestamp: string): string {
  try {
    const dt = new Date(isoTimestamp)
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

function formatMemories(results: SearchResult[], maxResults: number): string | null {
  if (results.length === 0) return null

  const memories = results.slice(0, maxResults)

  const lines = memories.map((r) => {
    const timeStr = r.created_at ? formatRelativeTime(r.created_at) : ""
    const pct = r.score != null ? `[${Math.round(r.score * 100)}%]` : ""
    const prefix = timeStr ? `[${timeStr}]` : ""
    const category = r.category ? `[${r.category}]` : ""
    return `- ${prefix}${category} ${r.text} ${pct}`.trim()
  })

  const intro =
    "The following is background context from Nexus Memory. Use this context silently to inform your understanding — only reference it when the user's message is directly related to something in these memories."
  const disclaimer =
    "Do not proactively bring up memories. Only use them when the conversation naturally calls for it."

  const section = `## Relevant Memories (with relevance %)\n${lines.join("\n")}`

  return `<nexus-context>\n${intro}\n\n${section}\n\n${disclaimer}\n</nexus-context>`
}

function stripInboundMetadata(text: string): string {
  if (!text) return text

  // Remove previously injected nexus context tags
  const cleaned = text
    .replace(/<nexus-context>[\s\S]*?<\/nexus-context>\s*/g, "")
    .trim()

  return cleaned
}

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

  try {
    for (const r of topResults.slice(0, maxBoost)) {
      const pid = r.id
      if (!pid || seenIds.has(pid)) continue
      seenIds.add(pid)

      const point = await qdrantClient.scrollPoint(pid)
      if (!point) continue

      const edges = (point.payload?.edges ?? []) as Array<Record<string, unknown>>
      for (const edge of edges) {
        const edgeStatus = edge.status as string
        if (edgeStatus && edgeStatus !== "active") continue

        const targetId = edge.target_fact_id as string
        if (!targetId || seenIds.has(targetId)) continue
        seenIds.add(targetId)

        const targetPoint = await qdrantClient.scrollPoint(targetId)
        if (!targetPoint) continue

        const tpPayload = (targetPoint.payload ?? {}) as Record<string, unknown>
        // Access-level check: skip memories the agent can't see
        const tpAccess = (tpPayload.access_level as string) || "public"
        const levelOrder = ["public", "trusted", "private"]
        const agentIdx = levelOrder.indexOf(accessLevel)
        const memIdx = levelOrder.indexOf(tpAccess)
        if (memIdx > agentIdx) continue

        const text = String(tpPayload.content ?? "")
        if (text) {
          const rel = (edge.relation as string) || "related"
          boosted.push(`[graph:${rel}] ${text.slice(0, 400)}`)
        }
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
) {
  return async (
    event: Record<string, unknown>,
    ctx?: Record<string, unknown>,
  ) => {
    const trigger = ctx?.trigger as string | undefined
    if (!isInteractiveTrigger(trigger)) {
      return
    }

    const rawPrompt = event.prompt as string | undefined
    if (!rawPrompt || rawPrompt.length < 5) return

    const query = stripInboundMetadata(rawPrompt)
    if (query.length < 5) return

    log.info(`nexus: before_prompt_build fired — recalling for query (${query.length} chars, accessLevel=${cfg.accessLevel})`)

    try {
      // Embed the query
      const queryVector = await embedder.embed(query)

      // Search Qdrant with access-level filtering
      const results = await qdrantClient.search(
        queryVector,
        cfg.maxRecallResults,
        cfg.accessLevel,
      )

      // Graph-boost: add 1-hop neighbors from top 3 vector hits
      const graphItems = (await graphBoost(qdrantClient, results, 3, cfg.accessLevel)).slice(0, 5)  // cap to prevent context bloat

      // Merge vector results with graph-boosted items
      const allItems: SearchResult[] = [...results]
      for (const gi of graphItems) {
        allItems.push({
          id: "",
          text: gi,
          score: 0,
          category: "graph",
          source: "graph-boost",
          created_at: "",
        } as SearchResult)
      }

      const memoryContext = formatMemories(allItems, cfg.maxRecallResults + graphItems.length)

      if (!memoryContext) {
        log.info("nexus: no memories to inject")
        return
      }

      log.info(`nexus: injecting context (${memoryContext.length} chars, ${allItems.length} memories, ${graphItems.length} graph-boosted)`)
      return { prependContext: memoryContext }
    } catch (err) {
      log.error("recall failed", err)
      return
    }
  }
}