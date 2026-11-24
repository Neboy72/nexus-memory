import { randomUUID } from "node:crypto"
import { Type } from "@sinclair/typebox"
import type { OpenClawPluginApi } from "openclaw/plugin-sdk"
import type { Embedder } from "../lib/embedder.ts"
import type { MemoryWriter } from "../lib/write-gate.ts"
import type { NexusConfig } from "../lib/config.ts"
import { ScopeCentroidCache } from "../lib/scope-auto.ts"
import { SHARED_SCOPE, scopeBundle } from "../lib/qdrant-client.ts"
import { limitText } from "../lib/text-preview.ts"
import { toolCallWriteRoomShared } from "../lib/turn-context.ts"
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
              "explicit search always finds them. Omit to place the memory " +
              "automatically: a session log lands in this agent's own bundle, " +
              "everything else in the shared area.",
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

        // Write-side room rule (revised 24.11.2026, ClawHub audit): in a room
        // PROVEN shared, "public" is the highest level a write may carry. A
        // call that asks for "trusted" or "private" there is REJECTED — the
        // level is never lowered on its behalf. The 13.11.2026 cap wrote the
        // lowered level and answered "Stored: …", so a caller that asked for
        // "private" got a public entry without being told; that is a silent
        // downgrade of confidentiality, not a courtesy.
        //
        // Rejected means: nothing embedded, nothing stored, no second copy. The
        // answer carries the file's error contract (`isError: true`) and names
        // the room, the ceiling and the two ways out. Only a proven-shared room
        // rejects: an unbound call, an unknown session and a stale marker are
        // left exactly as before (see `toolCallWriteRoomShared`, which never
        // decides on doubt). The read side is untouched.
        const levelOrder = ACCESS_LEVELS as readonly string[]
        const requestedAboveCeiling =
          levelOrder.indexOf(requestedLevel) > levelOrder.indexOf("public")
        if (requestedAboveCeiling && toolCallWriteRoomShared(toolCallId)) {
          return {
            isError: true,
            content: [
              {
                type: "text" as const,
                text:
                  `Memory store rejected: this session runs in a shared room, ` +
                  `where the highest access level is "public"; ` +
                  `"${requestedLevel}" was not stored and nothing was written. ` +
                  `Call again with access level "public" if this note may be ` +
                  `visible to everyone in the room, or write it in your own ` +
                  `chat, where "${requestedLevel}" applies. ` +
                  `(code: shared-room-write-rejected, reason: shared_room, ` +
                  `requested_level: ${requestedLevel}, max_level: public)`,
              },
            ],
          }
        }
        const accessLevel = requestedLevel

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
                  `digits and dashes, max 40 chars). Omit scope to place the ` +
                  `memory automatically (session → own bundle, else shared).`,
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

          // No explicit scope → exactly two targets (15.11.2026): a session
          // log belongs to this agent's own bundle (configured `scope`, else
          // `openclaw`), everything else to the shared area. Never inferred
          // from the centroids — a guessed topic area is not a bundle, and
          // the point would be invisible to everyone, the writer included.
          if (!scope) {
            scope = category === "session" ? scopeBundle(cfg) : SHARED_SCOPE
          }

          // Provenance is mandatory at the gate: a declared source wins, the
          // default names the tool itself (a statement from this conversation).
          const declaredSource =
            typeof rawSource === "string" ? rawSource.trim() : ""
          const payload = {
            // `content` ist das fuehrende Feld und wird von jedem Leser
            // gelesen; `text` bleibt zusaetzlich stehen (Abwaertskompatibilitaet).
            // Der Wiki-Ingest schreibt ueber dieses Werkzeug (source: "wiki") —
            // ohne `content` entstanden 301 Punkte, die nur `text` trugen.
            content: params.text,
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