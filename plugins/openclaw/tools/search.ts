import { Type } from "@sinclair/typebox"
import type { OpenClawPluginApi } from "openclaw/plugin-sdk"
import type { Embedder } from "../lib/embedder.ts"
import type { QdrantClient } from "../lib/qdrant-client.ts"
import type { NexusConfig } from "../lib/config.ts"
import { log } from "../logger.ts"

export function registerSearchTool(
  api: OpenClawPluginApi,
  embedder: Embedder,
  qdrantClient: QdrantClient,
  cfg: NexusConfig,
  toolName = "nexus_search",
): void {
  api.registerTool(
    {
      name: toolName,
      label: "Nexus Memory Search",
      description: "Search through long-term memories in Qdrant for relevant information.",
      parameters: Type.Object({
        query: Type.String({ description: "Search query" }),
        limit: Type.Optional(
          Type.Number({ description: "Max results (default: 5)" }),
        ),
      }),
      async execute(
        _toolCallId: string,
        params: { query: string; limit?: number },
      ) {
        const limit = params.limit ?? 5

        log.debug(`search tool: query="${params.query}" limit=${limit}`)

        try {
          const queryVector = await embedder.embed(params.query)
          const results = await qdrantClient.search(queryVector, limit, cfg.accessLevel)

          if (results.length === 0) {
            return {
              content: [
                { type: "text" as const, text: "No relevant memories found." },
              ],
            }
          }

          const text = results
            .map((r, i) => {
              const score = r.score ? ` (${(r.score * 100).toFixed(0)}%)` : ""
              const category = r.category ? ` [${r.category}]` : ""
              return `${i + 1}. ${r.text}${category}${score}`
            })
            .join("\n")

          return {
            content: [
              {
                type: "text" as const,
                text: `Found ${results.length} memories:\n\n${text}`,
              },
            ],
            details: {
              count: results.length,
              memories: results.map((r) => ({
                id: r.id,
                text: r.text,
                score: r.score,
                access_level: r.access_level,
                category: r.category,
              })),
            },
          }
        } catch (err) {
          log.error("search tool failed", err)
          return {
            content: [
              {
                type: "text" as const,
                text: `Memory search failed: ${err instanceof Error ? err.message : String(err)}`,
              },
            ],
          }
        }
      },
    },
    { name: toolName },
  )
}