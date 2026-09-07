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

const CENTROID_TTL_MS = 60_000;
const MARGIN = 0.05;          // must beat runner-up by this cosine margin
const MIN_SIMILARITY = 0.72;  // absolute floor for the closest centroid

export type Centroids = Record<string, number[]>;

function dot(a: number[], b: number[]): number {
  let s = 0;
  for (let i = 0; i < a.length; i++) s += a[i] * b[i];
  return s;
}

/** In-place L2-normalize; returns null for zero/odd-dim vectors. */
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
  const body = {
    filter: { must: [{ key: "lifecycle_status", match: { value: "canonical" } }] },
    with_payload: true,
    with_vectors: true,
    limit: 1000,
  }
  const resp = await fetch(
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
    }
  }

  const sums: Record<string, { sum: number[]; n: number }> = {}
  let dim: number | null = null
  for (const p of data.result?.points ?? []) {
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
    this.inflight = fetchCentroids(this.qdrantUrl, this.collection)
      .then((c) => {
        this.cache = c
        this.at = Date.now()
        return c
      })
      .catch((err) => {
        console.warn("scope_auto: centroid fetch failed — fail-open", err)
        this.at = Date.now() // don't hammer a down Qdrant every call
        return this.cache
      })
      .finally(() => {
        this.inflight = null
      })
    return this.inflight
  }

  invalidate(): void {
    this.at = 0
  }
}

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
    const cn = normalize(c)
    if (!cn) continue
    const sim = dot(norm, cn)
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

/** Same normalization contract as the server: [a-z0-9-], max 40, fail-open. */
export function normalizeScope(scope: unknown): string {
  if (typeof scope === "string") {
    const s = scope.trim().toLowerCase()
    if (s && /^[a-z0-9][a-z0-9-]{0,39}$/.test(s)) return s
  }
  return "default"
}