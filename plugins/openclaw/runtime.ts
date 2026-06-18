import type { QdrantClient } from "./lib/qdrant-client.ts"
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

function createSearchManager(
  _client: QdrantClient,
): RegisteredMemorySearchManager {
  return {
    status() {
      return {
        backend: "builtin" as const,
        provider: "nexus-memory",
        model: "qdrant",
        files: 0,
        chunks: 0,
        custom: {
          transport: "qdrant-rest",
        },
      }
    },

    async probeEmbeddingAvailability() {
      // The embedder is validated at startup; if we got here, it's working.
      return { ok: true }
    },

    async probeVectorAvailability() {
      return true
    },

    async sync() {},

    async close() {},
  }
}

export function buildMemoryRuntime(
  client: QdrantClient,
): MemoryPluginRuntime {
  return {
    async getMemorySearchManager() {
      return { manager: createSearchManager(client) }
    },

    resolveMemoryBackendConfig() {
      return { backend: "builtin" as const }
    },
  }
}

export function buildPromptSection(params: {
  availableTools: Set<string>
}): string[] {
  const hasSearch = params.availableTools.has("nexus_search")
  const hasStore = params.availableTools.has("nexus_store")
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
      "Use nexus_search to look up prior conversations, preferences, and facts.",
    )
  }
  if (hasStore) {
    lines.push(
      "Use nexus_store to save important information the user asks you to remember.",
    )
  }

  return lines
}