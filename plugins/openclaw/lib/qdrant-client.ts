import { log } from "../logger.ts"
import { fetchWithTimeout } from "./embedder.ts"

export type SearchResult = {
  id: string
  text: string
  score: number
  access_level: string
  category: string
  source: string
  created_at: string
  /** Project/agent area label (unreleased). Missing → 'default'. */
  scope?: string
}

/**
 * Hard cap on how many scroll pages a single query walks via
 * `next_page_offset`. Bounds the worst case (latency/fan-out) on large
 * collections while still returning far more than the first page.
 */
const MAX_SCROLL_PAGES = 5

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
  // Fail-closed: an unknown level must NOT degrade to public (0), which made
  // every memory visible. An unrecognized level sees nothing at all.
  const agentOrder = ACCESS_LEVEL_ORDER[level]
  if (agentOrder === undefined) {
    log.debug(`visibleAccessLevels: unknown access level "${level}" — fail-closed (nothing visible)`)
    return []
  }
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
  private collectionReady: boolean = false

  constructor(qdrantUrl: string, collection: string, dimensions: number) {
    this.qdrantUrl = qdrantUrl.replace(/\/+$/, "")
    this.collection = collection
    log.info(`Qdrant client initialized (url=${this.qdrantUrl}, collection=${collection}, dims=${dimensions})`)
  }

  /**
   * Ensures the Qdrant collection exists with the correct vector dimensions.
   *
   * On a dimension mismatch this NEVER auto-deletes the collection (the
   * 18.06.2026 Qdrant wipe incident): deleting drops every stored point.
   * Instead it throws, so a human can back up and recreate deliberately.
   *
   * @param dimensions   Expected vector size.
   * @param allowRecreate Only when explicitly `true` AND `backupPath` is
   *   provided may an existing mismatch be deleted and recreated.
   * @param backupPath   Path to a verified backup — required for recreate.
   */
  async ensureCollection(
    dimensions: number,
    allowRecreate: boolean = false,
    backupPath?: string,
  ): Promise<void> {
    const url = `${this.qdrantUrl}/collections/${this.collection}`

    // Check if collection exists
    let exists = false
    let currentDim: number | undefined
    let resp: Response
    try {
      resp = await fetch(url, { method: "GET" })
    } catch (err) {
      // Network-level failure (DNS, refused, timeout) is NOT proof that the
      // collection is missing — surface it instead of silently creating.
      throw new Error(
        `Qdrant connection failed while checking collection "${this.collection}" ` +
        `(url=${this.qdrantUrl}): ${err instanceof Error ? err.message : String(err)}`,
      )
    }

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
    } else if (resp.status === 404) {
      // The ONLY status that legitimately means "collection absent".
      exists = false
      currentDim = undefined
    } else {
      // Any other error status (401/403/500/…) is NOT an absent collection.
      // Falling through to "create" here would either fail confusingly or
      // mask a misconfigured connection/permission as a missing collection.
      throw new Error(
        `Qdrant collection check failed: ${resp.status} ` +
        `(collection="${this.collection}", url=${this.qdrantUrl}). ` +
        `Check the Qdrant connection and API permissions.`,
      )
    }

    if (exists && currentDim === dimensions) {
      log.debug(`collection ${this.collection} exists with correct dimensions (${dimensions})`)
      this.collectionReady = true
      return
    }

    if (exists && currentDim !== undefined && currentDim !== dimensions) {
      const mismatch =
        `Qdrant collection "${this.collection}" has dimensions=${currentDim}, expected=${dimensions}.`
      if (allowRecreate && backupPath) {
        // Explicit opt-in + verified backup path: recreate is permitted.
        log.warn(
          `${mismatch} — recreating (allowRecreate=true, backup=${backupPath}). ` +
          `All existing points are deleted!`,
        )
        // W31-17: verify the DELETE. The response used to be ignored and the
        // request had no timeout — a 401/403/500 silently left the old
        // collection in place, so the recreate-branch below either failed
        // confusingly or upserted into a wrong-dimension collection. Refuse
        // to continue on any non-2xx, and bound the request with a timeout.
        const deleteResp = await fetch(url, {
          method: "DELETE",
          signal: AbortSignal.timeout(10000),
        })
        if (!deleteResp.ok) {
          throw new Error(
            `DELETE failed HTTP ${deleteResp.status} — refusing recreate ` +
              `(collection="${this.collection}", url=${this.qdrantUrl})`,
          )
        }
        log.info(`collection ${this.collection} deleted (allowRecreate opt-in)`)
        exists = false
      } else {
        // Default: never delete. Refuse and point at the manual procedure.
        throw new Error(
          `${mismatch} Refusing to auto-delete the collection (data-loss risk). ` +
          `Back up the collection and recreate it manually, or call ` +
          `ensureCollection(${dimensions}, allowRecreate=true, backupPath="/path/to/backup") ` +
          `once a verified backup exists.`,
        )
      }
    }

    // Collection exists but its dimensions could not be read: accepting it
    // silently would let every later upsert/search fail deep inside Qdrant.
    // Refuse instead and ask for a manual check.
    if (exists && currentDim === undefined) {
      const msg =
        `Qdrant collection "${this.collection}" exists but its dimensions are not readable. ` +
        `Manual inspection is required (expected=${dimensions}); ` +
        `refusing to assume the collection is correct.`
      log.error(msg)
      throw new Error(msg)
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
   * Number of points currently stored in the collection, or null when it
   * cannot be determined (Qdrant down, non-2xx, malformed body).
   *
   * GET /collections/{collection} → result.points_count
   *
   * Never throws — callers use it as a liveness/count probe.
   */
  async countPoints(): Promise<number | null> {
    try {
      const resp = await fetch(`${this.qdrantUrl}/collections/${this.collection}`, {
        method: "GET",
      })
      if (!resp.ok) return null
      const data = await resp.json() as { result?: { points_count?: number } }
      const count = data.result?.points_count
      return typeof count === "number" && Number.isFinite(count) ? count : null
    } catch {
      return null
    }
  }

  /**
   * Search for similar vectors with access-level filtering.
   *
   * POST /collections/{collection}/points/search
   * { vector, limit, with_payload: true, filter: { must: [{ key: "access_level", match: { any: [...] } }] } }
   */
  async search(queryVector: number[], limit: number, accessLevel: string): Promise<SearchResult[]> {
    const levels = visibleAccessLevels(accessLevel)

    // W31-16: unknown level → levels=[] → fail-closed with NO Qdrant call.
    // Sending `match: { any: [] }` relied on the unverified Qdrant semantics
    // that an empty `any` matches nothing; if Qdrant ignored the clause an
    // unknown level would see every memory. Return empty instead.
    if (levels.length === 0) {
      log.debug(
        `search: no visible access levels for "${accessLevel}" — fail-closed (empty result)`,
      )
      return []
    }

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

    const resp = await fetchWithTimeout(
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
      scope: (r.payload?.scope as string) ?? undefined,
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

    const resp = await fetchWithTimeout(
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
   * Fetch a single point by its native ID — returns the point with payload,
   * or null when it does not exist / cannot be fetched.
   *
   * H137: this used to POST /points/scroll with a `must: [{ key: "id" }]`
   * payload filter. Qdrant matches the filter against the PAYLOAD, but the
   * point ID lives outside the payload, so the filter never matched and this
   * method always returned null — every graph_traverse/get_related caller saw
   * each fact as "not found". The correct primitive is the point-retrieve
   * endpoint.
   *
   * GET /collections/{collection}/points/{id}?with_payload=true
   * → { result: { id, payload } } on success, 404 when the point is absent.
   */
  async scrollPoint(id: string): Promise<{ id: string; payload?: Record<string, unknown> } | null> {
    try {
      const resp = await fetchWithTimeout(
        `${this.qdrantUrl}/collections/${this.collection}/points/${encodeURIComponent(id)}?with_payload=true`,
        { method: "GET" },
      )
      if (!resp.ok) {
        // Fail-open (null) stays, but the reason must be visible in the log:
        // a 404 (absent point) and a 500/403 (Qdrant problem) both used to
        // look identical to callers.
        log.error(
          `scrollPoint: Qdrant returned ${resp.status} for collection="${this.collection}" id="${id}"`,
        )
        return null
      }
      const data = await resp.json() as {
        result?: { id?: string | number; payload?: Record<string, unknown> } | null
      }
      const point = data.result
      if (!point || point.id === undefined || point.id === null) return null
      return { id: String(point.id), payload: point.payload }
    } catch (err) {
      log.error(`scrollPoint: request failed for collection="${this.collection}" id="${id}"`, err)
      return null
    }
  }

  /**
   * Scroll points with a Qdrant filter — returns points with payload.
   *
   * PAGINATED: `limit` is the PER-PAGE limit. The call follows
   * `next_page_offset` for up to MAX_SCROLL_PAGES pages, so the returned
   * array can hold up to `limit * MAX_SCROLL_PAGES` points. This makes
   * "top N / all matching" queries (protection rules, entity edges) complete
   * over large collections instead of only ever seeing the first page.
   * Returns whatever was collected so far if a later page fails (fail-open
   * to partial data, same spirit as the previous empty-array fallback).
   */
  async scrollFiltered(
    filter: Record<string, unknown>,
    limit: number,
    maxTotal?: number,
  ): Promise<Array<{ id: string; payload?: Record<string, unknown> }>> {
    const collected: Array<{ id: string; payload?: Record<string, unknown> }> = []
    let offset: unknown = undefined
    try {
      for (let page = 0; page < MAX_SCROLL_PAGES; page++) {
        const body: Record<string, unknown> = {
          filter,
          limit,
          with_payload: true,
          with_vector: false,
        }
        if (offset !== undefined && offset !== null) body.offset = offset

        const resp = await fetchWithTimeout(
          `${this.qdrantUrl}/collections/${this.collection}/points/scroll`,
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body),
          },
        )
        if (!resp.ok) {
          log.error(
            `scrollFiltered: Qdrant returned ${resp.status} for collection="${this.collection}" (returning ${collected.length} points collected so far)`,
          )
          return collected
        }
        const data = await resp.json() as {
          result?: {
            points?: Array<{ id: string | number; payload?: Record<string, unknown> }>
            next_page_offset?: unknown
          }
        }
        for (const p of data.result?.points ?? []) {
          collected.push({ id: String(p.id), payload: p.payload })
        }
        // W31-18(b): honor a TOTAL cap — `limit` is per-page, so without this
        // the 5-page walk returned up to limit*MAX_SCROLL_PAGES points and a
        // caller's `limit` was silently exceeded.
        if (maxTotal !== undefined && collected.length >= maxTotal) break
        const next = data.result?.next_page_offset
        if (next === null || next === undefined) break
        offset = next
      }
      return maxTotal !== undefined ? collected.slice(0, maxTotal) : collected
    } catch (err) {
      log.error(
        `scrollFiltered: request failed for collection="${this.collection}" (returning ${collected.length} points collected so far)`,
        err,
      )
      return collected
    }
  }

  /**
   * Find incoming edges — points where target_fact_id == factId in their edges payload.
   * Returns array of { source_id, relation, edge_id }.
   */
  async findIncomingEdges(
    factId: string,
    relation?: string,
  ): Promise<Array<{ source_id: string; relation: string; edge_id: string }>> {
    try {
      // Qdrant doesn't support nested object filtering well, so we scroll
      // entity-typed points and check their edges array in-memory. Uses the
      // paginated scrollFiltered (per-page limit 250, up to MAX_SCROLL_PAGES
      // pages) so edges past the first page are no longer missed.
      const points = await this.scrollFiltered(
        { must: [{ key: "category", match: { value: "entity" } }] },
        250,
      )
      const incoming: Array<{ source_id: string; relation: string; edge_id: string }> = []

      for (const pt of points) {
        const edges = (pt.payload?.edges ?? []) as Array<Record<string, unknown>>
        for (const edge of edges) {
          if (edge.target_fact_id === factId) {
            const edgeStatus = edge.status as string
            if (edgeStatus && edgeStatus !== "active") continue
            const edgeRelation = edge.relation as string
            if (relation && edgeRelation !== relation) continue
            incoming.push({
              source_id: String(pt.id),
              relation: edgeRelation,
              edge_id: String(edge.edge_id ?? ""),
            })
          }
        }
      }
      return incoming
    } catch (err) {
      log.error(
        `findIncomingEdges: request failed for collection="${this.collection}" factId="${factId}"`,
        err,
      )
      return []
    }
  }

  /**
   * Vector search used by forget-by-query.
   *
   * Applies the SAME access-level filter as search(): a memory an agent at
   * `accessLevel` cannot see must never be matched (and then deleted) via
   * forget-by-query. Unknown levels fail closed (match nothing).
   */
  async searchByVector(
    queryVector: number[],
    limit: number,
    accessLevel: string,
  ): Promise<SearchResult[]> {
    const levels = visibleAccessLevels(accessLevel)

    // W31-16: same fail-closed early return as search() — never send an empty
    // `match: { any: [] }` and never match a point for an unknown level.
    if (levels.length === 0) {
      log.debug(
        `searchByVector: no visible access levels for "${accessLevel}" — fail-closed (empty result)`,
      )
      return []
    }

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

    const resp = await fetchWithTimeout(
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