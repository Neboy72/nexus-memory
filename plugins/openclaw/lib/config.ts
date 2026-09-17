import { detectProvider, type EmbeddingProvider } from "./embedder.ts"
import { validateConfigScope } from "./scope-auto.ts"

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
  /** OCR-6 (maintainability low, L655): minimum similarity score the forget
   *  tool requires before deleting a query match (deployment-tunable).
   *  Default 0.8. */
  forgetMinScore: number
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
  "forgetMinScore",
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

/**
 * Expand ${ENV_VAR} references. Fail-closed: a missing — or intentionally
 * empty — variable is NOT silently resolved to undefined/"" (an empty API
 * key or a bogus Qdrant URL must break the load, not the first request).
 * `purpose` names the field so the caller can report which one it was.
 */
function resolveEnvVars(value: string, purpose?: string): string {
  return value.replace(/\$\{([^}]+)\}/g, (_, envVar: string) => {
    const envValue = process.env[envVar]
    if (!envValue) {
      throw new Error(
        `env var ${envVar} is not set (required for ${purpose ?? "config value"})`,
      )
    }
    return envValue
  })
}

/**
 * Coerce a config value to boolean.
 *
 * The old `(v as boolean) ?? dflt` cast left string values ("false", "0")
 * truthy. Accepted: real booleans; numbers (0 → false, non-zero → true);
 * and the usual string spellings case-insensitively. Anything else → dflt.
 */
function toBool(v: unknown, dflt: boolean): boolean {
  if (v === true || v === false) return v
  if (typeof v === "number") return Number.isFinite(v) ? v !== 0 : dflt
  if (typeof v === "string") {
    const s = v.trim().toLowerCase()
    if (s === "true" || s === "1" || s === "yes" || s === "on") return true
    if (s === "false" || s === "0" || s === "no" || s === "off" || s === "") return false
  }
  return dflt
}

/**
 * Coerce a config value to an integer clamped to [min, max].
 *
 * Decision: CLAMP (not reject). A non-numeric/NaN value → dflt; a numeric
 * value outside the range is clamped to the nearest bound so an over-large
 * limit still yields a usable, bounded result instead of silently resetting.
 * Fractions are truncated toward zero.
 */
function toClampedInt(v: unknown, dflt: number, min: number, max: number): number {
  const n = typeof v === "number" ? v : Number(v)
  if (!Number.isFinite(n)) return dflt
  return Math.min(max, Math.max(min, Math.trunc(n)))
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
      // Fail-closed: an unresolvable ${VAR} must surface, not become an
      // undefined key that silently disables the embedder.
      apiKey = resolveEnvVars(emb.apiKey, "embedding.apiKey")
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

  // Parse qdrantUrl with env var resolution. Fail-closed: an unresolvable
  // ${VAR} surfaces with the variable name instead of silently pointing at
  // the default URL (which would query the wrong store).
  let qdrantUrl = DEFAULT_QDRANT_URL
  if (typeof cfg.qdrantUrl === "string" && cfg.qdrantUrl.trim()) {
    qdrantUrl = resolveEnvVars(cfg.qdrantUrl.trim(), "qdrantUrl")
  }
  // Also check NEXUS_QDRANT_URL env var
  if (qdrantUrl === DEFAULT_QDRANT_URL && process.env.NEXUS_QDRANT_URL) {
    qdrantUrl = process.env.NEXUS_QDRANT_URL
  }

  // Parse scope (project/agent area label, unreleased).
  // Fail-open: empty/invalid → "" (no gating, old behavior).
  let scope = validateConfigScope(cfg.scope)
  if (!scope) {
    scope = validateConfigScope(process.env.NEXUS_SCOPE)
  }

  return {
    qdrantUrl,
    collection: typeof cfg.collection === "string" && cfg.collection.trim()
      ? cfg.collection.trim()
      : DEFAULT_COLLECTION,
    embedding,
    autoRecall: toBool(cfg.autoRecall, true),
    autoCapture: toBool(cfg.autoCapture, true),
    thoughtFilter: toBool(cfg.thoughtFilter, true),
    maxRecallResults: toClampedInt(cfg.maxRecallResults, 10, 1, 20),
    // OCR-6 (maintainability low, L655): forget-tool confidence threshold is
    // deployment-tunable — clamped into [0, 1] like the contract below.
    forgetMinScore:
      typeof cfg.forgetMinScore === "number" && Number.isFinite(cfg.forgetMinScore)
        ? Math.min(1, Math.max(0, cfg.forgetMinScore))
        : 0.8,
    accessLevel,
    scope,
    debug: toBool(cfg.debug, false),
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
      maxRecallResults: { type: "number", minimum: 1, maximum: 20 },
      forgetMinScore: { type: "number", minimum: 0, maximum: 1 },
      accessLevel: { type: "string", enum: VALID_ACCESS_LEVELS },
      scope: { type: "string" },
      debug: { type: "boolean" },
    },
  },
  parse: parseConfig,
}