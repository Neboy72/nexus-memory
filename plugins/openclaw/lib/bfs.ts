/**
 * Shared BFS over graph edges (W25 Nr 493).
 *
 * graph_traverse.ts and get_subgraph held two near-identical BFS loops. This
 * is the single implementation; the two callers differ only in what they
 * collect:
 *   - nexus_graph_traverse → collectEdges=false → `steps` (fact_id/depth/relation/path)
 *   - nexus_get_subgraph   → collectEdges=true  → `nodes` + `edges`
 *
 * Behavior is deliberately kept identical to the previous inline loops:
 * same visited-set, same queue order (FIFO), same path semantics, and the
 * same edge-emission rule (traverse skips an already-visited target, subgraph
 * still records the edge). H123 depth clamping stays in the tools — maxDepth
 * arrives here already bounded and is defensively re-bounded (W40-4).
 *
 * `isVisible` (24.10.2026 audit fix) is the one addition: the graph tools pass
 * the agent's access-level gate in, and an invisible point is treated as
 * absent everywhere — never emitted, never traversed through.
 */

import type { QdrantClient } from "./qdrant-client.ts"
import { isActiveEdge, normalizeEdge } from "./edge.ts"
import { log } from "../logger.ts"

export interface BfsStep {
  fact_id: string
  depth: number
  relation: string
  path: string[]
}

export interface BfsNode {
  id: string
  depth: number
}

export interface BfsEdge {
  source: string
  target: string
  relation: string
}

export interface BfsOptions {
  relation?: string
  targetType?: string
  /** true → collect nodes+edges (subgraph); false → collect steps (traverse). */
  collectEdges?: boolean
  /**
   * Visibility gate for every point the walk touches (24.10.2026 audit fix).
   * A point that fails it is treated exactly like an absent point: it is not
   * emitted, not enqueued and — crucially — not traversed *through*, so its
   * own edges can never surface a node behind it. The graph tools pass
   * `access_level` in here; a missing level counts as `private` (fail-closed).
   */
  isVisible?: (payload?: Record<string, unknown>) => boolean
}

export interface BfsResult {
  steps: BfsStep[]
  nodes: BfsNode[]
  edges: BfsEdge[]
}

/**
 * Strict single-point lookup shared by every graph read (24.10.2026 audit fix).
 *
 * `scrollPoint()` returns null for BOTH "the point does not exist" (404) and
 * "the request failed" (500/403/network), so a short outage silently read as
 * "nothing there": get_related answered "No related facts found." while it had
 * merely failed to look. `scrollPointStrict()` keeps 404 = null and throws on
 * every other failure — the error reaches the tool's catch and is reported
 * honestly instead of vanishing.
 *
 * A client that predates the strict variant (test stubs) still works through
 * `scrollPoint`; the real QdrantClient always provides both.
 */
export async function loadPointStrict(
  qdrantClient: QdrantClient,
  id: string,
): Promise<{ id: string; payload?: Record<string, unknown> } | null> {
  const strict = qdrantClient.scrollPointStrict
  if (typeof strict === "function") return strict.call(qdrantClient, id)
  return qdrantClient.scrollPoint(id)
}

export async function bfsEdges(
  qdrantClient: QdrantClient,
  factId: string,
  maxDepth: number,
  opts: BfsOptions = {},
): Promise<BfsResult> {
  const { relation, targetType, collectEdges = false, isVisible } = opts

  // W40-4: maxDepth arrives here already clamped (H123), but it is a shared
  // entry point parameter — a non-finite value made `depth >= maxDepth` always
  // false and expanded the entire reachable graph.
  const depthLimit = Number.isFinite(maxDepth)
    ? Math.max(0, Math.trunc(maxDepth))
    : 0

  const visited = new Set<string>([factId])
  const queue: Array<{ id: string; depth: number; path: string[] }> = [
    { id: factId, depth: 0, path: [] },
  ]
  const steps: BfsStep[] = []
  const nodes: BfsNode[] = collectEdges ? [{ id: factId, depth: 0 }] : []
  const edges: BfsEdge[] = []

  // W40-4: one point lookup per node. The target_type check used to fetch a
  // newly discovered target and the dequeue then fetched the very same point
  // again — two HTTP calls per node plus N+1 latency on a wide fan-out.
  // 24.10.2026: the lookup is strict — 404 stays "unknown/skip", a failed
  // request throws out of the walk instead of shortening the result silently.
  // A throwing lookup stores nothing, so the cache never holds an error.
  const pointCache = new Map<
    string,
    Awaited<ReturnType<QdrantClient["scrollPointStrict"]>>
  >()
  const loadPoint = async (id: string) => {
    if (!pointCache.has(id)) {
      pointCache.set(id, await loadPointStrict(qdrantClient, id))
    }
    return pointCache.get(id) ?? null
  }

  // The visibility gate wraps the cached load (not the cache itself), so a
  // point is fetched once and judged on every use.
  const loadVisiblePoint = async (id: string) => {
    const pt = await loadPoint(id)
    if (!pt) return null
    if (isVisible && !isVisible(pt.payload)) return null
    return pt
  }

  // W40-4: head index instead of queue.shift() — shift() re-indexes the whole
  // frontier (O(n)) and made the BFS quadratic on large graphs.
  for (let head = 0; head < queue.length; head++) {
    const { id, depth, path } = queue[head]
    if (depth >= depthLimit) continue

    const pt = await loadVisiblePoint(id)
    if (!pt) continue

    // W40-4: `edges` is untyped JSON from arbitrary writers — a non-iterable
    // value (e.g. an object) made `for...of` throw out of the whole traversal.
    const rawEdges = pt.payload?.edges
    if (!Array.isArray(rawEdges)) continue
    for (const edge of rawEdges as Array<Record<string, unknown>>) {
      if (!isActiveEdge(edge)) continue

      // H125: validate before the id enters visited/queue/results — an
      // unvalidated `as string` used to push `undefined` into the graph.
      const normalized = normalizeEdge(edge)
      if (!normalized) {
        log.debug(`graph bfs: skipping malformed edge on ${id}`)
        continue
      }
      const targetId = normalized.targetId
      const edgeRelation = normalized.relation

      if (relation && edgeRelation !== relation) continue

      const source = path.length > 0 ? path[path.length - 1] : factId

      // Load the target BEFORE the visited/enqueue decision, so both the
      // visibility gate (isVisible) and the target_type filter see it first.
      // An invisible target must neither be enqueued (its own edges would leak
      // descendants into the result) nor recorded as a node or an edge
      // endpoint that was never added to `nodes` — both broke the subgraph
      // invariant. The load is cached (pointCache), so the later dequeue of
      // this target reuses it instead of issuing a second request.
      const targetPoint = await loadVisiblePoint(targetId)
      if (!targetPoint) continue
      if (targetType && targetPoint.payload?.entity_type !== targetType) continue

      if (visited.has(targetId)) {
        // traverse: a revisited target yields nothing. subgraph: still record
        // the edge, because its endpoints are already in the node set.
        if (!collectEdges) continue
        edges.push({ source, target: targetId, relation: edgeRelation })
        continue
      }
      visited.add(targetId)

      const step: BfsStep = {
        fact_id: targetId,
        depth: depth + 1,
        relation: edgeRelation,
        path: [...path, targetId],
      }

      if (collectEdges) {
        // W40-4: nodeSet was redundant bookkeeping — this branch is only
        // reached when `visited.has(targetId)` was false, and every id in
        // nodeSet is also in visited, so a duplicate node was impossible.
        nodes.push({ id: targetId, depth: depth + 1 })
        edges.push({ source, target: targetId, relation: edgeRelation })
      } else {
        steps.push(step)
      }
      queue.push({ id: targetId, depth: depth + 1, path: step.path })
    }
  }

  return { steps, nodes, edges }
}
