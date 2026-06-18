import { log } from "../logger.ts"

export type EmbeddingProvider = "voyage" | "openai" | "ollama" | "google" | "jina"

/** Default models and dimensions per provider. */
const PROVIDER_DEFAULTS: Record<EmbeddingProvider, { model: string; dimensions: number; baseUrl?: string }> = {
  voyage: { model: "voyage-3-large", dimensions: 1024 },
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
  // Ollama needs no key — check if baseUrl is reachable is too expensive here,
  // just assume it's available if no other provider is configured.
  if (process.env.OLLAMA_HOST || process.env.OLLAMA_BASE_URL) return "ollama"
  return null
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

    const resp = await fetch("https://api.voyageai.com/v1/embeddings", {
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

    if (!resp.ok) {
      const body = await resp.text()
      throw new Error(`Voyage embedding failed: ${resp.status} ${body}`)
    }

    const data = await resp.json() as { data?: Array<{ embedding?: number[] }> }
    const vector = data.data?.[0]?.embedding
    if (!vector) throw new Error("Voyage returned no embedding")

    log.debugResponse("embed.voyage", { dims: vector.length })
    return vector
  }

  private async embedOpenAI(text: string): Promise<number[]> {
    log.debugRequest("embed.openai", { model: this.model, textLen: text.length })

    const resp = await fetch("https://api.openai.com/v1/embeddings", {
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

    if (!resp.ok) {
      const body = await resp.text()
      throw new Error(`OpenAI embedding failed: ${resp.status} ${body}`)
    }

    const data = await resp.json() as { data?: Array<{ embedding?: number[] }> }
    const vector = data.data?.[0]?.embedding
    if (!vector) throw new Error("OpenAI returned no embedding")

    log.debugResponse("embed.openai", { dims: vector.length })
    return vector
  }

  private async embedOllama(text: string): Promise<number[]> {
    const base = this.baseUrl ?? "http://localhost:11434"
    log.debugRequest("embed.ollama", { model: this.model, textLen: text.length, baseUrl: base })

    const resp = await fetch(`${base}/api/embed`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        input: text,
        model: this.model,
      }),
    })

    if (!resp.ok) {
      const body = await resp.text()
      throw new Error(`Ollama embedding failed: ${resp.status} ${body}`)
    }

    const data = await resp.json() as { embeddings?: number[][] }
    const vector = data.embeddings?.[0]
    if (!vector) throw new Error("Ollama returned no embedding")

    log.debugResponse("embed.ollama", { dims: vector.length })
    return vector
  }

  private async embedGoogle(text: string): Promise<number[]> {
    log.debugRequest("embed.google", { model: this.model, textLen: text.length })

    const url = `https://generativelanguage.googleapis.com/v1beta/models/${this.model}:embedContent`

    const resp = await fetch(url, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        content: { parts: [{ text }] },
        taskType: "RETRIEVAL_DOCUMENT",
      }),
      // Google uses query param for key
    })

    // If the key-as-header approach doesn't work, retry with key in URL
    if (!resp.ok && resp.status === 400) {
      const retryUrl = `${url}?key=${this.apiKey}`
      const retryResp = await fetch(retryUrl, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          content: { parts: [{ text }] },
          taskType: "RETRIEVAL_DOCUMENT",
        }),
      })
      if (retryResp.ok) {
        const retryData = await retryResp.json() as { embedding?: { values?: number[] } }
        const vector = retryData.embedding?.values
        if (vector) {
          log.debugResponse("embed.google", { dims: vector.length })
          return vector
        }
      }
    }

    if (!resp.ok) {
      const body = await resp.text()
      throw new Error(`Google embedding failed: ${resp.status} ${body}`)
    }

    const data = await resp.json() as { embedding?: { values?: number[] } }
    const vector = data.embedding?.values
    if (!vector) throw new Error("Google returned no embedding")

    log.debugResponse("embed.google", { dims: vector.length })
    return vector
  }

  private async embedJina(text: string): Promise<number[]> {
    log.debugRequest("embed.jina", { model: this.model, textLen: text.length })

    const resp = await fetch("https://api.jina.ai/v1/embeddings", {
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

    if (!resp.ok) {
      const body = await resp.text()
      throw new Error(`Jina embedding failed: ${resp.status} ${body}`)
    }

    const data = await resp.json() as { data?: Array<{ embedding?: number[] }> }
    const vector = data.data?.[0]?.embedding
    if (!vector) throw new Error("Jina returned no embedding")

    log.debugResponse("embed.jina", { dims: vector.length })
    return vector
  }
}