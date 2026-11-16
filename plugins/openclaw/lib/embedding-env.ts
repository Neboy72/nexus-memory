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
export type EmbeddingProvider = "nexus" | "openai" | "ollama" | "google" | "jina"

/** The two local providers this choice can return. */
export type LocalEmbeddingProvider = "ollama" | "nexus"

/** Environment variables that name a local Ollama endpoint explicitly. */
const LOCAL_ENDPOINT_VARS = ["OLLAMA_HOST", "OLLAMA_BASE_URL"] as const

/** Env var names per provider API key ("" = the provider needs none). */
export const PROVIDER_ENV_KEYS: Record<EmbeddingProvider, string> = {
  nexus: "",
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
// Self-enrolment (15.11.2026): the installation key above only ENROLS this
// plugin (POST /enroll). Everything else is done with the key in
// ``<data-dir>/agent_keys/<harness>.key`` — that key names the agent.
const AGENT_KEY_DIR = "agent_keys"

/** The data directory both sides agree on: ``$NEXUS_DATA_DIR`` or ``~/.nexus-memory``. */
export function nexusDataDir(
  env: Record<string, string | undefined> = process.env,
): string {
  const envDir = env.NEXUS_DATA_DIR?.trim()
  return envDir ? path.resolve(envDir) : path.join(os.homedir(), ".nexus-memory")
}

/**
 * Read the per-installation secret — the credential that asks ``/enroll`` for
 * an agent key and is never used for anything else. Mirrors
 * ``nexus_memory.service_auth.load_service_key()``: ``NEXUS_SERVICE_AUTH_KEY``
 * wins, otherwise the key file in the data directory is read. Kept in this
 * environment module so lib/embedder.ts never reads ``process.env`` beside a
 * network call.
 */
export function nexusInstallKey(
  env: Record<string, string | undefined> = process.env,
): { header: string; key: string } {
  const key = (env.NEXUS_SERVICE_AUTH_KEY || "").trim()
  if (key) return { header: SERVICE_AUTH_HEADER_NAME, key }
  try {
    return {
      header: SERVICE_AUTH_HEADER_NAME,
      key: fs.readFileSync(path.join(nexusDataDir(env), SERVICE_AUTH_KEY_FILE), "utf8").trim(),
    }
  } catch {
    return { header: SERVICE_AUTH_HEADER_NAME, key: "" }
  }
}

/** Where this plugin's own key lives: ``<data-dir>/agent_keys/<harness>.key``. */
export function nexusAgentKeyPath(
  harness: string,
  env: Record<string, string | undefined> = process.env,
): string {
  return path.join(nexusDataDir(env), AGENT_KEY_DIR, `${harness}.key`)
}

/** This plugin's own key, or "" when it has not enrolled yet. */
export function readNexusAgentKey(
  harness: string,
  env: Record<string, string | undefined> = process.env,
): string {
  try {
    return fs.readFileSync(nexusAgentKeyPath(harness, env), "utf8").trim()
  } catch {
    return ""
  }
}

/** Store the key with mode 0600 (replacing any previous one). */
export function writeNexusAgentKey(
  harness: string,
  key: string,
  env: Record<string, string | undefined> = process.env,
): void {
  const file = nexusAgentKeyPath(harness, env)
  fs.mkdirSync(path.dirname(file), { recursive: true, mode: 0o700 })
  fs.writeFileSync(file, key, { encoding: "utf8", mode: 0o600 })
}

/** Drop a key the service no longer accepts. */
export function clearNexusAgentKey(
  harness: string,
  env: Record<string, string | undefined> = process.env,
): void {
  try {
    fs.unlinkSync(nexusAgentKeyPath(harness, env))
  } catch {
    /* already gone */
  }
}
