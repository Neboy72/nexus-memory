import { Type } from "@sinclair/typebox"
import type { OpenClawPluginApi } from "openclaw/plugin-sdk"
import type { Embedder } from "../lib/embedder.ts"
import type { QdrantClient } from "../lib/qdrant-client.ts"
import type { NexusConfig } from "../lib/config.ts"
import { clampInt } from "../lib/num.ts"
import { log } from "../logger.ts"
import { NEXUS_SEARCH_TOOL } from "../runtime.ts"

/** Hard bound on search fan-out; mirrored by the schema below. */
const SEARCH_LIMIT_MAX = 50
const SEARCH_LIMIT_DEFAULT = 5

export function registerSearchTool(
  api: OpenClawPluginApi,
  embedder: Embedder,
  qdrantClient: QdrantClient,
  cfg: NexusConfig,
  toolName = NEXUS_SEARCH_TOOL,
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

        // Tool params come from an untrusted host and can bypass the TypeBox
        // schema (the same assumption is guarded in lib/num.ts and
        // tools/store.ts). Validate BEFORE dereferencing: `params.query.length`
        // on a missing/null/non-string query throws a TypeError that escapes
        // execute() entirely — no error result is returned to the caller.
        if (typeof params?.query !== "string" || params.query.trim().length === 0) {
          return {
            isError: true,
            content: [
              {
                type: "text" as const,
                text: "Missing or invalid 'query' — a non-empty string is required.",
              },
            ],
          }
        }
        const query = params.query

        // Never log the query text itself — it is user content and can hold
        // secrets. Length is enough for diagnostics.
        log.debug(`search tool: queryLen=${query.length} limit=${limit}`)

        try {
          const queryVector = await embedder.embed(query)
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
              // Finiteness, not truthiness: a score of 0 is a real value and
              // must render (the old truthiness check dropped it).
              const score =
                typeof r.score === "number" && Number.isFinite(r.score)
                  ? ` (${(r.score * 100).toFixed(0)}%)`
                  : ""
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