import { randomUUID } from "node:crypto"
import { Type } from "@sinclair/typebox"
import type { OpenClawPluginApi } from "openclaw/plugin-sdk"
import { ACCESS_LEVEL_ORDER, type QdrantClient } from "../lib/qdrant-client.ts"
import type { MemoryWriter } from "../lib/write-gate.ts"
import type { NexusConfig } from "../lib/config.ts"
import { toolCallAccessLevel } from "../lib/turn-context.ts"
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
export const PATH_CARRYING_TOOLS: Record<string, string> = {
  Write: "file_path",
  Edit: "file_path",
  MultiEdit: "file_path",
  NotebookEdit: "notebook_path",
  // The host's own tool names are lowercase and carry the target in `path`.
  write: "path",
  edit: "path",
  multiedit: "path",
  notebookedit: "notebook_path",
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

export /**
 * Relative path tokens inside RULE text: `.hermes`, `./build`, `../shared`.
 *
 * A protection rule often names a path relative to the project it lives in;
 * the absolute-only patterns above never saw those, so such a rule was inert
 * (and a relative command target looked "unprotected"). It is resolved against
 * the trusted working directory, exactly like a relative command target.
 */
const RELATIVE_RULE_PATH_PATTERN = /(?:^|\s)(\.{1,2}\/[\w./-]+|\.[A-Za-z_][\w.-]*)/g

export function classifyAction(command: string): string | null {
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
 * Relative operands of a delete-style command: `rm -rf build/x` → ["build/x"].
 *
 * Absolute, `~` and bare `.` operands are already covered by extractTargets;
 * this fills the gap the audit named (finding 2 #5): `rm -rf .hermes` used to
 * produce NO target at all and was therefore reported as "unprotected".
 * Everything that is not a concrete operand (flags, redirects, expansions,
 * globs) is skipped — a guessable pattern must not become a "target".
 */
function extractRelativeTargets(command: string): string[] {
  const out: string[] = []
  for (const segment of command.split(/[;&|\n]+/)) {
    const tokens = segment.trim().split(/\s+/).filter(Boolean)
    const at = tokens.findIndex((token) => /^(?:rm|rmdir|del)$/i.test(token))
    if (at < 0) continue
    for (const token of tokens.slice(at + 1)) {
      const raw = token.replace(/^['"]|['"]$/g, "").replace(/^of=/, "")
      if (!raw || raw.startsWith("-")) continue
      if (isAbsolutePath(raw) || raw === "~" || raw.startsWith("~/") || raw === ".") continue
      if (/[<>$*?]/.test(raw)) continue
      out.push(raw)
    }
  }
  return out
}

/**
 * All comparison-form targets of a text, plus whether a relative target could
 * NOT be resolved for lack of a trusted working directory.
 */
function resolveTargets(
  text: string,
  base: string | null,
): { targets: string[]; unresolved: boolean } {
  const resolved = extractTargets(text).map((target) => comparisonPath(target, base))
  const relative = extractRelativeTargets(text)
  if (relative.length > 0 && base === null) return { targets: resolved, unresolved: true }
  for (const target of relative) resolved.push(comparisonPath(target, base))
  return { targets: resolved, unresolved: false }
}

/**
 * True for a POSIX absolute path and for a Windows drive path (`C:\x`, `C:/x`).
 */
function isAbsolutePath(path: string): boolean {
  return path.startsWith("/") || /^[A-Za-z]:[\\/]/.test(path)
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

/**
 * The comparison form of a path: `~` expanded, `.`/`..` resolved textually.
 *
 * Relative paths are resolved against `base` (the trusted working directory)
 * when one is known. Without a base they stay relative, which can never equal
 * an absolute protected path — the caller reports "indeterminate" instead of
 * allowing (ClawHub audit 14.11.2026, finding 2 #5).
 */
function comparisonPath(raw: string, base: string | null): string {
  const expanded = expandHome(raw)
  if (isAbsolutePath(expanded) || base === null) return normalizePath(expanded)
  return normalizePath(`${normalizePath(base)}/${expanded}`)
}

/**
 * The working directory relative targets are resolved against, or null when
 * there is none that can be trusted.
 *
 * An EXPLICIT value is trusted only when it is absolute (a relative "cwd" is
 * itself ambiguous, so it is refused rather than guessed). Without one, the
 * plugin process's own working directory is used — it is a real directory of
 * this agent, and resolving against it can only ADD matches, never remove one.
 */
function trustedCwd(explicit?: unknown): string | null {
  if (explicit !== undefined) {
    if (typeof explicit !== "string") return null
    const trimmed = explicit.trim()
    return isAbsolutePath(trimmed) ? trimmed : null
  }
  try {
    const cwd = process.cwd()
    return isAbsolutePath(cwd) ? cwd : null
  } catch {
    return null
  }
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
  /** Comparison form of the protected path (expanded, normalized). */
  path: string
  ruleText: string
  sourceId: string
  /** Access level of the rule memory; a missing level counts as `private`. */
  accessLevel: string
}

/** One rule hit — the INTERNAL shape, including the rule's access level. */
type RuleMatch = {
  target: string
  protectedPath: string
  ruleText: string
  sourceId: string
  accessLevel: string
  action: string
}

/** What an evaluation concluded, independently of who asked. */
export type GuardrailEvaluation = {
  verdict: "allow" | "block" | "indeterminate"
  reason: string
  /** EVERY rule hit — disclosure is decided later, not here. */
  matches: RuleMatch[]
}

/** A stored rule whose level is unknown counts as private (fail-closed). */
function ruleRank(level: unknown): number {
  return ACCESS_LEVEL_ORDER[String(level)] ?? ACCESS_LEVEL_ORDER.private
}

/** A CALLER whose level is unknown counts as public (fail-closed). */
function viewerRank(level: unknown): number {
  return ACCESS_LEVEL_ORDER[String(level)] ?? ACCESS_LEVEL_ORDER.public
}

/**
 * Build the internal rule-hit entries.
 *
 * Shared by the normal destructive path and the W29-2 direct path check so
 * both produce the identical match shape. Carries the access level so that
 * DISCLOSURE can be decided per hit (see `guardrailResponse`); the level
 * itself never appears in an answer.
 */
function collectMatches(targets: string[], rules: ProtectionRule[], action: string): RuleMatch[] {
  const matched: RuleMatch[] = []
  for (const target of targets) {
    for (const rule of rules) {
      if (pathMatches(target, rule.path)) {
        matched.push({
          target,
          protectedPath: rule.path,
          ruleText: rule.ruleText,
          sourceId: rule.sourceId,
          accessLevel: rule.accessLevel,
          action,
        })
      }
    }
  }
  return matched
}

/** The disclosed shape of one rule hit. */
function ruleMatchResponse(match: RuleMatch): Record<string, unknown> {
  return {
    target: match.target,
    protected_path: match.protectedPath,
    rule_text: match.ruleText,
    source_memory_id: match.sourceId,
    action: match.action,
  }
}

/** Rules per scroll page — large on purpose: the page size is not a data limit. */
const RULE_PAGE_SIZE = 1000

/**
 * Load protection rules from Qdrant.
 *
 * Returns the (possibly empty) rule list only when the walk was COMPLETE, or
 * `null` when it was not. `null` means "protection rules unavailable", and the
 * caller must fail closed for destructive actions — a confirmed empty list is
 * the only state that may pass.
 *
 * ClawHub audit 14.11.2026 (finding 2): the old version read the best-effort
 * scroll result as the complete set, so a later-page error or the fixed
 * 200×5 ceiling produced a HALF rule set that looked exactly like a full one.
 * It now uses the strict scroll (all-or-error, complete/truncated
 * distinguishable) with pages far beyond any real rule count. The per-path
 * `length > 2` limit is gone too: it dropped rules protecting `/`, `~` and
 * `.` — exactly the targets a wipe hits first.
 */
async function loadProtectionRules(
  qdrantClient: QdrantClient,
  baseCwd: string | null,
): Promise<ProtectionRule[] | null> {
  try {
    const { points, complete } = await qdrantClient.scrollFilteredStrict(
      { must: [{ key: "category", match: { value: "rule" } }] },
      RULE_PAGE_SIZE,
    )
    if (!complete) {
      // Fail-closed: a cut-off walk is NOT "these are all the rules".
      log.warn("Guardrail: protection rule set is incomplete (page cap reached) — fail-closed")
      return null
    }

    const rules: ProtectionRule[] = []
    for (const point of points) {
      const payload = (point.payload || {}) as Record<string, unknown>
      const text = (payload.content as string) || ""
      const textLower = text.toLowerCase()
      if (!PROTECTION_KEYWORDS.some((kw) => textLower.includes(kw))) continue
      const accessLevel =
        typeof payload.access_level === "string" && payload.access_level
          ? payload.access_level
          : "private"

      // Extract paths from the rule text — normalized exactly like the targets.
      const seen = new Set<string>()
      for (const pattern of [...PATH_PATTERNS, RELATIVE_RULE_PATH_PATTERN]) {
        const globalPattern = new RegExp(pattern.source, pattern.flags)
        let match: RegExpExecArray | null
        while ((match = globalPattern.exec(text)) !== null) {
          const path = match[0].trim()
          if (!path) continue
          const resolved = comparisonPath(path, baseCwd)
          if (seen.has(resolved)) continue
          seen.add(resolved)
          // Full rule text on purpose: an override must cite it byte for byte,
          // so a truncation here would break that verification.
          rules.push({ path: resolved, ruleText: text, sourceId: String(point.id), accessLevel })
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

/** Reason for a relative target we could not resolve — never rule content. */
const INDETERMINATE_REASON =
  "Destructive action with a relative target and no trusted working directory — cannot prove the target is unprotected"

/**
 * Core guardrail evaluation, shared by `nexus_guardrail_check`, the override
 * re-check (H140) and the execution boundary (`hooks/pre-tool-gate.ts`).
 *
 * ENFORCEMENT IS VIEWER-INDEPENDENT: it always checks against every rule,
 * private ones included. Only the ANSWER is filtered, by `guardrailResponse`.
 * Never throws — a failing rule store yields the fail-closed block.
 */
export async function evaluateGuardrail(
  qdrantClient: QdrantClient,
  _cfg: NexusConfig,
  checkedToolName: string,
  command: string,
  toolInput: Record<string, unknown>,
  explicitCwd?: unknown,
): Promise<GuardrailEvaluation> {
  const base = trustedCwd(explicitCwd)

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
    return { verdict: "allow", reason: "Empty command", matches: [] }
  }

  if (directPath) {
    const direct = resolveTargets(directPath, base)
    if (direct.targets.length > 0 || direct.unresolved) {
      const rules = await loadProtectionRules(qdrantClient, base)
      if (rules === null) {
        // Fail-closed: rule store unavailable, cannot prove safety.
        return {
          verdict: "block",
          reason: "Destructive action (overwrite) but protection rules unavailable — fail-closed",
          matches: [],
        }
      }
      const matched = collectMatches(direct.targets, rules, "overwrite")
      if (matched.length > 0) {
        return {
          verdict: "block",
          reason: "Destructive action (overwrite) on protected target",
          matches: matched,
        }
      }
      if (direct.unresolved) {
        return { verdict: "indeterminate", reason: INDETERMINATE_REASON, matches: [] }
      }
    }
  }

  const fullInput = `${checkedToolName} ${command} ${JSON.stringify(toolInput)}`
  const action = classifyAction(fullInput)

  if (!action) {
    return { verdict: "allow", reason: "Non-destructive action", matches: [] }
  }

  const resolvedInput = resolveTargets(fullInput, base)
  let targets = resolvedInput.targets
  let unresolved = resolvedInput.unresolved
  // Also check tool_input values for paths
  if (toolInput && typeof toolInput === "object") {
    for (const v of Object.values(toolInput)) {
      if (typeof v === "string" && (v.includes("/") || v.includes("~"))) {
        const extra = resolveTargets(v, base)
        targets = targets.concat(extra.targets)
        unresolved = unresolved || extra.unresolved
      }
    }
  }

  if (targets.length === 0 && !unresolved) {
    return { verdict: "allow", reason: `Destructive action (${action}) but no protected target`, matches: [] }
  }

  const rules = await loadProtectionRules(qdrantClient, base)
  if (rules === null) {
    // Rules could not be loaded — we cannot prove the target is
    // unprotected, so block the destructive action (fail-closed).
    return {
      verdict: "block",
      reason: `Destructive action (${action}) but protection rules unavailable — fail-closed`,
      matches: [],
    }
  }
  if (rules.length === 0) {
    return { verdict: "allow", reason: `Destructive action (${action}) but no protection rules`, matches: [] }
  }

  const matched = collectMatches(targets, rules, action)

  if (matched.length > 0) {
    return {
      verdict: "block",
      reason: `Destructive action (${action}) on protected target`,
      matches: matched,
    }
  }
  // A target we could not resolve must never be reported as unprotected.
  if (unresolved) {
    return { verdict: "indeterminate", reason: INDETERMINATE_REASON, matches: [] }
  }
  return { verdict: "allow", reason: `Destructive action (${action}) on unprotected target`, matches: [] }
}

/**
 * Shape an evaluation for a caller at `viewerLevel`.
 *
 * ClawHub audit 14.11.2026 (MCP07): enforcement and disclosure are separated
 * here. The verdict is unchanged for every caller — a private rule still
 * blocks a public session — but a caller below the rule's access level gets a
 * GENERIC answer: no `rule_text`, no `protected_path`, no `source_memory_id`.
 * Only the number of withheld rules is reported, which names nothing.
 */
export function guardrailResponse(
  evaluation: GuardrailEvaluation,
  viewerLevel: string,
): Record<string, unknown> {
  const viewer = viewerRank(viewerLevel)
  const visible = evaluation.matches.filter((match) => viewer >= ruleRank(match.accessLevel))
  const withheld = evaluation.matches.length - visible.length
  const out: Record<string, unknown> = { verdict: evaluation.verdict, reason: evaluation.reason }
  if (evaluation.verdict !== "block") return out
  out.matched_rules = visible.map(ruleMatchResponse)
  if (withheld > 0) {
    out.withheld_rules = withheld
    out.reason =
      "Destructive action blocked by a protection rule. Rule details are withheld: " +
      "the rule's access level is above this session's."
  }
  return out
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
      cwd: Type.Optional(
        Type.String({
          description:
            "Absolute working directory relative targets are resolved against. Omit to use the plugin process's own working directory.",
        }),
      ),
    }),
    async execute(
      toolCallId: string,
      params: {
        command: string
        tool_name?: string
        tool_input?: Record<string, unknown>
        cwd?: string
      },
    ) {
      const { command, tool_name: checkedToolName = "", tool_input: toolInput = {}, cwd } = params

      // The EFFECTIVE level of this call, never the configured one: an
      // unbound, expired or group-bound call reads `public` and may not be
      // shown protection-rule content (ClawHub audit 14.11.2026, MCP07).
      const viewerLevel = toolCallAccessLevel(cfg.accessLevel, toolCallId)
      const evaluation = await evaluateGuardrail(
        qdrantClient,
        cfg,
        checkedToolName,
        command,
        toolInput,
        cwd,
      )

      return {
        content: [
          { type: "text" as const, text: JSON.stringify(guardrailResponse(evaluation, viewerLevel)) },
        ],
      }
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
  writer: MemoryWriter,
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
      toolCallId: string,
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
        // W34-Fund: the caller-supplied payload was previously confirmed by
        // protected_path alone — a fabricated matched_rules entry (real path,
        // invented source_memory_id/rule_text) was indistinguishable from a
        // server-verified rule. Confirm by ID (and rule_text) against the
        // re-check's own matches, not the caller's word.
        const recheckByIdText = new Map(
          recheck.matches.map((m) => [
            `${normalizePath(m.protectedPath)}\u0000${m.sourceId}\u0000${m.ruleText}`,
            m,
          ]),
        )
        const confirmedEntries = cited
          .map((c) =>
            recheckByIdText.get(
              `${normalizePath(c.protected_path)}\u0000${c.source_memory_id}\u0000${c.rule_text}`,
            ),
          )
          .filter((m): m is RuleMatch => m !== undefined)

        if (recheck.verdict !== "block" || confirmedEntries.length === 0) {
          return fail(
            "override rejected: command does not match a current guardrail block " +
              `(re-check produced ${String(recheck.verdict)} / different rules).`,
          )
        }

        // ClawHub audit 14.11.2026 (MCP07): a caller may only override a rule
        // it is allowed to SEE. Otherwise guessing a private rule's text would
        // confirm — and thereby disclose — it.
        const viewer = viewerRank(toolCallAccessLevel(cfg.accessLevel, toolCallId))
        const confirmed = confirmedEntries.filter(
          (match) => viewer >= ruleRank(match.accessLevel),
        )
        if (confirmed.length === 0) {
          return fail(
            "override rejected: the blocking rule is above this session's access level — "
              + "the owner has to record the override.",
          )
        }

        const overrideId = randomUUID()
        const auditText = `GUARDRAIL OVERRIDE: ${command} | Reasoning: ${trimmedReasoning} | Agent: ${trimmedAgentId}`
        const vector = await embedder.embed(auditText)

        // The audit record is a memory write too: it goes through the same
        // gate as every other point (lib/write-gate.ts).
        await writer.upsert(overrideId, vector, {
          content: auditText,
          category: "session",
          access_level: "private",
          guardrail_override: true,
          overridden_command: command,
          reasoning: trimmedReasoning,
          agent_id: trimmedAgentId,
          // Only rules the re-check confirmed right now, in the disclosed shape.
          matched_rules: confirmed.map(ruleMatchResponse),
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