/**
 * Nexus Memory — Scope Auto-Inference (TypeScript port of scope_auto.py).
 *
 * Self-organizing memory areas (Nebo law 07.09: full automation or useless).
 * No user ever types a scope: centroids are computed from scoped canonical
 * points; a new memory inherits its area automatically on a CLEAR closest
 * match; a query that clearly belongs to one area unlocks that area.
 *
 * Conservative margins — under-tagging is harmless, over-tagging is what
 * we must avoid. Zero LLM cost: pure vector math. Fail-open everywhere:
 * any error or empty state = no areas = old behavior.
 */

import { fetchWithTimeout } from "./embedder.ts";

const CENTROID_TTL_MS = 60_000;
const MARGIN = 0.05;          // must beat runner-up by this cosine margin
const MIN_SIMILARITY = 0.72;  // absolute floor for the closest centroid

// Hard cap on scroll pages for the centroid fetch (mirrors MAX_SCROLL_PAGES in
// qdrant-client.ts). Prevents an "any-size collection" scan from unbounded
// fan-out while still sampling well beyond the first 1000 canonical points.
const MAX_SCROLL_PAGES = 5;

export type Centroids = Record<string, number[]>;

function dot(a: number[], b: number[]): number {
  if (a.length !== b.length) throw new Error("vector length mismatch");
  let s = 0;
  for (let i = 0; i < a.length; i++) s += a[i] * b[i];
  return s;
}

/**
 * L2-normalize into a NEW array (the input is left untouched); returns null
 * for a (near-)zero vector. Dimension mismatches are NOT handled here —
 * callers skip those before calling.
 */
function normalize(v: number[]): number[] | null {
  let mag = 0;
  for (const x of v) mag += x * x;
  mag = Math.sqrt(mag);
  if (mag < 1e-12) return null;
  return v.map((x) => x / mag);
}

/**
 * Fetch scoped canonical points and compute one centroid per scope.
 * Odd dimensions and near-zero vectors are skipped safely.
 */
export async function fetchCentroids(
  qdrantUrl: string,
  collection: string,
): Promise<Centroids> {
  // Paginated inline scroll (own fetch, NOT QdrantClient.scrollFiltered):
  // the centroid computation needs `with_vectors: true`, which the shared
  // helper does not request. Follows next_page_offset up to MAX_SCROLL_PAGES.
  const allPoints: Array<{
    vector?: number[] | null
    payload?: Record<string, unknown>
  }> = []
  let offset: unknown = undefined
  for (let page = 0; page < MAX_SCROLL_PAGES; page++) {
    const body: Record<string, unknown> = {
      filter: { must: [{ key: "lifecycle_status", match: { value: "canonical" } }] },
      with_payload: true,
      with_vectors: true,
      limit: 1000,
    }
    if (offset !== undefined && offset !== null) body.offset = offset
    const resp = await fetchWithTimeout(
      `${qdrantUrl}/collections/${collection}/points/scroll`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      },
    )
    if (!resp.ok) {
      throw new Error(`qdrant scroll failed: ${resp.status}`)
    }
    const data = (await resp.json()) as {
      result?: {
        points?: Array<{
          vector?: number[] | null
          payload?: Record<string, unknown>
        }>
        next_page_offset?: unknown
      }
    }
    for (const p of data.result?.points ?? []) allPoints.push(p)
    const next = data.result?.next_page_offset
    if (next === null || next === undefined) break
    offset = next
  }

  const sums: Record<string, { sum: number[]; n: number }> = {}
  let dim: number | null = null
  for (const p of allPoints) {
    const pl = p.payload ?? {}
    const scope = String(pl.scope ?? "default").trim().toLowerCase() || "default"
    if (scope === "default" || !Array.isArray(p.vector)) continue
    if (dim === null) dim = p.vector.length
    if (p.vector.length !== dim) continue // dimension mismatch → skip
    const norm = normalize(p.vector)
    if (!norm) continue
    const e = (sums[scope] ??= { sum: new Array(dim).fill(0), n: 0 })
    for (let i = 0; i < dim; i++) e.sum[i] += norm[i]
    e.n++
  }

  const cents: Centroids = {}
  for (const [scope, e] of Object.entries(sums)) {
    if (e.n === 0) continue
    const norm = normalize(e.sum)
    if (norm) cents[scope] = norm
  }
  return cents
}

/** Cached centroids with TTL; failures fail-open to {} (no areas). */
export class ScopeCentroidCache {
  private cache: Centroids = {}
  private at = 0
  private inflight: Promise<Centroids> | null = null
  // Generation counter: invalidate() bumps it so an already-running fetch
  // discards its (now stale) result instead of resurrecting the old cache.
  private gen = 0

  private qdrantUrl: string
  private collection: string

  constructor(qdrantUrl: string, collection: string) {
    this.qdrantUrl = qdrantUrl
    this.collection = collection
  }

  async get(): Promise<Centroids> {
    const now = Date.now()
    if (now - this.at < CENTROID_TTL_MS) return this.cache
    if (this.inflight) return this.inflight
    const gen = this.gen
    this.inflight = fetchCentroids(this.qdrantUrl, this.collection)
      .then((c) => {
        if (gen !== this.gen) return this.cache // invalidated mid-flight
        this.cache = c
        this.at = Date.now()
        return c
      })
      .catch((err) => {
        console.warn("scope_auto: centroid fetch failed — fail-open", err)
        if (gen === this.gen) this.at = Date.now() // don't hammer a down Qdrant
        return this.cache
      })
      .finally(() => {
        if (gen === this.gen) this.inflight = null
      })
    return this.inflight
  }

  invalidate(): void {
    this.at = 0
    this.gen++
    this.inflight = null
  }
}

/**
 * Closest centroid by cosine similarity.
 *
 * Centroids produced by `fetchCentroids` are ALREADY L2-normalized (each
 * source vector and then the summed centroid are normalized there), so the
 * dot product below IS the cosine similarity — no per-centroid re-normalization.
 */
function closestScope(
  vector: number[],
  cents: Centroids,
): { scope: string; margin: number; sim: number } | null {
  const norm = normalize(vector)
  if (!norm) return null
  let best: string | null = null
  let bestSim = -Infinity
  let second = -Infinity
  for (const [scope, c] of Object.entries(cents)) {
    const sim = dot(norm, c)
    if (sim > bestSim) {
      second = bestSim
      best = scope
      bestSim = sim
    } else if (sim > second) {
      second = sim
    }
  }
  if (!best) return null
  return { scope: best, margin: bestSim - second, sim: bestSim }
}

/** inferScope for WRITE path: clear closest match → scope; else 'default'. */
export function inferScope(vector: number[], cents: Centroids): string {
  const c = closestScope(vector, cents)
  if (!c) return "default"
  if (c.sim >= MIN_SIMILARITY && c.margin >= MARGIN) return c.scope
  return "default"
}

/**
 * prefetchFilterScopes for READ path: the query itself steers.
 * Returns null = no gating (old behavior); otherwise the allowed set
 * (always includes 'default' plus the clearly matched area).
 */
export function prefetchFilterScopes(
  vector: number[],
  cents: Centroids,
  manualScope: string,
): Set<string> | null {
  if (Object.keys(cents).length === 0) return null
  const c = closestScope(vector, cents)
  if (!c) return null
  if (c.sim >= MIN_SIMILARITY && c.margin >= MARGIN) {
    // Self-organizing: the query steers. A manual scope is ADDED to the
    // inferred area (not a replacement) — the agent keeps its home base
    // while still seeing the area the question belongs to. Parity with
    // Python prefetch_allowed_scopes().
    const allowed = new Set(["default", c.scope])
    if (manualScope) allowed.add(manualScope)
    return allowed
  }
  // No clear match → manual scope alone still gates (old behavior);
  // no manual scope → no gating at all (fail-open).
  if (manualScope) return new Set(["default", manualScope])
  return null // ambiguous → no gating
}

/**
 * Normalize a scope for the write path: trim, lowercase, then accept only
 * `[a-z0-9][a-z0-9-]{0,39}` (starts alphanumeric, at most 40 chars). Any
 * absent, empty or malformed input becomes "default" — fail-open, so an
 * invalid scope never blocks a write. Returns a NEW string; the input is
 * never modified.
 */
export function normalizeScope(scope: unknown): string {
  if (typeof scope === "string") {
    const s = scope.trim().toLowerCase()
    if (s && /^[a-z0-9][a-z0-9-]{0,39}$/.test(s)) return s
  }
  return "default"
}

/**
 * Config-scope validator: the same [a-z0-9-] contract as normalizeScope, but
 * FAIL-OPEN to "" for absent/invalid input. Callers use "" to mean "not
 * configured" (no gating) — distinct from normalizeScope's "default". This is
 * the single implementation shared by config.ts (cfg.scope + NEXUS_SCOPE).
 */
export function validateConfigScope(scope: unknown): string {
  if (typeof scope === "string") {
    const s = scope.trim().toLowerCase()
    if (s && /^[a-z0-9][a-z0-9-]{0,39}$/.test(s)) return s
  }
  return ""
}