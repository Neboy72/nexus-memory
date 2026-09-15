import { Type } from "@sinclair/typebox"
import type { OpenClawPluginApi } from "openclaw/plugin-sdk"
import type { Embedder } from "../lib/embedder.ts"
import type { QdrantClient } from "../lib/qdrant-client.ts"
import type { NexusConfig } from "../lib/config.ts"
import { limitText } from "../lib/text-preview.ts"
import { log } from "../logger.ts"

/**
 * Minimum vector-search score for the "forget by query" path. Vector search
 * ALWAYS returns the nearest hits, however bad — without this threshold an
 * unrelated memory would be silently deleted.
 */
const FORGET_MIN_SCORE = 0.8

export function registerForgetTool(
  api: OpenClawPluginApi,
  embedder: Embedder,
  qdrantClient: QdrantClient,
  _cfg: NexusConfig,
  toolName = "nexus_forget",
): void {
  api.registerTool(
    {
      name: toolName,
      label: "Nexus Memory Forget",
      description:
        "Forget/delete a memory. Provide a memoryId for direct deletion, or a query to find and delete the closest match.",
      parameters: Type.Object({
        memoryId: Type.Optional(
          Type.String({ description: "Direct memory ID to delete" }),
        ),
        query: Type.Optional(
          Type.String({ description: "Search query — finds and deletes the closest match" }),
        ),
      }),
      async execute(
        _toolCallId: string,
        params: { memoryId?: string; query?: string },
      ) {
        const memoryId = params.memoryId
        const query = params.query
        const hasMemoryId = typeof memoryId === "string" && memoryId.length > 0
        const hasQuery = typeof query === "string" && query.length > 0

        // Ambiguous input must never silently pick one of the two paths:
        // `memoryId` takes precedence today, so a stray query would be ignored.
        if (hasMemoryId && hasQuery) {
          return {
            isError: true,
            content: [
              {
                type: "text" as const,
                text: "Provide either memoryId OR query, not both.",
              },
            ],
          }
        }

        // Direct delete by ID
        if (memoryId) {
          log.debug(`forget tool: direct delete id="${memoryId}"`)

          try {
            // Verify the point exists BEFORE deleting: Qdrant's delete is a
            // no-op for an unknown id, so the tool used to report "Memory
            // forgotten." for ids that were never there.
            let existing: { id: string; payload?: Record<string, unknown> } | null = null
            try {
              existing = await qdrantClient.scrollPoint(memoryId)
            } catch (err) {
              // Fail-open on a lookup error: do NOT delete (deleting on an
              // unverified id is the unsafe direction). Not an isError — the
              // caller can retry.
              log.error("forget tool (by ID) lookup failed", err)
              return {
                content: [
                  {
                    type: "text" as const,
                    text: "Memory not found (lookup failed, delete skipped).",
                  },
                ],
              }
            }

            if (existing === null) {
              return {
                content: [
                  { type: "text" as const, text: "Memory not found (id does not exist)." },
                ],
              }
            }

            await qdrantClient.delete(memoryId)
            return {
              content: [{ type: "text" as const, text: "Memory forgotten." }],
            }
          } catch (err) {
            log.error("forget tool (by ID) failed", err)
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
        }

        // Search-then-delete by query
        if (query) {
          log.debug(`forget tool: search-then-delete query="${query}"`)

          try {
            const queryVector = await embedder.embed(query)
            const results = await qdrantClient.searchByVector(queryVector, 5, _cfg.accessLevel)

            if (results.length === 0) {
              return {
                content: [
                  { type: "text" as const, text: "No matching memory found to forget." },
                ],
              }
            }

            const target = results[0]

            // Guard against a low-confidence match: refuse to delete when the
            // best hit is below the threshold, and ask for a precise query or
            // a direct memory_id instead.
            if (target.score < FORGET_MIN_SCORE) {
              return {
                content: [
                  {
                    type: "text" as const,
                    text:
                      `Unsicher (Score ${target.score.toFixed(3)} unter Threshold ` +
                      `${FORGET_MIN_SCORE}) — bitte mit memory_id löschen oder ` +
                      `präziser formulieren.`,
                  },
                ],
              }
            }

            await qdrantClient.delete(target.id)

            const preview = limitText(target.text, 100)
            return {
              content: [{ type: "text" as const, text: `Forgot: "${preview}"` }],
            }
          } catch (err) {
            log.error("forget tool (by query) failed", err)
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
        }

        return {
          content: [
            {
              type: "text" as const,
              text: "Provide a query or memoryId to forget.",
            },
          ],
        }
      },
    },
    { name: toolName },
  )
}