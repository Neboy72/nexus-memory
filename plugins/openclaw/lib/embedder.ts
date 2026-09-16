import { log } from "../logger.ts"

export type EmbeddingProvider = "voyage" | "openai" | "ollama" | "google" | "jina"

/** Default models and dimensions per provider. */
const PROVIDER_DEFAULTS: Record<EmbeddingProvider, { model: string; dimensions: number; baseUrl?: string }> = {
  voyage: { model: "voyage-4", dimensions: 1024 },
  openai: { model: "text-embedding-3-small", dimensions: 1536 },
  ollama: { model: "nomic-embed-text", dimensions: 768, baseUrl: "http://localhost:11434" },
  google: { model: "text-embedding-004", dimensions: 768 },
  jina: { model: "jina-embeddings-v3", dimensions: 1024 },
}

/** Env var names for each provider's API key. */
const PROVIDER_ENV_KEYS: Record<EmbeddingProvider, string> = {
  voyage: "VOYAGE_API_KEY",
  openai: "OPENAI_API_KEY",
  ollama: "",
  google: "GOOGLE_API_KEY",
  jina: "JINA_API_KEY",
}

/** Auto-detect a provider from environment variables. Priority order. */
export function detectProvider(): EmbeddingProvider | null {
  if (process.env.VOYAGE_API_KEY) return "voyage"
  if (process.env.OPENAI_API_KEY) return "openai"
  if (process.env.GOOGLE_API_KEY) return "google"
  if (process.env.JINA_API_KEY) return "jina"
  // Ollama needs no key. Reachability is NOT probed here (too expensive in
  // detectProvider): if no other provider is configured we assume Ollama is
  // intended. An unreachable Ollama is only surfaced later, when embed() calls
  // it — the constructor does NOT fail on it (verified: it only resolves
  // provider/model/baseUrl and logs).
  if (process.env.OLLAMA_HOST || process.env.OLLAMA_BASE_URL) return "ollama"
  return null
}

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
    // Resolve provider: explicit config > env auto-detect > throw
    if (provider) {
      this.provider = provider
    } else {
      const detected = detectProvider()
      if (!detected) {
        throw new Error(
          "No embedding provider configured. Set VOYAGE_API_KEY, OPENAI_API_KEY, GOOGLE_API_KEY, JINA_API_KEY, or configure Ollama.",
        )
      }
      this.provider = detected
    }

    const defaults = PROVIDER_DEFAULTS[this.provider]

    this.model = model ?? defaults.model
    this.dimensions = dimensions ?? defaults.dimensions

    // Resolve API key: explicit config > env var
    const envKey = PROVIDER_ENV_KEYS[this.provider]
    this.apiKey = apiKey ?? (envKey ? process.env[envKey] : undefined)

    // Resolve base URL: explicit config > provider default
    this.baseUrl = baseUrl ?? defaults.baseUrl

    // Ollama needs no API key
    if (this.provider !== "ollama" && !this.apiKey) {
      throw new Error(
        `No API key for embedding provider "${this.provider}". Set ${envKey} or configure embedding.apiKey.`,
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