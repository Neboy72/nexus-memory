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

      const memoryContext = formatMemories(results, cfg.maxRecallResults)

      if (!memoryContext) {
        log.info("nexus: no memories to inject")
        return
      }

      log.info(`nexus: injecting context (${memoryContext.length} chars, ${results.length} memories)`)
      return { prependContext: memoryContext }
    } catch (err) {
      log.error("recall failed", err)
      return
    }
  }
}