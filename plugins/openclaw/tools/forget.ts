import { Type } from "@sinclair/typebox"
import type { OpenClawPluginApi } from "openclaw/plugin-sdk"
import type { Embedder } from "../lib/embedder.ts"
import type { QdrantClient } from "../lib/qdrant-client.ts"
import type { NexusConfig } from "../lib/config.ts"
import { log } from "../logger.ts"

/**
 * Minimum vector-search score for the "forget by query" path. Vector search
 * ALWAYS returns the nearest hits, however bad — without this threshold an
 * unrelated memory would be silently deleted.
 */
const FORGET_MIN_SCORE = 0.8

function limitText(text: string, max: number): string {
  return text.length > max ? `${text.slice(0, max)}…` : text
}

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
        // Direct delete by ID
        if (params.memoryId) {
          log.debug(`forget tool: direct delete id="${params.memoryId}"`)

          try {
            await qdrantClient.delete(params.memoryId)
            return {
              content: [{ type: "text" as const, text: "Memory forgotten." }],
            }
          } catch (err) {
            log.error("forget tool (by ID) failed", err)
            return {
              content: [
                {
                  type: "text" as const,
                  text: `Forget failed: ${err instanceof Error ? err.message : String(err)}`,
                },
              ],
            }
          }
        }

        // Search-then-delete by query
        if (params.query) {
          log.debug(`forget tool: search-then-delete query="${params.query}"`)

          try {
            const queryVector = await embedder.embed(params.query)
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
              content: [
                {
                  type: "text" as const,
                  text: `Forget failed: ${err instanceof Error ? err.message : String(err)}`,
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