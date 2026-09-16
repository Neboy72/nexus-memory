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
}

export interface BfsResult {
  steps: BfsStep[]
  nodes: BfsNode[]
  edges: BfsEdge[]
}

export async function bfsEdges(
  qdrantClient: QdrantClient,
  factId: string,
  maxDepth: number,
  opts: BfsOptions = {},
): Promise<BfsResult> {
  const { relation, targetType, collectEdges = false } = opts

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

  // W40-4: one scrollPoint per node. The target_type check used to fetch a
  // newly discovered target and the dequeue then fetched the very same point
  // again — two HTTP calls per node plus N+1 latency on a wide fan-out.
  const pointCache = new Map<
    string,
    Awaited<ReturnType<QdrantClient["scrollPoint"]>>
  >()
  const loadPoint = async (id: string) => {
    if (!pointCache.has(id)) {
      pointCache.set(id, await qdrantClient.scrollPoint(id))
    }
    return pointCache.get(id) ?? null
  }

  // W40-4: head index instead of queue.shift() — shift() re-indexes the whole
  // frontier (O(n)) and made the BFS quadratic on large graphs.
  for (let head = 0; head < queue.length; head++) {
    const { id, depth, path } = queue[head]
    if (depth >= depthLimit) continue

    const pt = await loadPoint(id)
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

      // Filter by target_type BEFORE the visited/enqueue decision. A
      // non-matching node must neither be enqueued (its descendants would
      // leak into the result) nor recorded as an edge endpoint that was never
      // added to `nodes`/`nodeSet` — both broke the subgraph invariant. Doing
      // this first also makes the revisited branch below consistent for free:
      // only targets that already passed the filter are ever marked visited.
      if (targetType) {
        const targetPoint = await loadPoint(targetId)
        const entityType = targetPoint?.payload?.entity_type
        if (entityType !== targetType) continue
      }

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
