import type { QdrantClient } from "./lib/qdrant-client.ts"
import { buildUpdateNudgeLines, type UpdateCheckResult } from "./lib/update-check.ts"
import { log } from "./logger.ts"

type MemoryProviderStatus = {
  backend: "builtin" | "qmd"
  provider: string
  model?: string
  files?: number
  chunks?: number
  custom?: Record<string, unknown>
}

type MemoryEmbeddingProbeResult = {
  ok: boolean
  error?: string
}

type MemorySyncProgressUpdate = {
  completed: number
  total: number
  label?: string
}

type RegisteredMemorySearchManager = {
  status(): MemoryProviderStatus
  probeEmbeddingAvailability(): Promise<MemoryEmbeddingProbeResult>
  probeVectorAvailability(): Promise<boolean>
  sync?(params?: {
    reason?: string
    force?: boolean
    sessionFiles?: string[]
    progress?: (update: MemorySyncProgressUpdate) => void
  }): Promise<void>
  close?(): Promise<void>
}

type MemoryRuntimeBackendConfig =
  | { backend: "builtin" }
  | { backend: "qmd"; qmd?: { command?: string } }

/** Minimal structural view of the embedder — keeps this module decoupled/testable. */
type EmbeddingPinger = {
  embed(text: string): Promise<number[]>
}

type MemoryPluginRuntime = {
  getMemorySearchManager(params: {
    cfg: unknown
    agentId: string
    purpose?: "default" | "status"
  }): Promise<{
    manager: RegisteredMemorySearchManager | null
    error?: string
  }>
  resolveMemoryBackendConfig(params: {
    cfg: unknown
    agentId: string
  }): MemoryRuntimeBackendConfig
  closeAllMemorySearchManagers?(): Promise<void>
}

/**
 * How long a real point count is considered fresh. `status()` is a sync
 * framework method (MemoryProviderStatus), so it can only report the last
 * observed count; past this age the value is re-fetched in the background.
 */
const COUNT_TTL_MS = 60_000

/**
 * The embed text used as a cheap liveness ping. The probe only needs to know
 * whether embed() throws — the vector itself is discarded.
 */
const EMBED_PROBE_TEXT = "ping"

function createSearchManager(
  client: QdrantClient,
  embedder: EmbeddingPinger,
): RegisteredMemorySearchManager & { refreshStatus(): Promise<void> } {
  // Last OBSERVED real point count. null → none observed yet.
  let observedCount: number | null = null
  let observedAt = 0
  let inFlight: Promise<void> | null = null

  const refreshStatus = async (): Promise<void> => {
    if (inFlight) return inFlight
    inFlight = (async () => {
      const count = await client.countPoints()
      if (count !== null) {
        observedCount = count
        observedAt = Date.now()
      }
    })().finally(() => {
      inFlight = null
    })
    return inFlight
  }

  return {
    /** Async refresh backing the sync `status()` snapshot (also used by tests). */
    async refreshStatus() {
      return refreshStatus()
    },

    status() {
      // H122: report a REAL point count, not hardcoded zeros. `status()` is
      // synchronous by framework contract, so this returns the last observed
      // count and kicks a background refresh when the snapshot is stale.
      if (observedCount === null || Date.now() - observedAt > COUNT_TTL_MS) {
        void refreshStatus()
      }
      const points = observedCount ?? 0
      return {
        backend: "builtin" as const,
        provider: "nexus-memory",
        model: "qdrant",
        files: points,
        chunks: points,
        custom: {
          transport: "qdrant-rest",
          // Meaning of the numbers above: a Qdrant point IS one memory chunk.
          // There is no separate file→chunk mapping, so `files` mirrors
          // `chunks` (both = collection points_count). count_observed=false
          // means no real count has been fetched yet and 0 is a placeholder.
          count_source: "qdrant:collections/{c}.points_count",
          count_observed: observedCount !== null,
          count_observed_at: observedCount !== null ? observedAt : null,
        },
      }
    },

    async probeEmbeddingAvailability() {
      // H122: actually ping the configured embedder. The old version returned
      // a hardcoded { ok: true } while claiming "validated at startup" — which
      // this return value never proved.
      try {
        await embedder.embed(EMBED_PROBE_TEXT)
        return { ok: true }
      } catch (err) {
        return { ok: false, error: err instanceof Error ? err.message : String(err) }
      }
    },

    async probeVectorAvailability() {
      // H122: reachability of the vector store is exactly "can we count the
      // collection" — delegate instead of always answering true.
      return (await client.countPoints()) !== null
    },

    async sync() {
      // intentionally stateless (server-side backend): the plugin keeps no
      // local index to sync, so there is nothing to do here.
    },

    async close() {
      // intentionally stateless (server-side backend): no local handles to
      // release; the shared QdrantClient outlives this manager.
    },
  }
}

export function buildMemoryRuntime(
  client: QdrantClient,
  embedder: EmbeddingPinger,
): MemoryPluginRuntime {
  return {
    async getMemorySearchManager() {
      return { manager: createSearchManager(client, embedder) }
    },

    resolveMemoryBackendConfig() {
      // H122: `builtin` is the ONLY backend this plugin implements. The qmd
      // branch of MemoryRuntimeBackendConfig is unreachable here; say so
      // instead of pretending a backend was resolved.
      log.debug(
        "resolveMemoryBackendConfig: qmd backend not implemented; builtin is the only backend",
      )
      return { backend: "builtin" as const }
    },
  }
}

// Roadmap v0.13.1: update-check state (set async by index.ts, consumed here).
// `url` is carried for the cache format but unused at runtime (nudge text
// contains only the version).
let updateInfo: UpdateCheckResult | null = null
let updateNudged = false

export function setUpdateCheckResult(result: UpdateCheckResult): void {
  updateInfo = result
}

/**
 * Consume the once-per-process update nudge (H138).
 *
 * Returns the nudge text the first time it is called while an update is
 * available, then `{ text: null }` for the rest of the process. When no update
 * is available it returns `{ text: null }` WITHOUT consuming, so a later
 * arrival (the async check resolving) can still nudge exactly once.
 *
 * This is the only mutation of the nudge state; `buildPromptSection` itself is
 * pure, which makes the once-per-process behavior deterministically testable.
 */
/** Shared tool-name constants — single source of truth. OCR-6 (L941):
 *  the names used to be hardcoded in buildPromptSection, index.ts's
 *  availableTools checks, AND the tools' defaults; a rename in one place
 *  silently disabled the prompt section + update nudge for that tool. */
export const NEXUS_SEARCH_TOOL = "nexus_search"
export const NEXUS_STORE_TOOL = "nexus_store"

export function consumeUpdateNudge(): { text: string | null; lines: string[] } {
  // OCR-6 (maintainability low, L953): the lines are returned alongside the
  // text so the caller hands the SAME derivation to buildPromptSection —
  // previously promptBuilder and buildPromptSection each re-derived the nudge
  // lines from updateInfo through two independent boolean checks that could
  // drift apart (once-per-process state depending on two derivations agreeing).
  if (updateNudged || !updateInfo?.available) return { text: null, lines: [] }
  const { lines } = buildUpdateNudgeLines(updateInfo, false)
  if (lines.length === 0) return { text: null, lines: [] }
  updateNudged = true
  return { text: lines.join("\n").trim(), lines }
}

export function buildPromptSection(params: {
  availableTools: Set<string>
  /** OCR-6 (L953): pre-computed nudge lines from consumeUpdateNudge() — the
   *  single derivation shared between the caller and this function. When
   *  omitted the fallback re-derives from updateInfo (nudged flag). */
  nudgeLines?: string[]
  /**
   * True when this process has already emitted the update nudge. The caller
   * owns that decision (consumeUpdateNudge); this function never mutates it,
   * so identical inputs always produce identical output.
   */
  nudged?: boolean
}): string[] {
  const hasSearch = params.availableTools.has(NEXUS_SEARCH_TOOL)
  const hasStore = params.availableTools.has(NEXUS_STORE_TOOL)
  if (!hasSearch && !hasStore) return []

  const lines: string[] = [
    "## Memory (Nexus)",
    "",
    "Memory is managed by Nexus Memory (Qdrant). Do not read or write local memory files like MEMORY.md or memory/*.md — they do not exist.",
    "Relevant memories are automatically injected at the start of each conversation.",
    "",
  ]

  if (hasSearch) {
    lines.push(
      `Use ${NEXUS_SEARCH_TOOL} to look up prior conversations, preferences, and facts.`,
    )
  }
  if (hasStore) {
    lines.push(
      `Use ${NEXUS_STORE_TOOL} to save important information the user asks you to remember.`,
    )
  }

  // Roadmap v0.13.1: once-per-process update nudge (fail-open, no throw).
  // H138: reuses buildUpdateNudgeLines (single source of the nudge string) and
  // reads `updateInfo` read-only — the old version mutated a module global
  // here, which made the second prompt section nondeterministic.
  // OCR-6 (L953): nudge lines come from the CALLER (the same consumeUpdateNudge
  // derivation, single source of truth) when provided; the re-derivation from
  // module-global updateInfo remains as the fallback for direct callers.
  if (params.nudgeLines && params.nudgeLines.length > 0) {
    lines.push(...params.nudgeLines)
  } else if (updateInfo) {
    const { lines: nudgeLines } = buildUpdateNudgeLines(updateInfo, params.nudged === true)
    lines.push(...nudgeLines)
  }

  return lines
}
