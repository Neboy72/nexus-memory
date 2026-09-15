import { Type } from "@sinclair/typebox"
import type { OpenClawPluginApi } from "openclaw/plugin-sdk"
import type { Embedder } from "../lib/embedder.ts"
import type { QdrantClient } from "../lib/qdrant-client.ts"
import type { NexusConfig } from "../lib/config.ts"
import { clampInt } from "../lib/num.ts"
import { log } from "../logger.ts"

/** Hard bound on search fan-out; mirrored by the schema below. */
const SEARCH_LIMIT_MAX = 50
const SEARCH_LIMIT_DEFAULT = 5

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
          Type.Number({
            description: "Max results (default: 5)",
            minimum: 1,
            maximum: SEARCH_LIMIT_MAX,
          }),
        ),
      }),
      async execute(
        _toolCallId: string,
        params: { query: string; limit?: number },
      ) {
        // H123: never hand a raw host value to Qdrant — clamp to [1, 50].
        const limit = clampInt(params.limit, SEARCH_LIMIT_DEFAULT, 1, SEARCH_LIMIT_MAX)

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
            isError: true,
            content: [
              {
                type: "text" as const,
                text: "Operation failed. Details are in the server log.",
              },
            ],
          }
        }
      },
    },
    { name: toolName },
  )
}