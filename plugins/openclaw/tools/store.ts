import { randomUUID } from "node:crypto"
import { Type } from "@sinclair/typebox"
import type { OpenClawPluginApi } from "openclaw/plugin-sdk"
import type { Embedder } from "../lib/embedder.ts"
import type { QdrantClient } from "../lib/qdrant-client.ts"
import type { NexusConfig } from "../lib/config.ts"
import { ScopeCentroidCache, inferScope } from "../lib/scope-auto.ts"
import { log } from "../logger.ts"

const MEMORY_CATEGORIES = ["fact", "belief", "session", "rule", "preference", "temp"] as const
const ACCESS_LEVELS = ["public", "trusted", "private"] as const

export function registerStoreTool(
  api: OpenClawPluginApi,
  embedder: Embedder,
  qdrantClient: QdrantClient,
  cfg: NexusConfig,
  toolName = "nexus_store",
  centroidCache?: ScopeCentroidCache,
): void {
  api.registerTool(
    {
      name: toolName,
      label: "Nexus Memory Store",
      description: "Save important information to long-term memory via Qdrant.",
      parameters: Type.Object({
        text: Type.String({ description: "Information to remember" }),
        category: Type.Optional(
          Type.Unsafe<string>({ type: "string", enum: [...MEMORY_CATEGORIES] }),
        ),
        access_level: Type.Optional(
          Type.Unsafe<string>({ type: "string", enum: [...ACCESS_LEVELS] }),
        ),
        scope: Type.Optional(
          Type.String({
            description:
              "Optional project/agent area label ([a-z0-9-], max 40 chars). " +
              "Scoped memories are excluded from OTHER agents' auto-recall; " +
              "explicit search always finds them. Omit to let the memory " +
              "infer the area automatically (self-organizing).",
          }),
        ),
      }),
      async execute(
        _toolCallId: string,
        params: { text: string; category?: string; access_level?: string; scope?: string },
      ) {
        const category = params.category ?? "fact"
        const accessLevel = (params.access_level ?? cfg.accessLevel) as string
        // Scope normalization ([a-z0-9-], max 40) — fail-open to 'default' on
        // invalid input (same regex as lib/config.ts + server). Explicit
        // param.scope wins; else cfg.scope; else AUTO-infer from centroids
        // (self-organizing memory, Nebo law 07.09) inside the try below.
        const explicit = (params.scope ?? cfg.scope ?? "").trim().toLowerCase()
        let scope = /^[a-z0-9][a-z0-9-]{0,39}$/.test(explicit) ? explicit : ""

        log.debug(
          `store tool: category="${category}" accessLevel="${accessLevel}" textLen=${params.text.length}`,
        )

        try {
          const vector = await embedder.embed(params.text)
          const id = randomUUID()

          // No explicit scope → infer the area from scoped centroids on a
          // CLEAR match; else 'default'. Fail-open, zero cost.
          if (!scope && centroidCache) {
            try {
              scope = inferScope(vector, await centroidCache.get())
            } catch (err) {
              log.debug("scope_auto: store inference failed — default", err)
            }
          }
          if (!scope) scope = "default"

          const payload = {
            text: params.text,
            access_level: accessLevel,
            category,
            source: "openclaw_tool",
            source_url: "",
            confidence: 0.9,
            created_at: new Date().toISOString(),
            scope,
          }

          await qdrantClient.upsert(id, vector, payload)

          const preview =
            params.text.length > 80 ? `${params.text.slice(0, 80)}…` : params.text

          return {
            content: [{ type: "text" as const, text: `Stored: "${preview}"` }],
          }
        } catch (err) {
          log.error("store tool failed", err)
          return {
            content: [
              {
                type: "text" as const,
                text: `Memory store failed: ${err instanceof Error ? err.message : String(err)}`,
              },
            ],
          }
        }
      },
    },
    { name: toolName },
  )
}