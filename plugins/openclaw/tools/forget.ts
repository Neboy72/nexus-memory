import { Type } from "@sinclair/typebox"
import type { OpenClawPluginApi } from "openclaw/plugin-sdk"
import type { Embedder } from "../lib/embedder.ts"
import type { QdrantClient } from "../lib/qdrant-client.ts"
import { visibleAccessLevels } from "../lib/qdrant-client.ts"
import type { NexusConfig } from "../lib/config.ts"
import { limitText, PREVIEW_MAX } from "../lib/text-preview.ts"
import { log } from "../logger.ts"

/**
 * Minimum vector-search score for the "forget by query" path. Vector search
 * ALWAYS returns the nearest hits, however bad — without this threshold an
 * unrelated memory would be silently deleted.
 */
// OCR-6 (maintainability low, L655): the confidence threshold is now a
// deployment-tunable config field (forgetMinScore, default 0.8) instead of a
// hardcoded literal.

/**
 * OCR-6 (maintainability low, L664): the lookup-failure envelope was
 * duplicated across the by-id and by-query paths (and a later fix to one copy
 * could drift from the others). One shared error envelope instead.
 */
function lookupFailedEnvelope(logPrefix: string, err: unknown) {
  log.error(`${logPrefix} lookup failed`, err)
  return {
    isError: true as const,
    content: [
      {
        type: "text" as const,
        text: "Memory lookup failed (Qdrant error), delete skipped. Retry shortly.",
      },
    ],
  }
}

export function registerForgetTool(
  api: OpenClawPluginApi,
  embedder: Embedder,
  qdrantClient: QdrantClient,
  cfg: NexusConfig,
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
        // OCR-5 (bug low): whitespace-only values ("   ") passed `.length > 0`
        // and hit the lookup/delete path with a meaningless id/query. Trim
        // before the emptiness check so a blank string is "missing" like it
        // should be.
        // OCR-6 (bug medium): the guards trimmed, but the DISPATCH used the raw
        // values — `"   "` with a real query passed hasMemoryId===false yet was
        // truthy in `if (memoryId)`, silently taking the direct-delete path and
        // ignoring the query; a padded id/query went verbatim to
        // scrollPointStrict/delete/embed. Normalize once, use everywhere.
        const normMemoryId = typeof memoryId === "string" ? memoryId.trim() : undefined
        const normQuery = typeof query === "string" ? query.trim() : undefined
        const hasMemoryId = typeof normMemoryId === "string" && normMemoryId.length > 0
        const hasQuery = typeof normQuery === "string" && normQuery.length > 0

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
        if (normMemoryId) {
          log.debug(`forget tool: direct delete id="${normMemoryId}"`)

          try {
            // Verify the point exists BEFORE deleting: Qdrant's delete is a
            // no-op for an unknown id, so the tool used to report "Memory
            // forgotten." for ids that were never there.
            // scrollPointStrict (not scrollPoint) is used so a transient
            // Qdrant failure throws instead of returning null — otherwise a
            // 500/403/network error was indistinguishable from a 404 and the
            // caller was told "id does not exist" (unrecoverable-looking) for
            // what is actually a retryable outage.
            let existing: { id: string; payload?: Record<string, unknown> } | null = null
            try {
              existing = await qdrantClient.scrollPointStrict(normMemoryId)
            } catch (err) {
              // Fail-closed on a lookup error: do NOT delete (deleting on an
              // unverified id is the unsafe direction). The caller can retry.
              // OCR-5 (bug medium): this branch returned WITHOUT isError —
              // MCP clients that only inspect isError treated a failed,
              // skipped delete as a successful outcome (the only signal was
              // prose). A skipped delete is a FAILURE: mark it.
              return lookupFailedEnvelope("forget tool (by ID)", err)
            }

            if (existing === null) {
              return {
                content: [
                  { type: "text" as const, text: "Memory not found (id does not exist)." },
                ],
              }
            }

            // OCR-6 (security high): the direct-id path performed NO
            // access-level check — anyone who knew or guessed an id could
            // delete memories outside its visibility scope, while the query
            // path always constrains via cfg.accessLevel. The point payload
            // carries access_level: fail closed (skip) when the memory's
            // level is not visible to this agent.
            const memoryLevel = String(
              (existing.payload as Record<string, unknown> | undefined)?.access_level ?? "",
            )
            if (!visibleAccessLevels(cfg.accessLevel).includes(memoryLevel)) {
              log.warn(
                `forget tool: delete refused — memory access_level "${memoryLevel}" not visible at agent level "${cfg.accessLevel}"`,
              )
              return {
                isError: true,
                content: [
                  {
                    type: "text" as const,
                    text: "Delete refused: this memory is outside your access level.",
                  },
                ],
              }
            }

            await qdrantClient.delete(normMemoryId)
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
        if (normQuery) {
          log.debug(`forget tool: search-then-delete query="${normQuery}"`)

          try {
            const queryVector = await embedder.embed(normQuery)
            const results = await qdrantClient.searchByVector(queryVector, 5, cfg.accessLevel)

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
            if (target.score < cfg.forgetMinScore) {
              // OCR-6 (bug medium): a refused deletion is a non-performed
              // deletion — consistent isError signalling with the other
              // skipped-delete branches.
              return {
                isError: true,
                content: [
                  {
                    type: "text" as const,
                    text:
                      `Unsicher (Score ${target.score.toFixed(3)} unter Threshold ` +
                      `${cfg.forgetMinScore}) — bitte mit memory_id löschen oder ` +
                      `präziser formulieren.`,
                  },
                ],
              }
            }

            // OCR-5 (bug low): the query path deleted optimistically —
            // Qdrant's delete is a NO-OP for an unknown id, so a target found
            // by the earlier recall could vanish (or never exist under this
            // id) and the tool still reported "Forgot". Verify existence
            // right before the irreversible call; a vanished target reports
            // not-found instead of a false success.
            try {
              const stillThere = await qdrantClient.scrollPointStrict(target.id)
              if (stillThere === null) {
                // OCR-6 (bug medium): a vanished target is a non-performed
                // deletion — clients that only inspect isError must not read
                // it as a successful forget (same signalling as the other
                // skipped-delete branches).
                return {
                  isError: true,
                  content: [
                    {
                      type: "text" as const,
                      text: "Memory vanished between search and delete (already gone?). Nothing was deleted.",
                    },
                  ],
                }
              }
            } catch (verifyErr) {
              return lookupFailedEnvelope("forget tool (by query)", verifyErr)
            }
            await qdrantClient.delete(target.id)

            const preview = limitText(target.text, PREVIEW_MAX)
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