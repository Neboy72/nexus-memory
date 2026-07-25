/**
 * Nexus Memory — Knowledge Graph Tools for OpenClaw
 *
 * Provides graph traversal, entity search, subgraph, and related-facts queries.
 * Uses the shared Qdrant collection via REST API (same as other OpenClaw tools).
 */

import { Type } from "@sinclair/typebox"
import type { OpenClawPluginApi } from "openclaw/plugin-sdk"
import type { QdrantClient } from "../lib/qdrant-client.ts"
import type { NexusConfig } from "../lib/config.ts"
import { log } from "../logger.ts"

/**
 * Multi-hop traversal from a starting fact.
 * Answers 'what is connected to X?' across the entity graph.
 */
export function registerGraphTraverseTool(
  api: OpenClawPluginApi,
  qdrantClient: QdrantClient,
  _cfg: NexusConfig,
  toolName = "nexus_graph_traverse",
): void {
  api.registerTool(
    {
      name: toolName,
      label: "Nexus Graph Traverse",
      description:
        "Knowledge Graph: Multi-hop traversal from a starting fact. " +
        "Answers 'what is connected to X?' across the entity graph. " +
        "Returns a list of { fact_id, depth, relation, path }.",
      parameters: Type.Object({
        fact_id: Type.String({ description: "The Qdrant point ID to start traversal from" }),
        max_depth: Type.Optional(
          Type.Number({ description: "Maximum hops (default 3)", default: 3 }),
        ),
        relation: Type.Optional(
          Type.String({
            description: "Only follow edges with this relation (e.g. 'manages', 'runs_on')",
          }),
        ),
        target_type: Type.Optional(
          Type.String({
            description: "Only return targets with this entity_type (e.g. 'device', 'service')",
          }),
        ),
      }),
      async execute(
        _toolCallId: string,
        params: { fact_id: string; max_depth?: number; relation?: string; target_type?: string },
      ) {
        const { fact_id: factId } = params
        const maxDepth = params.max_depth ?? 3
        const relation = params.relation || undefined
        const targetType = params.target_type || undefined

        try {
          const point = await qdrantClient.scrollPoint(factId)
          if (!point) {
            return {
              content: [{ type: "text" as const, text: `Fact ${factId} not found` }],
            }
          }

          // BFS traversal over edges in Qdrant payloads
          const visited = new Set<string>([factId])
          const queue: Array<{ id: string; depth: number; path: string[] }> = [
            { id: factId, depth: 0, path: [] },
          ]
          const results: Array<Record<string, unknown>> = []

          while (queue.length > 0) {
            const { id, depth, path } = queue.shift()!
            if (depth >= maxDepth) continue

            const pt = await qdrantClient.scrollPoint(id)
            if (!pt) continue

            const edges = (pt.payload?.edges ?? []) as Array<Record<string, unknown>>
            for (const edge of edges) {
              const edgeStatus = edge.status as string
              if (edgeStatus && edgeStatus !== "active") continue

              const targetId = edge.target_fact_id as string
              const edgeRelation = edge.relation as string

              if (relation && edgeRelation !== relation) continue
              if (visited.has(targetId)) continue
              visited.add(targetId)

              const step: Record<string, unknown> = {
                fact_id: targetId,
                depth: depth + 1,
                relation: edgeRelation,
                path: [...path, targetId],
              }

              // Filter by target_type if specified
              if (targetType) {
                const targetPoint = await qdrantClient.scrollPoint(targetId)
                const entityType = targetPoint?.payload?.entity_type
                if (entityType !== targetType) {
                  queue.push({ id: targetId, depth: depth + 1, path: step.path as string[] })
                  continue
                }
              }

              results.push(step)
              queue.push({ id: targetId, depth: depth + 1, path: step.path as string[] })
            }
          }

          if (results.length === 0) {
            return {
              content: [{ type: "text" as const, text: "No connected facts found." }],
            }
          }

          const text = results
            .map((r, i) => {
              const pathStr = (r.path as string[]).join(" → ")
              return `${i + 1}. [depth=${r.depth}] ${r.relation} → ${r.fact_id} (path: ${pathStr})`
            })
            .join("\n")

          return {
            content: [
              { type: "text" as const, text: `Found ${results.length} connected facts:\n\n${text}` },
            ],
            details: { count: results.length, results },
          }
        } catch (err) {
          log.error("graph_traverse failed", err)
          return {
            content: [
              {
                type: "text" as const,
                text: `Graph traverse failed: ${err instanceof Error ? err.message : String(err)}`,
              },
            ],
          }
        }
      },
    },
    { name: toolName },
  )
}

/**
 * Find all entity-typed memories in Qdrant.
 * Returns a list of { id, name, entity_type, content, attributes }.
 */
export function registerFindEntitiesTool(
  api: OpenClawPluginApi,
  qdrantClient: QdrantClient,
  _cfg: NexusConfig,
  toolName = "nexus_find_entities",
): void {
  api.registerTool(
    {
      name: toolName,
      label: "Nexus Find Entities",
      description:
        "Knowledge Graph: Find all entity-typed memories in Qdrant. " +
        "Returns a list of { id, name, entity_type, content, attributes }.",
      parameters: Type.Object({
        entity_type: Type.Optional(
          Type.String({
            description:
              "Filter by entity type: device, service, person, location, organization, concept, software, protocol",
          }),
        ),
        limit: Type.Optional(
          Type.Number({ description: "Max results (default 50)", default: 50 }),
        ),
      }),
      async execute(
        _toolCallId: string,
        params: { entity_type?: string; limit?: number },
      ) {
        const entityType = params.entity_type || undefined
        const limit = params.limit ?? 50

        try {
          const filter: Record<string, unknown> = {
            must: [{ key: "category", match: { value: "entity" } }],
          }
          if (entityType) {
            ;(filter.must as Array<Record<string, unknown>>).push({
              key: "entity_type",
              match: { value: entityType },
            })
          }

          const points = await qdrantClient.scrollFiltered(filter, limit)
          const entities = points.map((pt) => {
            const payload = (pt.payload ?? {}) as Record<string, unknown>
            return {
              id: pt.id,
              name: payload.entity_name ?? "",
              entity_type: payload.entity_type ?? "",
              content: String(payload.content ?? "").slice(0, 200),
              attributes: payload.entity_attributes ?? {},
            }
          })

          if (entities.length === 0) {
            return {
              content: [{ type: "text" as const, text: "No entities found." }],
            }
          }

          const text = entities
            .map(
              (e, i) =>
                `${i + 1}. [${e.entity_type}] ${e.name} (id: ${e.id}) — ${e.content.slice(0, 80)}`,
            )
            .join("\n")

          return {
            content: [
              { type: "text" as const, text: `Found ${entities.length} entities:\n\n${text}` },
            ],
            details: { count: entities.length, entities },
          }
        } catch (err) {
          log.error("find_entities failed", err)
          return {
            content: [
              {
                type: "text" as const,
                text: `Find entities failed: ${err instanceof Error ? err.message : String(err)}`,
              },
            ],
          }
        }
      },
    },
    { name: toolName },
  )
}

/**
 * Get a subgraph centered on a fact for visualization.
 * Returns { nodes, edges } where nodes have { id, depth } and edges have { source, target, relation }.
 */
export function registerGetSubgraphTool(
  api: OpenClawPluginApi,
  qdrantClient: QdrantClient,
  cfg: NexusConfig,
  toolName = "nexus_get_subgraph",
): void {
  api.registerTool(
    {
      name: toolName,
      label: "Nexus Get Subgraph",
      description:
        "Knowledge Graph: Get a subgraph centered on a fact for visualization. " +
        "Returns { nodes, edges } where nodes have { id, depth } and edges have { source, target, relation }.",
      parameters: Type.Object({
        fact_id: Type.String({
          description: "The Qdrant point ID to center the subgraph on",
        }),
        max_depth: Type.Optional(
          Type.Number({ description: "Maximum hops (default 2)", default: 2 }),
        ),
      }),
      async execute(_toolCallId: string, params: { fact_id: string; max_depth?: number }) {
        const { fact_id: factId } = params
        const maxDepth = params.max_depth ?? 2

        try {
          // Reuse BFS logic
          const visited = new Set<string>([factId])
          const queue: Array<{ id: string; depth: number; path: string[] }> = [
            { id: factId, depth: 0, path: [] },
          ]
          const nodes: Array<Record<string, unknown>> = [{ id: factId, depth: 0 }]
          const edges: Array<Record<string, unknown>> = []
          const nodeSet = new Set<string>([factId])

          while (queue.length > 0) {
            const { id, depth, path } = queue.shift()!
            if (depth >= maxDepth) continue

            const pt = await qdrantClient.scrollPoint(id)
            if (!pt) continue

            const ptEdges = (pt.payload?.edges ?? []) as Array<Record<string, unknown>>
            for (const edge of ptEdges) {
              const edgeStatus = edge.status as string
              if (edgeStatus && edgeStatus !== "active") continue

              const targetId = edge.target_fact_id as string
              const edgeRelation = edge.relation as string

              if (visited.has(targetId)) {
                // Still add the edge even if node already exists
                const source = path.length > 0 ? path[path.length - 1] : factId
                edges.push({ source, target: targetId, relation: edgeRelation })
                continue
              }
              visited.add(targetId)

              if (!nodeSet.has(targetId)) {
                nodes.push({ id: targetId, depth: depth + 1 })
                nodeSet.add(targetId)
              }

              const source = path.length > 0 ? path[path.length - 1] : factId
              edges.push({ source, target: targetId, relation: edgeRelation })

              queue.push({ id: targetId, depth: depth + 1, path: [...path, targetId] })
            }
          }

          if (nodes.length <= 1) {
            return {
              content: [{ type: "text" as const, text: "No subgraph found around this fact." }],
            }
          }

          return {
            content: [
              {
                type: "text" as const,
                text: `Subgraph: ${nodes.length} nodes, ${edges.length} edges (centered on ${factId})`,
              },
            ],
            details: { nodes, edges },
          }
        } catch (err) {
          log.error("get_subgraph failed", err)
          return {
            content: [
              {
                type: "text" as const,
                text: `Get subgraph failed: ${err instanceof Error ? err.message : String(err)}`,
              },
            ],
          }
        }
      },
    },
    { name: toolName },
  )
}

/**
 * Get directly related facts (1-hop, bidirectional).
 * Returns a list of { fact_id, relation, edge_id, direction }.
 */
export function registerGetRelatedTool(
  api: OpenClawPluginApi,
  qdrantClient: QdrantClient,
  _cfg: NexusConfig,
  toolName = "nexus_get_related",
): void {
  api.registerTool(
    {
      name: toolName,
      label: "Nexus Get Related",
      description:
        "Knowledge Graph: Get directly related facts (1-hop, bidirectional). " +
        "Returns a list of { fact_id, relation, edge_id, direction }.",
      parameters: Type.Object({
        fact_id: Type.String({
          description: "The Qdrant point ID to find neighbors for",
        }),
        relation: Type.Optional(
          Type.String({
            description: "Only return edges with this relation (e.g. 'manages')",
          }),
        ),
      }),
      async execute(_toolCallId: string, params: { fact_id: string; relation?: string }) {
        const { fact_id: factId } = params
        const relation = params.relation || undefined

        try {
          const point = await qdrantClient.scrollPoint(factId)
          if (!point) {
            return {
              content: [{ type: "text" as const, text: `Fact ${factId} not found` }],
            }
          }

          const edges = (point.payload?.edges ?? []) as Array<Record<string, unknown>>
          const results: Array<Record<string, unknown>> = []

          // Outgoing edges
          for (const edge of edges) {
            const edgeStatus = edge.status as string
            if (edgeStatus && edgeStatus !== "active") continue

            const edgeRelation = edge.relation as string
            if (relation && edgeRelation !== relation) continue

            results.push({
              fact_id: edge.target_fact_id,
              relation: edgeRelation,
              edge_id: edge.edge_id,
              direction: "outgoing",
            })
          }

          // Incoming edges
          const incoming = await qdrantClient.findIncomingEdges(factId, relation)
          for (const edge of incoming) {
            results.push({
              fact_id: edge.source_id,
              relation: edge.relation,
              edge_id: edge.edge_id,
              direction: "incoming",
            })
          }

          if (results.length === 0) {
            return {
              content: [{ type: "text" as const, text: "No related facts found." }],
            }
          }

          const text = results
            .map((r, i) => {
              const dir = r.direction === "outgoing" ? "→" : "←"
              return `${i + 1}. ${dir} [${r.relation}] ${r.fact_id} (${r.direction})`
            })
            .join("\n")

          return {
            content: [
              { type: "text" as const, text: `Found ${results.length} related facts:\n\n${text}` },
            ],
            details: { count: results.length, results },
          }
        } catch (err) {
          log.error("get_related failed", err)
          return {
            content: [
              {
                type: "text" as const,
                text: `Get related failed: ${err instanceof Error ? err.message : String(err)}`,
              },
            ],
          }
        }
      },
    },
    { name: toolName },
  )
}