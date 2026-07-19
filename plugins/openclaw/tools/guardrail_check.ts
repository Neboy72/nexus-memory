import { Type } from "@sinclair/typebox"
import type { OpenClawPluginApi } from "openclaw/plugin-sdk"
import type { QdrantClient } from "../lib/qdrant-client.ts"
import type { NexusConfig } from "../lib/config.ts"
import { log } from "../logger.ts"

// Destructive command patterns
const DESTRUCTIVE_PATTERNS: Array<{ action: string; patterns: RegExp[] }> = [
  { action: "delete", patterns: [/\brm\b.*-r/i, /\brm\b.*-f/i, /\brmdir\b/i, /\bdel\b\s+\/[fsq]/i, /\bdrop\b/i, /\btruncate\b/i, /\buninstall\b/i, /\bfind\b.*-delete/i, /\bgit\b.*clean.*-[fd]/i, /\bdd\b.*\bof\b/i] },
  { action: "kill", patterns: [/\bkill\b.*-9/i, /\bpkill\b/i, /\bkillall\b/i, /\btaskkill\b/i] },
  { action: "overwrite", patterns: [/\bwrite_file\b/i, />\s*[^|&]/] },
  { action: "recreate", patterns: [/\brecreate_collection\b/i, /DELETE.*collection/i, /\bdrop\b.*collection/i] },
]

const PROTECTION_KEYWORDS = ["never delete", "never remove", "do not delete", "do not remove",
  "protected", "niemals", "verboten", "forbidden", "tabu", "sacred"]

const PATH_PATTERNS = [/~[\w./-]+/g, /\/[\w./-]+/g, /\w:[\\/][\w\\./-]+/g]

function classifyAction(command: string): string | null {
  for (const { action, patterns } of DESTRUCTIVE_PATTERNS) {
    for (const pattern of patterns) {
      if (pattern.test(command)) return action
    }
  }
  return null
}

function extractTargets(command: string): string[] {
  const targets: string[] = []
  for (const pattern of PATH_PATTERNS) {
    const globalPattern = new RegExp(pattern.source, pattern.flags)
    let match: RegExpExecArray | null
    while ((match = globalPattern.exec(command)) !== null) {
      const target = match[0].trim().replace(/^['"]|['"]$/g, "")
      if (target && target.length > 2 && target !== "~" && target !== "/" && target !== ".") {
        // Expand ~ to home directory
        const expanded = target.startsWith("~/") ? target.replace(/^~/, process.env.HOME || "~") : target
        targets.push(expanded)
      }
    }
  }
  return targets
}

function normalizePath(path: string): string {
  let p = path.replace(/\/+/g, "/").replace(/\/$/, "").toLowerCase()
  if (p.length > 1) p = p.replace(/\/$/, "")
  return p
}

function pathMatches(target: string, protectedPath: string): boolean {
  const t = normalizePath(target)
  const p = normalizePath(protectedPath)
  if (t === p) return true
  if (p.endsWith("*")) {
    const prefix = p.slice(0, -1)
    if (t.startsWith(prefix)) return true
  }
  if (t.startsWith(p + "/")) return true
  return false
}

interface ProtectionRule {
  path: string
  ruleText: string
  sourceId: string
}

async function loadProtectionRules(qdrantClient: QdrantClient, collection: string): Promise<ProtectionRule[]> {
  try {
    const results = await qdrantClient.scroll(collection, {
      filter: { must: [{ key: "category", match: { value: "rule" } }] },
      limit: 200,
      withPayload: true,
      withVector: false,
    })

    const rules: ProtectionRule[] = []
    for (const point of results.points || []) {
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
    log.warn(`Guardrail: Failed to load rules (fail-open): ${exc}`)
    return []
  }
}

export function registerGuardrailCheckTool(
  api: OpenClawPluginApi,
  qdrantClient: QdrantClient,
  cfg: NexusConfig,
  toolName = "nexus_guardrail_check",
): void {
  api.registerTool(
    {
      name: toolName,
      label: "Nexus Guardrail Check",
      description:
        "Active Guardrails: Check if an action is safe before executing it. Queries Nexus Memory for protection rules. Use before destructive operations (rm, drop, kill, overwrite).",
      parameters: Type.Object({
        command: Type.String({ description: "The command string to check (e.g. 'rm -rf ~/project/')" }),
        tool_name: Type.Optional(Type.String({ description: "The tool being called", default: "" })),
        tool_input: Type.Optional(Type.Record(Type.String(), Type.Unknown())),
      }),
    },
    async (params: { command: string; tool_name?: string; tool_input?: Record<string, unknown> }) => {
      const { command, tool_name: toolName = "", tool_input: toolInput = {} } = params

      if (!command) {
        return { verdict: "allow", reason: "Empty command" }
      }

      const fullInput = `${toolName} ${command} ${JSON.stringify(toolInput)}`
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

      const rules = await loadProtectionRules(qdrantClient, cfg.collection || "nexus")
      if (rules.length === 0) {
        return { verdict: "allow", reason: `Destructive action (${action}) but no protection rules` }
      }

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

      if (matched.length > 0) {
        return {
          verdict: "block",
          reason: `Destructive action (${action}) on protected target`,
          matched_rules: matched,
        }
      }

      return { verdict: "allow", reason: `Destructive action (${action}) on unprotected target` }
    },
  )
}

export function registerGuardrailOverrideTool(
  api: OpenClawPluginApi,
  qdrantClient: QdrantClient,
  cfg: NexusConfig,
  embedder: { embed: (text: string) => Promise<number[]>; dim: number },
  toolName = "nexus_guardrail_override",
): void {
  api.registerTool(
    {
      name: toolName,
      label: "Nexus Guardrail Override",
      description:
        "Active Guardrails: Record a guardrail override with full audit trail. Required when guardrail_check returns 'block' but the action is explicitly authorized.",
      parameters: Type.Object({
        command: Type.String({ description: "The command that was blocked" }),
        reasoning: Type.String({ description: "Explicit reasoning why this action is safe despite the guardrail block. Minimum 10 characters." }),
        matched_rules: Type.Optional(Type.Array(Type.Record(Type.String(), Type.Unknown()))),
        agent_id: Type.Optional(Type.String({ default: "unknown" })),
      }),
    },
    async (params: { command: string; reasoning: string; matched_rules?: unknown[]; agent_id?: string }) => {
      const { command, reasoning, matched_rules: matchedRules = [], agent_id: agentId = "unknown" } = params

      const trimmedReasoning = reasoning.trim()
      if (!trimmedReasoning || trimmedReasoning.length < 10) {
        return { status: "error", error: "Override requires explicit reasoning (min 10 chars)." }
      }

      try {
        const overrideId = crypto.randomUUID()
        const auditText = `GUARDRAIL OVERRIDE: ${command} | Reasoning: ${trimmedReasoning} | Agent: ${agentId}`
        const vector = await embedder.embed(auditText)

        await qdrantClient.upsert(cfg.collection || "nexus", {
          points: [
            {
              id: overrideId,
              vector,
              payload: {
                content: auditText,
                category: "session",
                access_level: "private",
                guardrail_override: true,
                overridden_command: command,
                reasoning: trimmedReasoning,
                agent_id: agentId,
                matched_rules: matchedRules,
                timestamp: new Date().toISOString(),
              },
            },
          ],
        })

        return { status: "override_recorded", override_id: overrideId }
      } catch (exc) {
        log.warn(`Guardrail override failed: ${exc}`)
        return { status: "error", error: String(exc) }
      }
    },
  )
}