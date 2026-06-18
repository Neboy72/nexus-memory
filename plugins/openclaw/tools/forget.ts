import { Type } from "@sinclair/typebox"
import type { OpenClawPluginApi } from "openclaw/plugin-sdk"
import type { Embedder } from "../lib/embedder.ts"
import type { QdrantClient } from "../lib/qdrant-client.ts"
import type { NexusConfig } from "../lib/config.ts"
import { log } from "../logger.ts"

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
            const results = await qdrantClient.searchByVector(queryVector, 5)

            if (results.length === 0) {
              return {
                content: [
                  { type: "text" as const, text: "No matching memory found to forget." },
                ],
              }
            }

            const target = results[0]
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