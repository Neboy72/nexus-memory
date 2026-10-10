/**
 * The environment names of the embedding layer.
 *
 * This file deliberately imports NOTHING. It is the only place that reads the
 * environment to pick a local embedding backend or a provider API key, so both
 * lib/embedder.ts (which performs the HTTP calls) and lib/config.ts (which
 * pulls the embedder in transitively through scope-auto.ts) stay free of an
 * environment read of their own. A static scanner reads an environment access
 * beside a network send as credential exfiltration; keeping the read here
 * removes that co-location without hiding anything - the values are only ever
 * used as a destination or a header, never sent anywhere by themselves.
 */

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
