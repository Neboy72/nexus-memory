import { randomUUID } from "node:crypto"
import { Type } from "@sinclair/typebox"
import type { OpenClawPluginApi } from "openclaw/plugin-sdk"
import type { QdrantClient } from "../lib/qdrant-client.ts"
import type { NexusConfig } from "../lib/config.ts"
import { log } from "../logger.ts"

// Destructive command patterns
const DESTRUCTIVE_PATTERNS: Array<{ action: string; patterns: RegExp[] }> = [
  { action: "delete", patterns: [/\brm\b.*-r/i, /\brm\b.*-f/i, /\brmdir\b/i, /\bdel\b\s+\/[fsq]/i, /\bdrop\b/i, /\btruncate\b/i, /\buninstall\b/i, /\bfind\b.*-delete/i, /\bgit\b.*clean.*-[fd]/i, /\bdd\b.*\bof\b/i] },
  { action: "kill", patterns: [/\bkill\b.*-9/i, /\bpkill\b/i, /\bkillall\b/i, /\btaskkill\b/i] },
  { action: "overwrite", patterns: [/\bwrite_file\b/i, /(?:^|[\s;&|])\s*>{1,2}\s*[^\s'"&|<>;]*([\/.~][^\s'"&|<>;]*|\.[a-z0-9]{1,6}\b)/i] },
  { action: "recreate", patterns: [/\brecreate_collection\b/i, /DELETE.*collection/i, /\bdrop\b.*collection/i] },
]

const PROTECTION_KEYWORDS = ["never delete", "never remove", "do not delete", "do not remove",
  "protected", "niemals", "verboten", "forbidden", "tabu", "sacred"]

/**
 * W29-2: destructive tools that carry their target in a dedicated path field
 * instead of `command`. The TS mirror of the Python PATH_CARRYING_TOOLS map.
 */
const PATH_CARRYING_TOOLS: Record<string, string> = {
  Write: "file_path",
  Edit: "file_path",
  MultiEdit: "file_path",
  NotebookEdit: "notebook_path",
}

/**
 * Path-ish tokens inside a command or rule text.
 *
 * H124: the bare classic targets `~`, `/` and `.` are matched explicitly.
 * They are exactly the catastrophic ones (`rm -rf /`, `rm -rf ~`) and the
 * previous caller-side `length > 2` guard silently dropped them, so those
 * commands produced zero targets and were allowed.
 */
const PATH_PATTERNS = [
  /\w:[\\/][\w\\./-]+/g, // C:\path or C:/path
  /~(?:\/[\w./-]+)?/g, // ~ or ~/foo/bar
  /(?:^|\s)\/(?=\s|$)/g, // bare / (filesystem root)
  /\/[\w./-]+/g, // /abs/path
  /(?:^|\s)\.(?=\s|$)/g, // bare . (current directory)
]

function classifyAction(command: string): string | null {
  for (const { action, patterns } of DESTRUCTIVE_PATTERNS) {
    for (const pattern of patterns) {
      if (pattern.test(command)) return action
    }
  }
  return null
}

/** Expand a leading `~` (bare or `~/...`) to the home directory. */
function expandHome(path: string): string {
  if (path === "~" || path.startsWith("~/")) {
    return path.replace(/^~/, process.env.HOME || "~")
  }
  return path
}

/**
 * Extract candidate destructive targets from a command string.
 *
 * H124: NO length/classic-path guard any more — `~`, `/`, `.` and one-char
 * paths are kept. If a caller ends up with an empty list it still treats the
 * action as allowed (the "no protected target" branch), but these classic
 * targets are now always present in the list.
 *
 * Case is preserved (file systems are case-sensitive); `~` is expanded here so
 * the comparison against rule paths happens on concrete paths.
 */
function extractTargets(command: string): string[] {
  const targets: string[] = []
  for (const pattern of PATH_PATTERNS) {
    const globalPattern = new RegExp(pattern.source, pattern.flags)
    let match: RegExpExecArray | null
    while ((match = globalPattern.exec(command)) !== null) {
      // The bare `/` and `.` patterns capture a leading space via `(?:^|\s)`.
      const target = match[0].trim().replace(/^['"]|['"]$/g, "")
      if (!target) continue
      targets.push(expandHome(target))
    }
  }
  return targets
}

/**
 * Normalize a path for comparison.
 *
 * H124: (a) NO lowercasing — macOS/Linux file systems are case-sensitive, so
 * `rm -rf /Data` must not be treated as `rm -rf /data`; (b) `.` and `..`
 * segments are resolved textually (no fs access), so `~/a/../b` and `~/./b`
 * compare equal to `~/b`; (c) the filesystem root `/` is preserved.
 */
function normalizePath(path: string): string {
  const raw = path.replace(/\\/g, "/")
  const isAbsolute = raw.startsWith("/")
  const segments: string[] = []
  for (const segment of raw.split("/")) {
    if (segment === "" || segment === ".") continue
    if (segment === "..") {
      segments.pop()
      continue
    }
    segments.push(segment)
  }
  const joined = segments.join("/")
  if (isAbsolute) return `/${joined}` // "/" + "" === "/" (root survives)
  return joined === "" ? "." : joined
}

/** True when `child` lies strictly below `parent` at a path-segment boundary. */
function isPathInside(child: string, parent: string): boolean {
  if (parent === "/") return child !== "/" // everything else is inside root
  return child.startsWith(`${parent}/`)
}

/**
 * Does a destructive `target` touch the `protectedPath`?
 *
 * H124 — four documented rules:
 *  1. same path;
 *  2. the protected path is INSIDE the target → parent deletion (`rm -rf
 *     ~/proj` against a protected `~/proj/secret`, or `rm -rf /`);
 *  3. the target is inside the protected path → child deletion, SEGMENT
 *     BOUNDARY only, so `~/projekt-alt` does NOT match a protected `~/proj`;
 *  4. an explicit `*` suffix on the protected path is a wildcard: any target
 *     sharing the prefix matches. `*` is deliberate, so prefix matching is the
 *     documented intent here (unlike rule 3, which stays boundary-exact).
 */
function pathMatches(target: string, protectedPath: string): boolean {
  const t = normalizePath(target)
  const p = normalizePath(protectedPath)

  if (t === p) return true // rule 1
  // rule 2 (parent deletion) — W29-1: this IS the reverse-containment check
  // the Python mirror (path_matches) was missing. `isPathInside(p, t)` is
  // exactly `p.startsWith(t + "/")`, including the bare-root case (`t === "/"`
  // matches every protected path). Kept as the TS-side W29-1 implementation.
  if (isPathInside(p, t)) return true // rule 2 (parent deletion / ancestor wipe)
  if (isPathInside(t, p)) return true // rule 3 (child deletion, segment-exact)

  if (p.endsWith("*")) {
    // rule 4
    const prefix = normalizePath(p.slice(0, -1))
    if (t === prefix || t.startsWith(prefix)) return true
  }
  return false
}

interface ProtectionRule {
  path: string
  ruleText: string
  sourceId: string
}

/**
 * Build the matched_rules entries for targets that hit a rule.
 *
 * Shared by the normal destructive path and the W29-2 direct path check so
 * both produce the identical matched_rules shape.
 */
function collectMatches(
  targets: string[],
  rules: ProtectionRule[],
  action: string,
): Array<Record<string, unknown>> {
  const matched: Array<Record<string, unknown>> = []
  for (const target of targets) {
    for (const rule of rules) {
      if (pathMatches(target, rule.path)) {
        matched.push({
          target,
          protected_path: rule.path,
          rule_text: rule.ruleText,
          source_memory_id: rule.sourceId,
          action,
        })
      }
    }
  }
  return matched
}

/**
 * Load protection rules from Qdrant.
 *
 * Returns the (possibly empty) rule list on a successful load, or `null` when
 * the rules could NOT be loaded. The caller must treat `null` as "protection
 * rules unavailable" and fail closed for destructive actions — a confirmed
 * empty list is the only state that may pass.
 */
async function loadProtectionRules(qdrantClient: QdrantClient, _collection?: string): Promise<ProtectionRule[] | null> {
  try {
    // The QdrantClient is already bound to the configured collection, so the
    // `_collection` parameter is NOT used and callers must not pass one — it
    // was misleading (it looked like the query target without being one).
    const points = await qdrantClient.scrollFiltered(
      { must: [{ key: "category", match: { value: "rule" } }] },
      200,
    )

    const rules: ProtectionRule[] = []
    for (const point of points) {
      const payload = (point.payload || {}) as Record<string, unknown>
      const text = (payload.content as string) || ""
      const textLower = text.toLowerCase()
      if (!PROTECTION_KEYWORDS.some((kw) => textLower.includes(kw))) continue

      // Extract paths from rule text
      for (const pattern of PATH_PATTERNS) {
        const globalPattern = new RegExp(pattern.source, pattern.flags)
        let match: RegExpExecArray | null
        while ((match = globalPattern.exec(text)) !== null) {
          const path = match[0].trim()
          if (path && path.length > 2) {
            const expanded = path.startsWith("~/") ? path.replace(/^~/, process.env.HOME || "~") : path
            rules.push({
              path: expanded,
              ruleText: text.slice(0, 200),
              sourceId: String(point.id),
            })
          }
        }
      }
    }
    return rules
  } catch (exc) {
    // Fail-closed: signal that the rule store is unavailable instead of
    // pretending there are no protection rules. The caller blocks
    // destructive actions when rules are missing (never fail open).
    log.warn(`Guardrail: Failed to load protection rules (fail-closed): ${exc}`)
    return null
  }
}

/**
 * Core guardrail evaluation, shared by `nexus_guardrail_check` and the
 * override re-check (H140). Returns the JSON-shaped result object; never
 * throws (the rule store failing yields the fail-closed block).
 */
async function evaluateGuardrail(
  qdrantClient: QdrantClient,
  cfg: NexusConfig,
  checkedToolName: string,
  command: string,
  toolInput: Record<string, unknown>,
): Promise<Record<string, unknown>> {
  // W29-2: path-carrying tools (Write/Edit/MultiEdit/NotebookEdit) name their
  // target in file_path/notebook_path, NOT in `command`. Check that path
  // directly against the protection rules BEFORE the empty-command shortcut —
  // otherwise an empty `command` returns "allow" and the write slips through.
  const directPathField = PATH_CARRYING_TOOLS[checkedToolName]
  const directPath =
    directPathField && typeof toolInput[directPathField] === "string"
      ? String(toolInput[directPathField])
      : ""

  if (!command && !directPath) {
    return { verdict: "allow", reason: "Empty command" }
  }

  if (directPath) {
    const directTargets = extractTargets(directPath)
    if (directTargets.length > 0) {
      const rules = await loadProtectionRules(qdrantClient)
      if (rules === null) {
        // Fail-closed: rule store unavailable, cannot prove safety.
        return {
          verdict: "block",
          reason: "Destructive action (overwrite) but protection rules unavailable — fail-closed",
        }
      }
      const matched = collectMatches(directTargets, rules, "overwrite")
      if (matched.length > 0) {
        return {
          verdict: "block",
          reason: "Destructive action (overwrite) on protected target",
          matched_rules: matched,
        }
      }
    }
  }

  const fullInput = `${checkedToolName} ${command} ${JSON.stringify(toolInput)}`
  const action = classifyAction(fullInput)

  if (!action) {
    return { verdict: "allow", reason: "Non-destructive action" }
  }

  let targets = extractTargets(fullInput)
  // Also check tool_input values for paths
  if (toolInput && typeof toolInput === "object") {
    for (const v of Object.values(toolInput)) {
      if (typeof v === "string" && (v.includes("/") || v.includes("~"))) {
        targets = targets.concat(extractTargets(v))
      }
    }
  }

  if (targets.length === 0) {
    return { verdict: "allow", reason: `Destructive action (${action}) but no protected target` }
  }

  // No collection argument: the bound QdrantClient already targets cfg.collection.
  const rules = await loadProtectionRules(qdrantClient)
  if (rules === null) {
    // Rules could not be loaded — we cannot prove the target is
    // unprotected, so block the destructive action (fail-closed).
    return {
      verdict: "block",
      reason: `Destructive action (${action}) but protection rules unavailable — fail-closed`,
    }
  }
  if (rules.length === 0) {
    return { verdict: "allow", reason: `Destructive action (${action}) but no protection rules` }
  }

  const matched = collectMatches(targets, rules, action)

  if (matched.length > 0) {
    return {
      verdict: "block",
      reason: `Destructive action (${action}) on protected target`,
      matched_rules: matched,
    }
  }
  return { verdict: "allow", reason: `Destructive action (${action}) on unprotected target` }
}

export function registerGuardrailCheckTool(
  api: OpenClawPluginApi,
  qdrantClient: QdrantClient,
  cfg: NexusConfig,
  toolName = "nexus_guardrail_check",
): void {
  api.registerTool({
    name: toolName,
    label: "Nexus Guardrail Check",
    description:
      "Active Guardrails: Check if an action is safe before executing it. Queries Nexus Memory for protection rules. Use before destructive operations (rm, drop, kill, overwrite).",
    parameters: Type.Object({
      command: Type.String({ description: "The command string to check (e.g. 'rm -rf ~/project/')" }),
      tool_name: Type.Optional(Type.String({ description: "The tool being called", default: "" })),
      tool_input: Type.Optional(Type.Record(Type.String(), Type.Unknown())),
    }),
    async execute(
      _toolCallId: string,
      params: { command: string; tool_name?: string; tool_input?: Record<string, unknown> },
    ) {
      const { command, tool_name: checkedToolName = "", tool_input: toolInput = {} } = params

      const result = await evaluateGuardrail(
        qdrantClient,
        cfg,
        checkedToolName,
        command,
        toolInput,
      )

      return { content: [{ type: "text" as const, text: JSON.stringify(result) }] }
    },
  })
}

/** Shape an override must cite from a real guardrail_check block. */
type CitedRule = {
  protected_path: string
  rule_text: string
  source_memory_id: string
}

export function registerGuardrailOverrideTool(
  api: OpenClawPluginApi,
  qdrantClient: QdrantClient,
  cfg: NexusConfig,
  embedder: { embed: (text: string) => Promise<number[]> },
  toolName = "nexus_guardrail_override",
): void {
  api.registerTool({
    name: toolName,
    label: "Nexus Guardrail Override",
    description:
      "Active Guardrails: Record a guardrail override with full audit trail. Required when guardrail_check returns 'block' but the action is explicitly authorized. The command is re-checked against the current protection rules; only a still-blocking command with matching rules is recorded.",
    parameters: Type.Object({
      command: Type.String({ description: "The command that was blocked" }),
      reasoning: Type.String({ description: "Explicit reasoning why this action is safe despite the guardrail block. Minimum 30 characters." }),
      matched_rules: Type.Optional(
        Type.Array(Type.Record(Type.String(), Type.Unknown()), {
          description:
            "The matched_rules array from the guardrail_check block result. At least one entry, each with string protected_path, rule_text and source_memory_id.",
        }),
      ),
      agent_id: Type.Optional(
        Type.String({
          description:
            "The real agent id taking responsibility. Must be a non-empty id — \"unknown\" is rejected.",
        }),
      ),
    }),
    async execute(
      _toolCallId: string,
      params: { command: string; reasoning: string; matched_rules?: unknown[]; agent_id?: string },
    ) {
      const { command, reasoning, matched_rules: matchedRules = [], agent_id: agentId = "unknown" } = params

      const fail = (error: string) => ({
        isError: true,
        content: [{ type: "text" as const, text: JSON.stringify({ status: "error", error }) }],
      })

      // H140 (1): a longer, non-gameable reasoning bar (no word list — that
      // would only invite keyword stuffing). Raised 10 → 30 chars.
      const trimmedReasoning = reasoning.trim()
      if (!trimmedReasoning || trimmedReasoning.length < 30) {
        return fail("Override requires explicit reasoning (min 30 chars).")
      }

      // H140 (2): the override must cite a real block. `matched_rules` was
      // optional and stored verbatim, so an override could be "proven" with
      // an empty or fabricated list — the audit chain did not hold.
      if (!Array.isArray(matchedRules) || matchedRules.length === 0) {
        return fail(
          "override rejected: matched_rules must list at least one rule from a guardrail_check block.",
        )
      }
      const cited: CitedRule[] = []
      for (const entry of matchedRules) {
        if (!entry || typeof entry !== "object" || Array.isArray(entry)) {
          return fail(
            "override rejected: every matched_rules entry must be an object with string protected_path, rule_text and source_memory_id.",
          )
        }
        const o = entry as Record<string, unknown>
        if (
          typeof o.protected_path !== "string" ||
          typeof o.rule_text !== "string" ||
          typeof o.source_memory_id !== "string"
        ) {
          return fail(
            "override rejected: every matched_rules entry must have string protected_path, rule_text and source_memory_id.",
          )
        }
        cited.push({
          protected_path: o.protected_path,
          rule_text: o.rule_text,
          source_memory_id: o.source_memory_id,
        })
      }

      // H140 (3): an anonymous override is not auditable.
      const trimmedAgentId = typeof agentId === "string" ? agentId.trim() : ""
      if (trimmedAgentId === "" || trimmedAgentId.toLowerCase() === "unknown") {
        return fail(
          'override rejected: agent_id must be a real, non-empty agent id (not "unknown").',
        )
      }

      try {
        // H140 (4): re-check the command against the CURRENT protection rules
        // with the same matching logic the check tool uses. An override is only
        // meaningful if the command would still be blocked right now.
        const recheck = await evaluateGuardrail(qdrantClient, cfg, "", command, {})
        const recheckMatches = (recheck.matched_rules as Array<Record<string, unknown>> | undefined) ?? []
        const recheckPaths = new Set(
          recheckMatches.map((m) => normalizePath(String(m.protected_path ?? ""))),
        )
        // W34-Fund: the caller-supplied payload was previously confirmed by
        // protected_path alone — a fabricated matched_rules entry (real path,
        // invented source_memory_id/rule_text) was indistinguishable from a
        // server-verified rule. Confirm by ID (and rule_text) against the
        // re-check's own matches, not the caller's word.
        const recheckByIdText = new Map(
          recheckMatches.map((m) => [
            `${normalizePath(String(m.protected_path ?? ""))}\u0000${String(m.source_memory_id ?? "")}\u0000${String(m.rule_text ?? "")}`,
            m,
          ]),
        )
        const confirmed = cited.filter(
          (c) =>
            recheckByIdText.has(
              `${normalizePath(c.protected_path)}\u0000${c.source_memory_id}\u0000${c.rule_text}`,
            ),
        )

        if (recheck.verdict !== "block" || confirmed.length === 0) {
          return fail(
            "override rejected: command does not match a current guardrail block " +
              `(re-check produced ${String(recheck.verdict)} / different rules).`,
          )
        }

        const overrideId = randomUUID()
        const auditText = `GUARDRAIL OVERRIDE: ${command} | Reasoning: ${trimmedReasoning} | Agent: ${trimmedAgentId}`
        const vector = await embedder.embed(auditText)

        await qdrantClient.upsert(overrideId, vector, {
          content: auditText,
          category: "session",
          access_level: "private",
          guardrail_override: true,
          overridden_command: command,
          reasoning: trimmedReasoning,
          agent_id: trimmedAgentId,
          // Only rules whose protected_path the re-check confirmed right now.
          matched_rules: confirmed,
          recheck_verdict: recheck.verdict,
          verified_at: new Date().toISOString(),
          timestamp: new Date().toISOString(),
        })

        return {
          content: [{ type: "text" as const, text: JSON.stringify({ status: "override_recorded", override_id: overrideId }) }],
        }
      } catch (exc) {
        log.warn(`Guardrail override failed: ${exc}`)
        return fail(String(exc))
      }
    },
  })
}