import { log } from "../logger.ts"

export type SearchResult = {
  id: string
  text: string
  score: number
  access_level: string
  category: string
  source: string
  created_at: string
}

/** Access-level hierarchy: public=0, trusted=1, private=2. */
const ACCESS_LEVEL_ORDER: Record<string, number> = {
  public: 0,
  trusted: 1,
  private: 2,
}

/**
 * Returns the list of access levels that are visible to an agent with the
 * given access level. An agent can see memories at its own level or below.
 */
function visibleAccessLevels(level: string): string[] {
  const agentOrder = ACCESS_LEVEL_ORDER[level] ?? 0
  const result: string[] = []
  for (const [key, value] of Object.entries(ACCESS_LEVEL_ORDER)) {
    if (value <= agentOrder) result.push(key)
  }
  return result
}

/**
 * Thin Qdrant REST API client.
 *
 * Uses fetch() directly — no native dependencies, no Python, no SDK.
 * All operations target a single Qdrant collection.
 */
export class QdrantClient {
  private qdrantUrl: string
  private collection: string
  private dimensions: number
  private collectionReady: boolean = false

  constructor(qdrantUrl: string, collection: string, dimensions: number) {
    this.qdrantUrl = qdrantUrl.replace(/\/+$/, "")
    this.collection = collection
    this.dimensions = dimensions
    log.info(`Qdrant client initialized (url=${this.qdrantUrl}, collection=${collection}, dims=${dimensions})`)
  }

  /** Ensures the Qdrant collection exists with the correct vector dimensions. */
  async ensureCollection(dimensions: number): Promise<void> {
    const url = `${this.qdrantUrl}/collections/${this.collection}`

    // Check if collection exists
    let exists = false
    let currentDim: number | undefined
    try {
      const resp = await fetch(url, { method: "GET" })
      if (resp.ok) {
        const data = await resp.json() as {
          result?: {
            config?: { params?: { vectors?: { size?: number } } }
            vectors?: { size?: number }
          }
        }
        exists = true
        // Qdrant returns dimensions at result.config.params.vectors.size
        currentDim = data.result?.config?.params?.vectors?.size ?? data.result?.vectors?.size
      }
    } catch {
      // Collection doesn't exist or Qdrant is down — try to create
    }

    if (exists && currentDim === dimensions) {
      log.debug(`collection ${this.collection} exists with correct dimensions (${dimensions})`)
      this.collectionReady = true
      return
    }

    if (exists && currentDim !== undefined && currentDim !== dimensions) {
      log.warn(`collection ${this.collection} has dimensions=${currentDim}, expected ${dimensions} — recreating`)
      await fetch(url, { method: "DELETE" })
      exists = false
    }

    // If collection exists with unknown dimensions, assume it's fine
    if (exists) {
      log.debug(`collection ${this.collection} exists (dimensions unknown, assuming correct)`)
      this.collectionReady = true
      return
    }

    // Create collection — Qdrant uses PUT /collections/{name}
    log.debug(`creating collection ${this.collection} (dimensions=${dimensions}, distance=Cosine)`)
    const createResp = await fetch(`${this.qdrantUrl}/collections/${this.collection}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        vectors: { size: dimensions, distance: "Cosine" },
      }),
    })

    if (!createResp.ok) {
      const body = await createResp.text()
      throw new Error(`Failed to create Qdrant collection: ${createResp.status} ${body}`)
    }

    log.info(`collection ${this.collection} created (dimensions=${dimensions})`)
    this.collectionReady = true
  }

  /**
   * Search for similar vectors with access-level filtering.
   *
   * POST /collections/{collection}/points/search
   * { vector, limit, with_payload: true, filter: { must: [{ key: "access_level", match: { any: [...] } }] } }
   */
  async search(queryVector: number[], limit: number, accessLevel: string): Promise<SearchResult[]> {
    const levels = visibleAccessLevels(accessLevel)

    const filter =
      levels.length < 3
        ? { must: [{ key: "access_level", match: { any: levels } }] }
        : undefined // private sees everything — no filter needed

    const body: Record<string, unknown> = {
      vector: queryVector,
      limit,
      with_payload: true,
    }
    if (filter) body.filter = filter

    log.debugRequest("search", { collection: this.collection, limit, accessLevel, levels })

    const resp = await fetch(
      `${this.qdrantUrl}/collections/${this.collection}/points/search`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      },
    )

    if (!resp.ok) {
      const text = await resp.text()
      throw new Error(`Qdrant search failed: ${resp.status} ${text}`)
    }

    const data = await resp.json() as {
      result?: Array<{
        id: string | number
        score: number
        payload?: Record<string, unknown>
      }>
    }

    const results: SearchResult[] = (data.result ?? []).map((r) => ({
      id: String(r.id),
      text: (r.payload?.text as string) ?? "",
      score: r.score,
      access_level: (r.payload?.access_level as string) ?? "public",
      category: (r.payload?.category as string) ?? "fact",
      source: (r.payload?.source as string) ?? "conversation",
      created_at: (r.payload?.created_at as string) ?? "",
    }))

    log.debugResponse("search", { count: results.length })
    return results
  }

  /**
   * Upsert a point with vector + payload.
   *
   * PUT /collections/{collection}/points
   * { points: [{ id, vector, payload }] }
   */
  async upsert(id: string, vector: number[], payload: Record<string, unknown>): Promise<void> {
    log.debugRequest("upsert", { id, payloadKeys: Object.keys(payload), vectorDim: vector.length })

    const resp = await fetch(
      `${this.qdrantUrl}/collections/${this.collection}/points`,
      {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          points: [{ id, vector, payload }],
        }),
      },
    )

    if (!resp.ok) {
      const text = await resp.text()
      throw new Error(`Qdrant upsert failed: ${resp.status} ${text}`)
    }

    log.debugResponse("upsert", { id })
  }

  /**
   * Delete a point by ID.
   *
   * POST /collections/{collection}/points/delete
   * { points: [id] }
   */
  async delete(id: string): Promise<void> {
    log.debugRequest("delete", { id })

    const resp = await fetch(
      `${this.qdrantUrl}/collections/${this.collection}/points/delete`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ points: [id] }),
      },
    )

    if (!resp.ok) {
      const text = await resp.text()
      throw new Error(`Qdrant delete failed: ${resp.status} ${text}`)
    }

    log.debugResponse("delete", { id })
  }

  /**
   * Search by query text (convenience — embeds then searches).
   * Used by forget-by-query: searches and returns the first result.
   */
  async searchByVector(queryVector: number[], limit: number): Promise<SearchResult[]> {
    // For internal use (forget-by-query) — no access-level filtering
    const body = {
      vector: queryVector,
      limit,
      with_payload: true,
    }

    const resp = await fetch(
      `${this.qdrantUrl}/collections/${this.collection}/points/search`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      },
    )

    if (!resp.ok) {
      const text = await resp.text()
      throw new Error(`Qdrant search failed: ${resp.status} ${text}`)
    }

    const data = await resp.json() as {
      result?: Array<{
        id: string | number
        score: number
        payload?: Record<string, unknown>
      }>
    }

    return (data.result ?? []).map((r) => ({
      id: String(r.id),
      text: (r.payload?.text as string) ?? "",
      score: r.score,
      access_level: (r.payload?.access_level as string) ?? "public",
      category: (r.payload?.category as string) ?? "fact",
      source: (r.payload?.source as string) ?? "conversation",
      created_at: (r.payload?.created_at as string) ?? "",
    }))
  }
}