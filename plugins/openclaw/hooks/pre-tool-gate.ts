/**
 * Pre-Tool Gate — Vorbereitung vor Aktionen (adaptiert von Kioshas nexus-gate.py)
 *
 * Drei Ebenen, in dieser Reihenfolge:
 *
 * 1. GUARDRAILS — Config/Kill/Delete ohne GO = sofort BLOCK
 * 2. PRE-ACTION RECALL — Bei Keywords in Tool-Params → Nexus fragen.
 *    Der Kontext wird NUR in einen Block-Grund (blockReason) eingehängt;
 *    before_tool_call kann keinen Kontext injizieren (siehe H135 unten).
 * 3. PLAN-ZWANG — System-Kommandos ohne Plan-Lock = BLOCK (mit Recall-Kontext als Info)
 *
 * Read-only Tools (read, web_search, web_fetch, image, pdf, etc.) brauchen keinen Plan.
 *
 * Der Plan-Lock ist eine Datei die der Agent schreiben muss bevor er nicht-triviale
 * Aktionen ausführt. Sie ist 5 Minuten gültig. Das zwingt ihn zu denken bevor er handelt.
 */

import { lstatSync, readFileSync, realpathSync, unlinkSync } from "node:fs"
import os from "node:os"
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
// (tilde entries are expanded below; absolute entries are portable via os.homedir())
const PROTECTED_PATHS = [
  "~/.openclaw",
  "~/.hermes",
  "~/nexus-memory",
]
// W25 Nr-491-adjacent: developer-home absolute paths are not portable —
// resolve from the runtime user's home instead of hardcoding one machine.
for (const p of ["~/.openclaw", "~/.hermes", "~/nexus-memory"]) {
  PROTECTED_PATHS.push(p.replace("~", os.homedir()))
}

// ── Helpers ──────────────────────────────────────────────────────

/**
 * W29-3: `rm` with recursive+force flags, tolerant of flag order and the long
 * forms. The old `command.includes("rm") && command.includes("-rf")` check
 * missed `rm -fr`, `rm -Rf`, `rm --recursive --force`.
 */
function looksLikeRecursiveRm(command: string): boolean {
  return /(?:^|[\s;&(])rm\s+(?:-{1,2}[a-z]*r[a-z]*f|-[a-z]*f[a-z]*r|--recursive(?:\s+(?:-{1,2}force)?)?|--force\s+--recursive)\b/i.test(
    command,
  )
}

/**
 * W29-3: resolve `$HOME` / `${HOME}` / `~/` literals for the path COMPARISON
 * only (never for execution). `isProtectedPath` is a substring check, so an
 * unexpanded `$HOME/.openclaw` never matched the concrete home-anchored
 * entries in PROTECTED_PATHS.
 */
function expandHomeLiterals(command: string): string {
  const home = os.homedir()
  return command
    .replace(/\$\{?HOME\}?/gi, home)
    .replace(/(^|[\s;&(=])~(?=\/|$)/g, (_m, prefix: string) => `${prefix}${home}`)
}

/**
 * W29-4: commands do not only arrive in `params.command`. Exec-style tools
 * also pass `input`/`script`/`patch`/`cmd`; concatenate every present string
 * field (fixed order, join " ") so needsPlan and checkGuardrails see the same
 * text. extractRecallQuery already scanned this broader field set.
 */
function firstCommand(params: Record<string, unknown>): string {
  const parts: string[] = []
  for (const field of ["command", "input", "script", "patch", "cmd"]) {
    const val = params[field]
    if (typeof val === "string" && val) parts.push(val)
  }
  return parts.join(" ")
}

function hasValidPlan(): boolean {
  try {
    // Fix 27.08.2026: direkter node:fs-Import statt Deno/require-Shim.
    // Der alte Shim ((globalThis as any).Deno?.statSync ?? (globalThis as any).require?.("fs")?.statSync)
    // resolvierte im OpenClaw-Gateway (Node, ESM) zu undefined —
    // hasValidPlan() war dadurch IMMER false, der Plan-Lock wurde nie erkannt.
    //
    // W29-5: the old check was mtime-only, so an empty/touched file passed and
    // a symlink swap between stat and unlink was possible. Hardened:
    //  - lstatSync: a symlink (or non-regular file) is invalid — never follow
    //    it, just drop the link and reject;
    //  - the lock is a CONTRACT (written by the agent via write_file): its
    //    content MUST start with `plan:`;
    //  - unlink only after the realpath comparison shows the file we read is
    //    still the one at the path (no TOCTOU delete of a swapped file).
    const lst = lstatSync(PLAN_LOCK_PATH) // wirft, wenn Datei fehlt
    if (lst.isSymbolicLink() || !lst.isFile()) {
      try {
        unlinkSync(PLAN_LOCK_PATH)
      } catch {}
      return false
    }

    const realPathBefore = realpathSync(PLAN_LOCK_PATH)
    const content = readFileSync(PLAN_LOCK_PATH, "utf8")
    const realPathAfter = realpathSync(PLAN_LOCK_PATH)
    if (realPathBefore !== realPathAfter) {
      // TOCTOU: the file we read is not the one currently at the path.
      return false
    }

    if (!content.startsWith("plan:")) {
      try {
        unlinkSync(realPathAfter)
      } catch {}
      return false
    }

    const age = Date.now() - lst.mtimeMs
    if (age > PLAN_MAX_AGE_MS) {
      // Plan expired — clean up
      try {
        unlinkSync(realPathAfter)
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
  // W29-4: concatenate all command-carrying fields (command/input/script/...).
  const command = firstCommand(params).toLowerCase()
  for (const trigger of PLAN_REQUIRED_COMMANDS) {
    if (command.includes(trigger)) return true
  }

  // W29-3: PLAN_REQUIRED_COMMANDS matches literally ("rm -r"), so the
  // flag-order variants (`rm -fr`, `rm --recursive --force`) slipped through.
  if (looksLikeRecursiveRm(command)) return true

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
  // PROTECTED_PATHS mixes tilde and absolute forms — compare lowercased on
  // both sides so the match does not depend on the caller's case.
  const p = path.toLowerCase()
  return PROTECTED_PATHS.some(pp => p.includes(pp.toLowerCase()))
}

// ── Guardrail Checks ─────────────────────────────────────────────

interface GuardrailResult {
  block: boolean
  reason?: string
}

function checkGuardrails(toolName: string, params: Record<string, unknown>): GuardrailResult {
  // Block rm -rf on protected paths
  if (toolName === "exec") {
    // Normalize case: the keyword checks below are lowercase, so `RM -RF /x`
    // or `KILL ollama` must be lowercased here too — otherwise uppercase
    // commands bypass the guardrail entirely.
    // W29-4: read all command-carrying fields, not only params.command.
    const command = firstCommand(params).toLowerCase()
    if (looksLikeRecursiveRm(command)) {
      // W29-3: expand $HOME/${HOME}/~/ literals for the path COMPARISON so
      // `rm -rf $HOME/.openclaw` resolves to the real protected path. The raw
      // `command` stays as the cheap literal fast-path (isProtectedPath is a
      // substring check on the concrete PROTECTED_PATHS entries).
      const expanded = expandHomeLiterals(command)
      if (isProtectedPath(command) || isProtectedPath(expanded)) {
        return {
          block: true,
          reason: "BLOCKED: rm -rf auf einen geschützten Pfad ist verboten. Nie kritische Pfade löschen.",
        }
      }
    }

    // Block kill/pkill on ollama.
    // Wortgrenzen statt Substring: 'skill'/'grill'/'killfile' und Pfade wie
    // ~/ollama-notes dürfen NICHT als kill-auf-ollama fehlklassifiziert werden
    // (case-insensitive wie oben — command ist bereits lowercased).
    if (/\b(?:kill|pkill|killall)\b/i.test(command) && /\bollama\b/i.test(command)) {
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
      const pct = typeof r.score === "number" ? `[${Math.round(r.score * 100)}%]` : ""
      const category = r.category ? `[${r.category}]` : ""
      return `- ${category} ${(r.text ?? "").slice(0, 400)} ${pct}`.trim()
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

    // ── 4. RECALL-OUTPUT ──
    // H135: before_tool_call has exactly two valid return shapes — `{}` (allow)
    // and `{ block, blockReason }` (deny). The previous branch returned
    // `{ params, _nexusRecallContext }`: the framework does not inject either
    // field, and echoing `params` back could corrupt the tool arguments.
    //
    // The recall context is still delivered where it can be: on the block
    // paths above (embedded in blockReason) and, for the general case, by the
    // before_prompt_build hook (buildRecallHandler). The context computed for
    // THIS call — preActionRecall() has already paid the embed() round-trip —
    // is therefore discarded on the allow path; we log it at warn level so the
    // loss is visible instead of silent.
    if (recallContext) {
      log.warn(
        `nexus-gate: recall context (${recallContext.length} chars) cannot be injected by ` +
        `before_tool_call — delivered via before_prompt_build instead; dropping`,
      )
    }

    // ── 5. ALLOW ──
    return {}
  }
}