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
 * arrives here already bounded.
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

  const visited = new Set<string>([factId])
  const queue: Array<{ id: string; depth: number; path: string[] }> = [
    { id: factId, depth: 0, path: [] },
  ]
  const steps: BfsStep[] = []
  const nodes: BfsNode[] = collectEdges ? [{ id: factId, depth: 0 }] : []
  const nodeSet = new Set<string>([factId])
  const edges: BfsEdge[] = []

  while (queue.length > 0) {
    const { id, depth, path } = queue.shift()!
    if (depth >= maxDepth) continue

    const pt = await qdrantClient.scrollPoint(id)
    if (!pt) continue

    const ptEdges = (pt.payload?.edges ?? []) as Array<Record<string, unknown>>
    for (const edge of ptEdges) {
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
        const targetPoint = await qdrantClient.scrollPoint(targetId)
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
        if (!nodeSet.has(targetId)) {
          nodes.push({ id: targetId, depth: depth + 1 })
          nodeSet.add(targetId)
        }
        edges.push({ source, target: targetId, relation: edgeRelation })
      } else {
        steps.push(step)
      }
      queue.push({ id: targetId, depth: depth + 1, path: step.path })
    }
  }

  return { steps, nodes, edges }
}
