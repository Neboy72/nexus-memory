import { detectProvider, type EmbeddingProvider } from "./embedder.ts"

export type AccessLevel = "public" | "trusted" | "private"

export type EmbeddingConfig = {
  provider: EmbeddingProvider | undefined
  model: string | undefined
  apiKey: string | undefined
  baseUrl: string | undefined
  dimensions: number | undefined
}

export type NexusConfig = {
  qdrantUrl: string
  collection: string
  embedding: EmbeddingConfig
  autoRecall: boolean
  autoCapture: boolean
  thoughtFilter: boolean
  maxRecallResults: number
  accessLevel: AccessLevel
  /** Project/agent area label (unreleased): auto-recall surfaces only
   *  'default'-scoped memories plus this agent's own scope. Explicit
   *  search (nexus_search) is never scope-filtered. Empty = no gating. */
  scope: string
  debug: boolean
}

const ALLOWED_KEYS = [
  "qdrantUrl",
  "collection",
  "embedding",
  "autoRecall",
  "autoCapture",
  "thoughtFilter",
  "maxRecallResults",
  "accessLevel",
  "scope",
  "debug",
]

const ALLOWED_EMBEDDING_KEYS = [
  "provider",
  "model",
  "apiKey",
  "baseUrl",
  "dimensions",
]

const VALID_ACCESS_LEVELS: AccessLevel[] = ["public", "trusted", "private"]

const VALID_PROVIDERS: EmbeddingProvider[] = ["voyage", "openai", "ollama", "google", "jina"]

function assertAllowedKeys(
  value: Record<string, unknown>,
  allowed: string[],
  label: string,
): void {
  const unknown = Object.keys(value).filter((k) => !allowed.includes(k))
  if (unknown.length > 0) {
    throw new Error(`${label} has unknown keys: ${unknown.join(", ")}`)
  }
}

function resolveEnvVars(value: string): string {
  return value.replace(/\$\{([^}]+)\}/g, (_, envVar: string) => {
    const envValue = process.env[envVar]
    if (!envValue) {
      throw new Error(`Environment variable ${envVar} is not set`)
    }
    return envValue
  })
}

export const DEFAULT_QDRANT_URL = "http://localhost:6333"
export const DEFAULT_COLLECTION = "nexus"

export function parseConfig(raw: unknown): NexusConfig {
  const cfg =
    raw && typeof raw === "object" && !Array.isArray(raw)
      ? (raw as Record<string, unknown>)
      : {}

  if (Object.keys(cfg).length > 0) {
    assertAllowedKeys(cfg, ALLOWED_KEYS, "nexus-memory config")
  }

  // Parse embedding sub-config
  let embedding: EmbeddingConfig = {
    provider: undefined,
    model: undefined,
    apiKey: undefined,
    baseUrl: undefined,
    dimensions: undefined,
  }

  if (cfg.embedding && typeof cfg.embedding === "object" && !Array.isArray(cfg.embedding)) {
    const emb = cfg.embedding as Record<string, unknown>
    assertAllowedKeys(emb, ALLOWED_EMBEDDING_KEYS, "nexus-memory embedding config")

    let provider: EmbeddingProvider | undefined
    if (typeof emb.provider === "string") {
      if (!VALID_PROVIDERS.includes(emb.provider as EmbeddingProvider)) {
        throw new Error(
          `Invalid embedding provider "${emb.provider}". Valid: ${VALID_PROVIDERS.join(", ")}`,
        )
      }
      provider = emb.provider as EmbeddingProvider
    }

    let apiKey: string | undefined
    if (typeof emb.apiKey === "string" && emb.apiKey.length > 0) {
      try {
        apiKey = resolveEnvVars(emb.apiKey)
      } catch {
        apiKey = undefined
      }
    }

    embedding = {
      provider,
      model: typeof emb.model === "string" ? emb.model : undefined,
      apiKey,
      baseUrl: typeof emb.baseUrl === "string" ? emb.baseUrl : undefined,
      dimensions: typeof emb.dimensions === "number" ? emb.dimensions : undefined,
    }
  }

  // If provider not set in config, try auto-detect from env
  if (!embedding.provider) {
    const detected = detectProvider()
    if (detected) embedding.provider = detected
  }

  // Parse access level
  let accessLevel: AccessLevel = "public"
  if (typeof cfg.accessLevel === "string") {
    if (!VALID_ACCESS_LEVELS.includes(cfg.accessLevel as AccessLevel)) {
      throw new Error(
        `Invalid access level "${cfg.accessLevel}". Valid: ${VALID_ACCESS_LEVELS.join(", ")}`,
      )
    }
    accessLevel = cfg.accessLevel as AccessLevel
  }

  // Parse qdrantUrl with env var resolution
  let qdrantUrl = DEFAULT_QDRANT_URL
  if (typeof cfg.qdrantUrl === "string" && cfg.qdrantUrl.trim()) {
    try {
      qdrantUrl = resolveEnvVars(cfg.qdrantUrl.trim())
    } catch {
      qdrantUrl = DEFAULT_QDRANT_URL
    }
  }
  // Also check NEXUS_QDRANT_URL env var
  if (qdrantUrl === DEFAULT_QDRANT_URL && process.env.NEXUS_QDRANT_URL) {
    qdrantUrl = process.env.NEXUS_QDRANT_URL
  }

  // Parse scope (project/agent area label, unreleased).
  // Fail-open: empty/invalid → "" (no gating, old behavior).
  let scope = ""
  if (typeof cfg.scope === "string" && cfg.scope.trim()) {
    const s = cfg.scope.trim().toLowerCase()
    if (/^[a-z0-9][a-z0-9-]{0,39}$/.test(s)) {
      scope = s
    }
  }
  if (!scope && process.env.NEXUS_SCOPE) {
    const s = process.env.NEXUS_SCOPE.trim().toLowerCase()
    if (/^[a-z0-9][a-z0-9-]{0,39}$/.test(s)) {
      scope = s
    }
  }

  return {
    qdrantUrl,
    collection: typeof cfg.collection === "string" && cfg.collection.trim()
      ? cfg.collection.trim()
      : DEFAULT_COLLECTION,
    embedding,
    autoRecall: (cfg.autoRecall as boolean) ?? true,
    autoCapture: (cfg.autoCapture as boolean) ?? true,
    thoughtFilter: (cfg.thoughtFilter as boolean) ?? true,
    maxRecallResults: (cfg.maxRecallResults as number) ?? 10,
    accessLevel,
    scope,
    debug: (cfg.debug as boolean) ?? false,
  }
}

export const nexusConfigSchema = {
  jsonSchema: {
    type: "object",
    additionalProperties: false,
    properties: {
      qdrantUrl: { type: "string" },
      collection: { type: "string" },
      embedding: {
        type: "object",
        properties: {
          provider: { type: "string", enum: VALID_PROVIDERS },
          model: { type: "string" },
          apiKey: { type: "string" },
          baseUrl: { type: "string" },
          dimensions: { type: "number" },
        },
      },
      autoRecall: { type: "boolean" },
      autoCapture: { type: "boolean" },
      thoughtFilter: { type: "boolean" },
      maxRecallResults: { type: "number" },
      accessLevel: { type: "string", enum: VALID_ACCESS_LEVELS },
      scope: { type: "string" },
      debug: { type: "boolean" },
    },
  },
  parse: parseConfig,
}