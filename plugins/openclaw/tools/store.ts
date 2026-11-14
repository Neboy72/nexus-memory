import { randomUUID } from "node:crypto"
import { Type } from "@sinclair/typebox"
import type { OpenClawPluginApi } from "openclaw/plugin-sdk"
import type { Embedder } from "../lib/embedder.ts"
import type { MemoryWriter } from "../lib/write-gate.ts"
import type { NexusConfig } from "../lib/config.ts"
import { ScopeCentroidCache, inferScope } from "../lib/scope-auto.ts"
import { limitText } from "../lib/text-preview.ts"
import { toolCallWriteAccessLevel } from "../lib/turn-context.ts"
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
  writer: MemoryWriter,
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
        source: Type.Optional(
          Type.String({
            description:
              "Where the text came from: 'paperless', 'wiki', 'document', " +
              "'web', 'mail' ... for imported material; omit for a statement " +
              "made in this conversation. The write gate uses this to set the " +
              "access level.",
          }),
        ),
        rule_proof: Type.Optional(
          Type.String({
            description:
              "Only for category 'rule': the token a HUMAN generated with " +
              "'rule-confirm \"<text>\"' at a terminal (valid 10 minutes, " +
              "single use, bound to that text). Without it a rule write is " +
              "stored as 'fact'. You cannot produce this token yourself.",
          }),
        ),
      }),
      async execute(
        toolCallId: string,
        params: {
          text: string
          category?: string
          access_level?: string
          scope?: string
          source?: string
          rule_proof?: string
        },
      ) {
        const category = params.category ?? "fact"
        const requestedLevel = (params.access_level ?? cfg.accessLevel) as string

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

        // Fail-closed on a non-string source too: the write gate derives the
        // access level and the rule-origin verdict from it, so a smuggled
        // object/array must not reach the payload.
        const rawSource: unknown = params.source
        if (rawSource !== undefined && typeof rawSource !== "string") {
          return {
            isError: true,
            content: [
              {
                type: "text" as const,
                text: 'Memory store failed: source must be a string (e.g. "paperless", "wiki", "chat").',
              },
            ],
          }
        }

        // Write-side room cap (13.11.2026): the level a WRITE may carry is
        // capped by the turn's room — in a shared room "public" is the ceiling,
        // even when the call asks for "private". A registered tool gets no
        // inbound context from the host, so the room verdict comes from the
        // session this very call was bound to by `before_tool_call`
        // (`toolCallWriteAccessLevel`, see lib/turn-context.ts). Never a
        // rejection: only the level is lowered. The requested level itself is
        // the baseline, NOT cfg.accessLevel: only a provably shared room caps,
        // so an unbound call or a stale marker leaves the level untouched.
        const roomLevel = toolCallWriteAccessLevel(requestedLevel, toolCallId)
        const levelOrder = ACCESS_LEVELS as readonly string[]
        const accessLevel =
          levelOrder.indexOf(requestedLevel) > levelOrder.indexOf(roomLevel)
            ? roomLevel
            : requestedLevel

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

          // Provenance is mandatory at the gate: a declared source wins, the
          // default names the tool itself (a statement from this conversation).
          const declaredSource =
            typeof rawSource === "string" ? rawSource.trim() : ""
          const payload = {
            text: params.text,
            access_level: accessLevel,
            category,
            source: declaredSource || "openclaw_tool",
            source_url: "",
            confidence: 0.9,
            created_at: new Date().toISOString(),
            scope,
            // Human proof for a `rule` (see tools/store.ts parameters). An
            // automatic capture never carries one, so it can never create a
            // rule on its own.
            rule_proof:
              typeof params.rule_proof === "string" ? params.rule_proof.trim() : "",
          }

          // Every write goes through the shared gate (lib/write-gate.ts):
          // empty text, labels, provenance and the rule-origin check are
          // enforced by the service before anything reaches the collection.
          const written = await writer.upsert(id, vector, payload)

          const preview = limitText(params.text, 80)

          // H139: echo the effective values so the caller always sees where
          // the memory actually landed. The gate may have TIGHTENED the level
          // (a paperless source forces private), so the service's answer wins
          // over the locally computed one when it carries a value.
          const effectiveScope = written?.scope ?? scope
          const effectiveCategory = written?.category ?? category
          const effectiveLevel = written?.access_level ?? accessLevel
          return {
            content: [
              {
                type: "text" as const,
                text:
                  `Stored: "${preview}" ` +
                  `(scope: ${effectiveScope}, category: ${effectiveCategory}, ` +
                  `access_level: ${effectiveLevel})`,
              },
            ],
          }
        } catch (err) {
          // The gate's reason ("leerer Inhalt", "Regel aus einer Dokument-
          // oder Webquelle") must not be echoed to the model: the Nr-369
          // contract keeps tool error responses generic and the detail in the
          // log. `isError: true` is what makes the failure loud.
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