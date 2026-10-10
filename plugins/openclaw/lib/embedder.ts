import { log } from "../logger.ts"

export type { EmbeddingProvider } from "./embedding-env.ts"

/** Default models and dimensions per provider. */
const PROVIDER_DEFAULTS: Record<EmbeddingProvider, { model: string; dimensions: number; baseUrl?: string }> = {
  // The local Nexus service embeds with the engine's own model (local
  // HuggingFace by default), so this plugin needs nothing installed.
  nexus: { model: "nexus-engine", dimensions: 1024, baseUrl: "http://127.0.0.1:9122" },
  voyage: { model: "voyage-4", dimensions: 1024 },
  openai: { model: "text-embedding-3-small", dimensions: 1536 },
  ollama: { model: "qwen3-embedding:0.6b", dimensions: 1024, baseUrl: "http://localhost:11434" },
  google: { model: "text-embedding-004", dimensions: 768 },
  jina: { model: "jina-embeddings-v3", dimensions: 1024 },
}

/** Local first (developer default, 2026-10-07). Ollama needs no key;
 * reachability is NOT probed here and an unreachable Ollama does NOT fail the
 * constructor (it only resolves provider/model/baseUrl and logs) - it surfaces
 * later, when embed() calls it. The choice itself lives in lib/embedding-env.ts;
 * this module does not read the environment.
 */
import type { EmbeddingProvider } from "./embedding-env.ts"

/** Default time budget for a single HTTP call (provider or Qdrant). */
export const DEFAULT_FETCH_TIMEOUT_MS = 30_000

/** Combine two abort signals, using AbortSignal.any when available. */
function mergeSignals(a: AbortSignal, b: AbortSignal): AbortSignal {
  const anyFn = (AbortSignal as unknown as {
    any?: (signals: AbortSignal[]) => AbortSignal
  }).any
  if (typeof anyFn === "function") return anyFn([a, b])
  // Fallback for runtimes without AbortSignal.any: combine manually so BOTH
  // signals stay live. Returning only `b` silently dropped the caller's
  // signal (e.g. session/cache-level cancellation), leaving the request
  // uncancellable on those runtimes.
  const controller = new AbortController()
  const onAbort = () => controller.abort()
  if (a.aborted || b.aborted) {
    controller.abort()
    return controller.signal
  }
  a.addEventListener("abort", onAbort, { once: true })
  b.addEventListener("abort", onAbort, { once: true })
  // Detach the listeners once either side wins, so a long-lived signal does
  // not accumulate handlers across requests.
  controller.signal.addEventListener(
    "abort",
    () => {
      a.removeEventListener("abort", onAbort)
      b.removeEventListener("abort", onAbort)
    },
    { once: true },
  )
  return controller.signal
}

/**
 * fetch() with a hard timeout.
 *
 * AbortSignal.timeout(ms) is merged into init.signal so a hung provider or
 * Qdrant can never leave a request (or ScopeCentroidCache.inflight) pending
 * forever. On abort the error names the timeout so the cause is obvious.
 */
export async function fetchWithTimeout(
  url: string,
  init: RequestInit = {},
  ms: number = DEFAULT_FETCH_TIMEOUT_MS,
): Promise<Response> {
  const timeout = AbortSignal.timeout(ms)
  const signal = init.signal ? mergeSignals(init.signal, timeout) : timeout
  try {
    return await fetch(url, { ...init, signal })
  } catch (err) {
    if (timeout.aborted) {
      throw new Error(`${url} timed out after ${ms}ms`)
    }
    throw err
  }
}

/**
 * Shared response guard for the provider blocks: fail on HTTP errors with a
 * truncated body (never auth headers) and on non-JSON payloads.
 */
async function parseEmbeddingResponse(
  resp: Response,
  provider: string,
): Promise<Record<string, unknown>> {
  const text = await resp.text()
  if (!resp.ok) {
    throw new Error(
      `${provider} embedding failed: ${resp.status} ${text.slice(0, 200)}`,
    )
  }
  try {
    return JSON.parse(text) as Record<string, unknown>
  } catch {
    // text() may be empty when the body arrived via a custom/stub Response —
    // give the Response's own decoder a chance before declaring it non-JSON.
    try {
      return (await resp.json()) as Record<string, unknown>
    } catch {
      throw new Error(`${provider}: non-JSON response: ${text.slice(0, 200)}`)
    }
  }
}

/**
 * Embedding provider — thin HTTP client for Voyage/OpenAI/Ollama/Google/Jina.
 *
 * No SDK dependencies, just fetch(). Returns a vector (number[]).
 */
export class Embedder {
  private provider: EmbeddingProvider
  private model: string
  private apiKey: string | undefined
  private baseUrl: string | undefined
  private dimensions: number

  constructor(
    provider: EmbeddingProvider | undefined,
    model: string | undefined,
    apiKey: string | undefined,
    baseUrl: string | undefined,
    dimensions: number | undefined,
  ) {
    // Resolve provider. The normal path never reaches the guard: parseConfig
    // always resolves one (explicit config first, else localEmbeddingProvider)
    // and passes it in. The guard exists for direct construction of this class.
    // This module never reads the environment itself.
    if (!provider) {
      throw new Error(
        "No embedding provider configured. Set embedding.provider in the plugin config (see the README).",
      )
    }
    this.provider = provider

    const defaults = PROVIDER_DEFAULTS[this.provider]

    this.model = model ?? defaults.model
    this.dimensions = dimensions ?? defaults.dimensions

    // API key: the resolved value only. parseConfig reads the provider's
    // environment variable and passes the key in; no environment variable name
    // appears in this module.
    this.apiKey = apiKey

    // Resolve base URL: explicit config > provider default
    this.baseUrl = baseUrl ?? defaults.baseUrl

    // Ollama and the local Nexus service need no API key
    if (this.provider !== "ollama" && this.provider !== "nexus" && !this.apiKey) {
      throw new Error(
        `No API key for embedding provider "${this.provider}". Set its API key variable or configure embedding.apiKey.`,
      )
    }

    log.info(
      `Embedder initialized (provider=${this.provider}, model=${this.model}, dimensions=${this.dimensions}` +
        (this.baseUrl ? `, baseUrl=${this.baseUrl}` : "") + ")",
    )
  }

  getDimensions(): number {
    return this.dimensions
  }

  getProvider(): EmbeddingProvider {
    return this.provider
  }

  /** Guard: a provider must return exactly this.dimensions floats. */
  private validateVector(vector: unknown, provider: string): number[] {
    if (!Array.isArray(vector) || vector.length !== this.dimensions) {
      const actual = Array.isArray(vector) ? vector.length : "none"
      throw new Error(
        `${provider} embedding dimension mismatch: expected ${this.dimensions}, got ${actual}`,
      )
    }
    return vector as number[]
  }

  async embed(text: string): Promise<number[]> {
    switch (this.provider) {
      case "nexus":
        return this.embedNexus(text)
      case "voyage":
        return this.embedVoyage(text)
      case "openai":
        return this.embedOpenAI(text)
      case "ollama":
        return this.embedOllama(text)
      case "google":
        return this.embedGoogle(text)
      case "jina":
        return this.embedJina(text)
      default:
        throw new Error(`Unknown embedding provider: ${this.provider}`)
    }
  }

  /**
   * The local Nexus service (POST /embed). It embeds with the engine's
   * provider — local HuggingFace by default — so one service serves every
   * agent in the house, and no per-plugin provider list has to be kept.
   */
  private async embedNexus(text: string): Promise<number[]> {
    const base = this.baseUrl ?? "http://127.0.0.1:9122"
    log.debugRequest("embed.nexus", { textLen: text.length, baseUrl: base })

    const resp = await fetchWithTimeout(`${base}/embed`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text, is_query: true }),
    })

    const data = (await parseEmbeddingResponse(resp, "Nexus")) as {
      embedding?: number[]
      model?: string
    }
    const vector = this.validateVector(data.embedding, "Nexus")

    log.debugResponse("embed.nexus", { dims: vector.length, model: data.model })
    return vector
  }

  private async embedVoyage(text: string): Promise<number[]> {
    log.debugRequest("embed.voyage", { model: this.model, textLen: text.length })

    const resp = await fetchWithTimeout("https://api.voyageai.com/v1/embeddings", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${this.apiKey}`,
      },
      body: JSON.stringify({
        input: [text],
        model: this.model,
      }),
    })

    const data = await parseEmbeddingResponse(resp, "Voyage") as {
      data?: Array<{ embedding?: number[] }>
    }
    const vector = this.validateVector(data.data?.[0]?.embedding, "Voyage")

    log.debugResponse("embed.voyage", { dims: vector.length })
    return vector
  }

  private async embedOpenAI(text: string): Promise<number[]> {
    log.debugRequest("embed.openai", { model: this.model, textLen: text.length })

    const resp = await fetchWithTimeout("https://api.openai.com/v1/embeddings", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${this.apiKey}`,
      },
      body: JSON.stringify({
        input: text,
        model: this.model,
      }),
    })

    const data = await parseEmbeddingResponse(resp, "OpenAI") as {
      data?: Array<{ embedding?: number[] }>
    }
    const vector = this.validateVector(data.data?.[0]?.embedding, "OpenAI")

    log.debugResponse("embed.openai", { dims: vector.length })
    return vector
  }

  private async embedOllama(text: string): Promise<number[]> {
    const base = this.baseUrl ?? "http://localhost:11434"
    log.debugRequest("embed.ollama", { model: this.model, textLen: text.length, baseUrl: base })

    const resp = await fetchWithTimeout(`${base}/api/embed`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        input: text,
        model: this.model,
      }),
    })

    const data = await parseEmbeddingResponse(resp, "Ollama") as {
      embeddings?: number[][]
    }
    const vector = this.validateVector(data.embeddings?.[0], "Ollama")

    log.debugResponse("embed.ollama", { dims: vector.length })
    return vector
  }

  private async embedGoogle(text: string): Promise<number[]> {
    log.debugRequest("embed.google", { model: this.model, textLen: text.length })

    const url = `https://generativelanguage.googleapis.com/v1beta/models/${this.model}:embedContent`

    const resp = await fetchWithTimeout(url, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        // Google expects the key in this header. Without it the request could
        // never succeed; the old code omitted it and then retried only on 400,
        // which a missing/invalid key (401/403) never triggers — dead path.
        "x-goog-api-key": this.apiKey ?? "",
      },
      body: JSON.stringify({
        content: { parts: [{ text }] },
        taskType: "RETRIEVAL_DOCUMENT",
      }),
    })

    const data = await parseEmbeddingResponse(resp, "Google") as {
      embedding?: { values?: number[] }
    }
    const vector = this.validateVector(data.embedding?.values, "Google")

    log.debugResponse("embed.google", { dims: vector.length })
    return vector
  }

  private async embedJina(text: string): Promise<number[]> {
    log.debugRequest("embed.jina", { model: this.model, textLen: text.length })

    const resp = await fetchWithTimeout("https://api.jina.ai/v1/embeddings", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${this.apiKey}`,
      },
      body: JSON.stringify({
        input: [text],
        model: this.model,
      }),
    })

    const data = await parseEmbeddingResponse(resp, "Jina") as {
      data?: Array<{ embedding?: number[] }>
    }
    const vector = this.validateVector(data.data?.[0]?.embedding, "Jina")

    log.debugResponse("embed.jina", { dims: vector.length })
    return vector
  }
}