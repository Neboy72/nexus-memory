/**
 * Pre-Tool Gate — Vorbereitung vor Aktionen (adaptiert von Kioshas nexus-gate.py)
 *
 * Drei Ebenen, in dieser Reihenfolge:
 *
 * 1. GUARDRAILS — Config/Kill/Delete ohne GO = sofort BLOCK
 * 2. PRE-ACTION RECALL — Bei Keywords in Tool-Params → Nexus fragen → Kontext injizieren
 * 3. PLAN-ZWANG — System-Kommandos ohne Plan-Lock = BLOCK (mit Recall-Kontext als Info)
 *
 * Read-only Tools (read, web_search, web_fetch, image, pdf, etc.) brauchen keinen Plan.
 *
 * Der Plan-Lock ist eine Datei die der Agent schreiben muss bevor er nicht-triviale
 * Aktionen ausführt. Sie ist 5 Minuten gültig. Das zwingt ihn zu denken bevor er handelt.
 */

import { statSync, unlinkSync } from "node:fs"
import { Embedder } from "../lib/embedder.ts"
import type { QdrantClient, SearchResult } from "../lib/qdrant-client.ts"
import type { NexusConfig } from "../lib/config.ts"
import { log } from "../logger.ts"

// ── Configuration ────────────────────────────────────────────────

const NEXUS_QUERY_LIMIT = 5
const PLAN_LOCK_PATH = "/tmp/miosha-think-gate.lock"
const PLAN_MAX_AGE_MS = 5 * 60 * 1000 // 5 minutes

// Keywords die Pre-Action Recall triggern (Tool-Parameter-Scan)
const RECALL_KEYWORDS = new Set([
  "browser", "chrome", "chromium", "brave", "safari", "firefox",
  "config", "tailscale", "openclaw", "launchctl",
  "kill", "pkill", "rm -rf", "shutdown", "reboot",
  "ollama", "qdrant", "nexus",
  "dashboard", "serve", "gateway", "cron",
  "ssh", "scp", "rsync", "git push", "git checkout",
  "pip", "npm", "yarn", "brew",
])

// Terminal-Kommandos die Plan-Zwang erfordern (destruktive Eingriffe only)
// 27.08.2026: verengt nach Kiosha-Request — Routine-Installs (brew/pip/npm/npx)
// und nicht-destruktive git-ops (checkout/stash) sind jetzt plan-frei.
const PLAN_REQUIRED_COMMANDS = new Set([
  "launchctl load", "launchctl unload", "launchctl bootstrap", "launchctl kickstart",
  "kill", "pkill", "killall",
  "shutdown", "reboot",
  "rm -rf", "rm -r",
  "git push", "git reset --hard",
  "defaults write",
  "openclaw gateway restart", "openclaw gateway stop",
  "openclaw update", "openclaw plugins install",
])

// Tools die immer Plan-frei sind (read-only / low-risk)
const ALWAYS_ALLOW_TOOLS = new Set([
  "read", "web_search", "web_fetch", "image", "pdf",
  "tavily_search", "tavily_extract", "session_status",
  "sessions_list", "sessions_history", "subagents",
  "nexus_search", "nexus_recall", "nexus-memory__recall",
  "nexus-memory__health", "nexus-memory__check_update",
  "nexus-memory__cost_routing_stats", "nexus-memory__cost_routing_explain",
  "nexus-memory__list_subscriptions", "nexus-memory__find_entities",
  "nexus-memory__get_related", "nexus-memory__get_subgraph",
  "nexus-memory__graph_traverse",
  "agents_list", "nodes",
  "message", // sending messages is Yellow Zone, handled by GO discipline
])

// Critical paths that should never be rm -rf'd
const PROTECTED_PATHS = [
  "~/.openclaw",
  "~/.hermes",
  "~/nexus-memory",
  "/Users/miosha/.openclaw",
  "/Users/miosha/.hermes",
  "/Users/miosha/nexus-memory",
]

// ── Helpers ──────────────────────────────────────────────────────

function hasValidPlan(): boolean {
  try {
    // Fix 27.08.2026: direkter node:fs-Import statt Deno/require-Shim.
    // Der alte Shim ((globalThis as any).Deno?.statSync ?? (globalThis as any).require?.("fs")?.statSync)
    // resolvierte im OpenClaw-Gateway (Node, ESM) zu undefined —
    // hasValidPlan() war dadurch IMMER false, der Plan-Lock wurde nie erkannt.
    const statResult = statSync(PLAN_LOCK_PATH) // wirft, wenn Datei fehlt
    const age = Date.now() - statResult.mtimeMs
    if (age > PLAN_MAX_AGE_MS) {
      // Plan expired — clean up
      try {
        unlinkSync(PLAN_LOCK_PATH)
      } catch {}
      return false
    }
    return true
  } catch {
    return false
  }
}

function extractRecallQuery(toolName: string, params: Record<string, unknown>): string {
  const parts: string[] = []
  for (const field of ["command", "path", "content", "url", "input", "script", "patch"]) {
    const val = String(params[field] ?? "")
    if (val) parts.push(val)
  }
  const combined = parts.join(" ").toLowerCase()
  const hits = [...RECALL_KEYWORDS].filter(kw => combined.includes(kw))
  if (hits.length > 0) {
    return `${toolName} ${hits.slice(0, 5).join(" ")}`
  }
  return ""
}

function needsPlan(toolName: string, params: Record<string, unknown>): boolean {
  // Read-only tools: always free
  if (ALWAYS_ALLOW_TOOLS.has(toolName)) return false

  // Terminal/exec commands: check for system-level commands
  const command = String(params.command ?? "").toLowerCase()
  for (const trigger of PLAN_REQUIRED_COMMANDS) {
    if (command.includes(trigger)) return true
  }

  // Write/edit to config files
  const path = String(params.path ?? "")
  if (path && (path.includes("openclaw.json") || path.includes("config.yaml"))) {
    return true
  }

  // Gateway config changes
  const action = String(params.action ?? "")
  if (toolName === "gateway" && ["config.patch", "config.apply", "restart"].includes(action)) {
    return true
  }

  return false
}

function isProtectedPath(path: string): boolean {
  return PROTECTED_PATHS.some(p => path.includes(p))
}

// ── Guardrail Checks ─────────────────────────────────────────────

interface GuardrailResult {
  block: boolean
  reason?: string
}

function checkGuardrails(toolName: string, params: Record<string, unknown>): GuardrailResult {
  // Block rm -rf on protected paths
  if (toolName === "exec") {
    const command = String(params.command ?? "")
    if (command.includes("rm") && command.includes("-rf")) {
      for (const p of PROTECTED_PATHS) {
        if (command.includes(p)) {
          return {
            block: true,
            reason: `BLOCKED: rm -rf auf ${p} ist verboten. Nie kritische Pfade löschen.`,
          }
        }
      }
    }

    // Block kill/pkill on ollama
    if ((command.includes("kill") || command.includes("pkill")) && command.includes("ollama")) {
      return {
        block: true,
        reason: "BLOCKED: Ollama killen = alle Agenten tot. Erst Nexus Memory lesen, Config-Chain prüfen, Nebo GO holen.",
      }
    }
  }

  // Block write/edit on openclaw.json without plan
  if (toolName === "write" || toolName === "edit" || toolName === "apply_patch") {
    const path = String(params.path ?? "")
    if (path.includes("openclaw.json")) {
      // We don't block outright — plan-zwang handles this
      // But we do block full overwrites (write) vs patches (edit/apply_patch)
      if (toolName === "write") {
        return {
          block: true,
          reason: "BLOCKED: openclaw.json darf nicht mit write() überschrieben werden. edit() oder gateway config.patch nutzen.",
        }
      }
    }
  }

  // Block gateway config.apply (full replace is dangerous)
  if (toolName === "gateway" && String(params.action ?? "") === "config.apply") {
    return {
      block: true,
      reason: "BLOCKED: config.apply (full replace) ist gefährlich. config.patch (merge) nutzen.",
    }
  }

  return { block: false }
}

// ── Pre-Action Recall ────────────────────────────────────────────

async function preActionRecall(
  embedder: Embedder,
  qdrantClient: QdrantClient,
  cfg: NexusConfig,
  query: string,
): Promise<string | null> {
  try {
    const queryVector = await embedder.embed(query)
    const results = await qdrantClient.search(queryVector, NEXUS_QUERY_LIMIT, cfg.accessLevel)

    if (results.length === 0) return null

    const lines = results.map(r => {
      const pct = r.score != null ? `[${Math.round(r.score * 100)}%]` : ""
      const category = r.category ? `[${r.category}]` : ""
      return `- ${category} ${r.text.slice(0, 400)} ${pct}`.trim()
    })

    return `🧠 VORBEREITUNG-GATE (pre-action recall):\nDu planst: ${query}\nRelevante Memories:\n${lines.join("\n")}\nNUTZE diesen Kontext fuer deine Aktion.`
  } catch (err) {
    log.error("pre-action recall failed", err)
    return null
  }
}

// ── Main Hook Handler ────────────────────────────────────────────

export function buildPreToolGateHandler(
  embedder: Embedder,
  qdrantClient: QdrantClient,
  cfg: NexusConfig,
) {
  return async (
    event: Record<string, unknown>,
    ctx?: Record<string, unknown>,
  ) => {
    const toolName = String(event.toolName ?? "")
    const params = (event.params ?? {}) as Record<string, unknown>

    log.info(`nexus-gate: before_tool_call — tool=${toolName}`)

    // ── 1. GUARDRAILS (erst blocken, dann informieren) ──
    const guardrail = checkGuardrails(toolName, params)
    if (guardrail.block) {
      log.warn(`nexus-gate: BLOCKED — ${guardrail.reason}`)
      return {
        block: true,
        blockReason: guardrail.reason,
      }
    }

    // ── 2. PRE-ACTION RECALL (Nexus mit Tool-Parametern fragen) ──
    const recallQuery = extractRecallQuery(toolName, params)
    let recallContext: string | null = null
    if (recallQuery) {
      log.info(`nexus-gate: pre-action recall — query="${recallQuery}"`)
      recallContext = await preActionRecall(embedder, qdrantClient, cfg, recallQuery)
    }

    // ── 3. PLAN-ZWANG (System-Kommandos ohne Plan = BLOCK) ──
    // Plan-Lock schreiben ist immer erlaubt (sonst Deadlock)
    if (toolName === "write" && String(params.path ?? "") === PLAN_LOCK_PATH) {
      return {} // allow
    }

    if (needsPlan(toolName, params) && !hasValidPlan()) {
      const reason = [
        "VORBEREITUNG-GATE: Du planst eine nicht-triviale Aktion ohne Plan.",
        "",
        "BEVOR du losrennst:",
        "1. Was ist das Ziel? Welcher Weg ist der beste?",
        "2. Welche Tools brauchst du? Was fehlt dir?",
        "3. Was koennte danach kaputt sein?",
        "",
        `Schreibe deinen Plan nach ${PLAN_LOCK_PATH} (write tool).`,
        "Der Plan ist 5 Minuten gueltig.",
      ].join("\n")

      const fullReason = recallContext ? `${reason}\n\n${recallContext}` : reason

      log.warn(`nexus-gate: BLOCKED (no plan) — tool=${toolName}`)
      return {
        block: true,
        blockReason: fullReason,
      }
    }

    // ── 4. RECALL-OUTPUT (Aktion erlaubt, aber Kontext injizieren) ──
    if (recallContext) {
      log.info(`nexus-gate: injecting recall context (${recallContext.length} chars)`)
      // OpenClaw before_tool_call doesn't support prependContext like before_prompt_build,
      // but we can modify params to include context in the tool result or return as metadata
      // For now, we return it as part of the hook result for the framework to inject
      return {
        params,
        _nexusRecallContext: recallContext,
      }
    }

    // ── 5. ALLOW ──
    return {}
  }
}