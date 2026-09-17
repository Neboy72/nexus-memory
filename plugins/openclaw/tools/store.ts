import { randomUUID } from "node:crypto"
import { Type } from "@sinclair/typebox"
import type { OpenClawPluginApi } from "openclaw/plugin-sdk"
import type { Embedder } from "../lib/embedder.ts"
import type { QdrantClient } from "../lib/qdrant-client.ts"
import type { NexusConfig } from "../lib/config.ts"
import { ScopeCentroidCache, inferScope } from "../lib/scope-auto.ts"
import { limitText } from "../lib/text-preview.ts"
import { log } from "../logger.ts"
import { NEXUS_STORE_TOOL } from "../runtime.ts"

const MEMORY_CATEGORIES = ["fact", "belief", "session", "rule", "preference", "temp"] as const
const ACCESS_LEVELS = ["public", "trusted", "private"] as const

/** Scope pattern shared with lib/config.ts, lib/scope-auto.ts and the server. */
const SCOPE_PATTERN = /^[a-z0-9][a-z0-9-]{0,39}$/
const SCOPE_PATTERN_TEXT = "[a-z0-9][a-z0-9-]{0,39}"

export function registerStoreTool(
  api: OpenClawPluginApi,
  embedder: Embedder,
  qdrantClient: QdrantClient,
  cfg: NexusConfig,
  toolName = NEXUS_STORE_TOOL,
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

        // Fail-closed: the JSON schema declares both as enums, but a host that
        // bypasses the schema could smuggle arbitrary values through the
        // runtime cast above. Validate explicitly BEFORE touching embedder or
        // Qdrant. An absent param still falls back to cfg as before.
        if (
          params.category !== undefined &&
          !(MEMORY_CATEGORIES as readonly string[]).includes(params.category)
        ) {
          return {
            content: [
              {
                type: "text" as const,
                text:
                  `Memory store failed: invalid category "${params.category}". ` +
                  `Allowed: ${MEMORY_CATEGORIES.join(", ")}`,
              },
            ],
          }
        }
        if (
          params.access_level !== undefined &&
          !(ACCESS_LEVELS as readonly string[]).includes(params.access_level)
        ) {
          return {
            content: [
              {
                type: "text" as const,
                text:
                  `Memory store failed: invalid access_level "${params.access_level}". ` +
                  `Allowed: ${ACCESS_LEVELS.join(", ")}`,
              },
            ],
          }
        }

        // Scope normalization ([a-z0-9-], max 40; same regex as lib/config.ts,
        // lib/scope-auto.ts and the server).
        //
        // H139: an EXPLICITLY passed scope must be valid — a silently dropped
        // "My Project" / "team_a" / >40-char scope used to land in default (or
        // the auto-inferred area) with the caller still seeing "Stored: …".
        // A scope that merely comes from cfg stays fail-open (as before): one
        // bad global setting must not brick every store call. Either way the
        // success text now echoes the EFFECTIVE values.
        // A blank explicit scope ("" / whitespace) is treated as "omitted", not
        // as an invalid value: callers use "" to mean "no scope given", and the
        // schema documents omitted as "let the area be inferred".
        const trimmedExplicit =
          params.scope === undefined ? null : String(params.scope).trim().toLowerCase()
        const explicitScope = trimmedExplicit ? trimmedExplicit : null
        if (explicitScope !== null && !SCOPE_PATTERN.test(explicitScope)) {
          return {
            // W39 (medium, Test-Fund): hard-fail paths carry the machine-readable
            // error contract (isError:true) — callers must not mistake a rejected
            // scope for a silent success. Mirrors the catch-path contract below.
            isError: true,
            content: [
              {
                type: "text" as const,
                text:
                  `Memory store failed: invalid scope "${params.scope}". ` +
                  `Allowed pattern: ${SCOPE_PATTERN_TEXT} (lowercase letters, ` +
                  `digits and dashes, max 40 chars). Omit scope to let the ` +
                  `area be inferred automatically.`,
              },
            ],
          }
        }

        const cfgScope = (cfg.scope ?? "").trim().toLowerCase()
        let scope =
          explicitScope !== null
            ? explicitScope
            : SCOPE_PATTERN.test(cfgScope)
              ? cfgScope
              : ""

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

          const preview = limitText(params.text, 80)

          // H139: echo the effective values so the caller always sees where
          // the memory actually landed (no more silent default fallback).
          return {
            content: [
              {
                type: "text" as const,
                text:
                  `Stored: "${preview}" ` +
                  `(scope: ${scope}, category: ${category}, access_level: ${accessLevel})`,
              },
            ],
          }
        } catch (err) {
          log.error("store tool failed", err)
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