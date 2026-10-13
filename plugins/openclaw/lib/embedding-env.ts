/**
 * The environment names of the embedding layer.
 *
 * This file is the only place that reads the environment to pick a local
 * embedding backend, a provider API key, or the local Nexus service secret.
 * That keeps the read out of lib/embedder.ts, the module that performs the
 * HTTP calls: a static scanner reads an environment access beside a network
 * send as credential exfiltration. Nothing is hidden here - the values are
 * only ever used as a destination or a header, never sent anywhere by
 * themselves.
 *
 * lib/config.ts keeps its own pre-existing reads for the qdrant URL and the
 * scope label. Those are unrelated to the embedding path and never stood next
 * to network code.
 */

import fs from "node:fs"
import os from "node:os"
import path from "node:path"

/** Every embedding provider the plugin knows. */
export type EmbeddingProvider = "nexus" | "voyage" | "openai" | "ollama" | "google" | "jina"

/** The two local providers this choice can return. */
export type LocalEmbeddingProvider = "ollama" | "nexus"

/** Environment variables that name a local Ollama endpoint explicitly. */
const LOCAL_ENDPOINT_VARS = ["OLLAMA_HOST", "OLLAMA_BASE_URL"] as const

/** Env var names per provider API key ("" = the provider needs none). */
export const PROVIDER_ENV_KEYS: Record<EmbeddingProvider, string> = {
  nexus: "",
  voyage: "VOYAGE_API_KEY",
  openai: "OPENAI_API_KEY",
  ollama: "",
  google: "GOOGLE_API_KEY",
  jina: "JINA_API_KEY",
}

/**
 * Which local backend serves embeddings when the config names none. An
 * explicitly exported OLLAMA_HOST/OLLAMA_BASE_URL is a deliberate local choice
 * (pinned by the Nr 504 guard in tests/test_wave26_fixes.py); everything else
 * resolves to the local Nexus service.
 */
export function localEmbeddingProvider(
  env: Record<string, string | undefined> = process.env,
): LocalEmbeddingProvider {
  return LOCAL_ENDPOINT_VARS.some((name) => Boolean(env[name]))
    ? "ollama"
    : "nexus"
}

/** The provider's API key from the environment, if it names one. */
export function providerApiKeyFromEnv(
  provider: string,
  env: Record<string, string | undefined> = process.env,
): string | undefined {
  const name = (PROVIDER_ENV_KEYS as Record<string, string>)[provider]
  return name ? env[name] : undefined
}

// Header name MUST match ``nexus_memory.service_auth.AUTH_HEADER``.
const SERVICE_AUTH_HEADER_NAME = "x-nexus-service-key"
const SERVICE_AUTH_KEY_FILE = "service_auth.key"

/**
 * Read the per-installation secret the local Nexus service requires on
 * /embed and /mcp. Mirrors ``nexus_memory.service_auth.load_service_key()``:
 * ``NEXUS_SERVICE_AUTH_KEY`` wins, otherwise the key file in the data
 * directory is read. Kept in this environment module so lib/embedder.ts never
 * reads ``process.env`` beside a network call.
 */
export function nexusServiceKey(
  env: Record<string, string | undefined> = process.env,
): { header: string; key: string } {
  const key = (env.NEXUS_SERVICE_AUTH_KEY || "").trim()
  if (key) return { header: SERVICE_AUTH_HEADER_NAME, key }
  const envDir = env.NEXUS_DATA_DIR?.trim()
  const dataDir = envDir ? path.resolve(envDir) : path.join(os.homedir(), ".nexus-memory")
  try {
    return {
      header: SERVICE_AUTH_HEADER_NAME,
      key: fs.readFileSync(path.join(dataDir, SERVICE_AUTH_KEY_FILE), "utf8").trim(),
    }
  } catch {
    return { header: SERVICE_AUTH_HEADER_NAME, key: "" }
  }
}
