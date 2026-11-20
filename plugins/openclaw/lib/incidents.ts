/**
 * Incident reporting for the nightly learner (0.22.62).
 *
 * Nexus learns from what agents report, not from reading their session
 * stores. This module is the OpenClaw side of that: one failed tool call
 * becomes one line in the service's journal (`POST /events`).
 *
 * Two rules it keeps:
 *
 * 1. It never guesses. Only the host's own failure marks count
 *    (`error` on the event, `isError`, or a non-zero `exitCode`). Which
 *    error class that is, and whether it deserves a place in the memory,
 *    is decided in Nexus — one place, one language, one set of rules.
 * 2. It never blocks. A missing incident costs nothing; a turn waiting on
 *    a reporting HTTP call costs the user time. Failures are logged at
 *    debug level and dropped.
 */
import type { NexusConfig } from "./config.ts"
import { NEXUS_HARNESS, ensureNexusAgentKey, fetchWithTimeout } from "./embedder.ts"
import { log } from "../logger.ts"

/** One report line, as `POST /events` takes it. */
export type ToolIncident = {
  tool: string
  ok: false
  error: string
  hint: string
  session: string
}

/** The part of the host's `after_tool_call` event this module reads. */
export type ToolCallOutcome = {
  toolName?: string
  result?: unknown
  error?: unknown
  session?: string
  maxChars?: number
}

const DEFAULT_MAX_CHARS = 2000
const REPORT_TIMEOUT_MS = 4000

function collapse(text: unknown): string {
  return String(text ?? "")
    .replace(/\s+/g, " ")
    .trim()
}

/** Pull the readable error text out of whatever the tool returned. */
function failureText(result: unknown): string {
  if (result === undefined || result === null) return ""
  if (typeof result === "string") return collapse(result)
  if (typeof result !== "object") return ""
  const record = result as Record<string, unknown>
  const pieces = [record.output, record.content, record.error, record.message]
    .filter((value) => typeof value === "string" && value.trim())
    .map((value) => collapse(value))
  return collapse(pieces.join(" "))
}

/** Did the host mark this call as failed? */
function isFailure(result: unknown, error: unknown): boolean {
  if (typeof error === "string" && error.trim()) return true
  if (error !== undefined && error !== null && typeof error !== "string") return true
  if (result && typeof result === "object") {
    const record = result as Record<string, unknown>
    if (record.isError === true || record.is_error === true) return true
    for (const key of ["exitCode", "exit_code"]) {
      const code = record[key]
      if (typeof code === "number" && code !== 0) return true
    }
    if (typeof record.error === "string" && record.error.trim()) return true
  }
  return false
}

/**
 * One incident, or null when the call did not fail (or has no name).
 *
 * `agent` is deliberately absent: the service names the agent from the key
 * that writes, so a plugin cannot claim to report for someone else.
 */
export function incidentFromToolCall(outcome: ToolCallOutcome): ToolIncident | null {
  const tool = collapse(outcome.toolName).slice(0, 64)
  if (!tool) return null
  if (!isFailure(outcome.result, outcome.error)) return null
  const text = collapse(
    `${failureText(outcome.result)} ${
      typeof outcome.error === "string" ? collapse(outcome.error) : ""
    }`,
  )
  if (!text) return null
  return {
    tool,
    ok: false,
    error: text.slice(0, Math.max(1, outcome.maxChars ?? DEFAULT_MAX_CHARS)),
    hint: "",
    session: collapse(outcome.session).slice(0, 128),
  }
}

/**
 * Where the service lives. In order: the write gate (it always names the
 * service), then the embedding base URL when it IS the nexus service, then
 * the default port. An Ollama base URL is never mistaken for the service.
 */
function serviceBase(cfg: NexusConfig): string {
  const gateUrl = cfg.writeGate?.url
  if (typeof gateUrl === "string" && gateUrl.trim()) {
    return gateUrl.trim().replace(/\/+$/, "")
  }
  const embedding = cfg.embedding
  if (embedding?.provider === "nexus" && typeof embedding.baseUrl === "string") {
    if (embedding.baseUrl.trim()) return embedding.baseUrl.trim().replace(/\/+$/, "")
  }
  return "http://127.0.0.1:9122"
}

/**
 * Send one incident. Returns whether the service took it; never throws.
 *
 * Uses the agent key (self-enrolling from the installation key when needed),
 * so the journal names this harness and not "unknown".
 */
export async function reportIncident(
  cfg: NexusConfig,
  incident: ToolIncident,
): Promise<boolean> {
  const base = serviceBase(cfg)
  try {
    const key = await ensureNexusAgentKey(base, cfg.writeGate?.key)
    if (!key) {
      log.debug("nexus-incidents: no agent key — incident not reported")
      return false
    }
    const response = await fetchWithTimeout(
      `${base}/events`,
      {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "x-nexus-service-key": key,
        },
        body: JSON.stringify(incident),
      },
      REPORT_TIMEOUT_MS,
    )
    if (!response.ok) {
      log.debug("nexus-incidents: /events answered %s", response.status)
      return false
    }
    return true
  } catch (error) {
    log.debug("nexus-incidents: /events failed: %s", String(error))
    return false
  }
}

/** Only used in logs and tests; keeps the harness name in one place. */
export const INCIDENT_HARNESS = NEXUS_HARNESS

/** Per-session cap: a broken tool in a loop must not flood the journal. */
const DEFAULT_MAX_PER_SESSION = 50

/**
 * Build the `after_tool_call` handler.
 *
 * The host calls it for every tool; this returns a promise that settles even
 * when the service is unreachable, so a hook timeout can never cost a turn.
 */
export function buildIncidentHandler(
  cfg: NexusConfig,
  options: { maxPerSession?: number; maxChars?: number } = {},
) {
  const maxPerSession = options.maxPerSession ?? DEFAULT_MAX_PER_SESSION
  const counts = new Map<string, number>()
  return async (event: unknown, ctx?: unknown): Promise<void> => {
    const e = (event ?? {}) as ToolCallOutcome
    const context = (ctx ?? {}) as {
      toolName?: string
      sessionKey?: string
      sessionId?: string
      agentId?: string
    }
    const session = collapse(
      context.sessionKey ?? context.sessionId ?? context.agentId ?? "",
    )
    const incident = incidentFromToolCall({
      toolName: e.toolName ?? context.toolName,
      result: e.result,
      error: e.error,
      session,
      maxChars: options.maxChars,
    })
    if (!incident) return
    const count = (counts.get(session) ?? 0) + 1
    if (count > maxPerSession) return
    counts.set(session, count)
    await reportIncident(cfg, incident)
  }
}
