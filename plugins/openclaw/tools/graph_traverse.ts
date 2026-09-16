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
import { clampInt } from "../lib/num.ts"
import { isActiveEdge, normalizeEdge } from "../lib/edge.ts"
import { bfsEdges } from "../lib/bfs.ts"
import { log } from "../logger.ts"

// H123: hard bounds for the graph tools, mirrored by their Typebox schemas.
const TRAVERSE_DEPTH_DEFAULT = 3
const TRAVERSE_DEPTH_MAX = 5
const ENTITIES_LIMIT_DEFAULT = 50
const ENTITIES_LIMIT_MAX = 500
const SUBGRAPH_DEPTH_DEFAULT = 2
const SUBGRAPH_DEPTH_MAX = 5

// W31-18: relations that may enter the public result contract. Mirrors the
// canonical `nexus.graph.schema.EdgeRelation` values — the whitelist the
// outgoing (normalizeEdge) and incoming validation gates share.
const KG_RELATIONS: readonly string[] = [
  "supersedes",
  "contradicts",
  "supports",
  "alternative_to",
  "depends_on",
  "references",
  "installed_at",
  "connected_to",
  "manages",
  "runs_on",
  "part_of",
  "owns",
  "located_at",
  "depends_on_service",
  "uses",
  "provides",
  "controls",
]

/**
 * Validate one INCOMING edge (a `findIncomingEdges` result) before it enters
 * the public `{ fact_id, relation, edge_id }` contract.
 *
 * W31-18(a): the incoming branch used to push raw payload fields as casts —
 * mirroring the H125 gate the outgoing branch already runs through
 * (`normalizeEdge`): fact_id must be a non-empty string, relation must be a
 * non-empty whitelisted string, edge_id degrades to "" (never `undefined`).
 */
function validateIncomingEdge(
  edge: { source_id?: unknown; relation?: unknown; edge_id?: unknown } | null | undefined,
): { fact_id: string; relation: string; edge_id: string } | null {
  if (!edge || typeof edge !== "object") return null
  const factId = edge.source_id
  if (typeof factId !== "string" || factId === "") return null
  const relation = edge.relation
  if (typeof relation !== "string" || relation === "") return null
  if (!KG_RELATIONS.includes(relation)) return null
  const edgeId =
    typeof edge.edge_id === "string" && edge.edge_id !== "" ? edge.edge_id : ""
  return { fact_id: factId, relation, edge_id: edgeId }
}

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
          Type.Number({
            description: "Maximum hops (default 3)",
            default: 3,
            minimum: 1,
            maximum: TRAVERSE_DEPTH_MAX,
          }),
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
        // H123: bound the BFS fan-out (raw host value never reaches the loop).
        const maxDepth = clampInt(params.max_depth, TRAVERSE_DEPTH_DEFAULT, 1, TRAVERSE_DEPTH_MAX)
        const relation = params.relation || undefined
        const targetType = params.target_type || undefined

        try {
          const point = await qdrantClient.scrollPoint(factId)
          if (!point) {
            return {
              content: [{ type: "text" as const, text: `Fact ${factId} not found` }],
            }
          }

          // BFS traversal over edges in Qdrant payloads (shared lib/bfs.ts).
          const { steps: results } = await bfsEdges(qdrantClient, factId, maxDepth, {
            relation,
            targetType,
          })

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
          Type.Number({
            description: "Max results (default 50)",
            default: 50,
            minimum: 1,
            maximum: ENTITIES_LIMIT_MAX,
          }),
        ),
      }),
      async execute(
        _toolCallId: string,
        params: { entity_type?: string; limit?: number },
      ) {
        const entityType = params.entity_type || undefined
        // H123: bound the paginated scroll fan-out (per page).
        const limit = clampInt(params.limit, ENTITIES_LIMIT_DEFAULT, 1, ENTITIES_LIMIT_MAX)

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

          // W31-18(b): `limit` is the PER-PAGE scroll limit; scrollFiltered
          // walks up to 5 pages, so without a total cap this returned up to
          // limit*5 entities. Pass `limit` as the total cap as well.
          const points = await qdrantClient.scrollFiltered(filter, limit, limit)
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
          Type.Number({
            description: "Maximum hops (default 2)",
            default: 2,
            minimum: 1,
            maximum: SUBGRAPH_DEPTH_MAX,
          }),
        ),
      }),
      async execute(_toolCallId: string, params: { fact_id: string; max_depth?: number }) {
        const { fact_id: factId } = params
        // H123: bound the BFS fan-out (raw host value never reaches the loop).
        const maxDepth = clampInt(params.max_depth, SUBGRAPH_DEPTH_DEFAULT, 1, SUBGRAPH_DEPTH_MAX)

        try {
          // Shared BFS (lib/bfs.ts), subgraph mode: collect nodes + edges.
          const { nodes, edges } = await bfsEdges(qdrantClient, factId, maxDepth, {
            collectEdges: true,
          })

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
            if (!isActiveEdge(edge)) continue

            // H125: only validated values may enter the public contract —
            // results used to carry `fact_id: undefined` / `edge_id: undefined`.
            const normalized = normalizeEdge(edge)
            if (!normalized) {
              log.debug(`get_related: skipping malformed edge on ${factId}`)
              continue
            }
            if (relation && normalized.relation !== relation) continue

            results.push({
              fact_id: normalized.targetId,
              relation: normalized.relation,
              edge_id: normalized.edgeId,
              direction: "outgoing",
            })
          }

          // Incoming edges
          const incoming = await qdrantClient.findIncomingEdges(factId, relation)
          for (const edge of incoming) {
            // W31-18(a): run the SAME validation gate as the outgoing branch
            // (normalizeEdge/H125). Raw casts used to leak `fact_id:
            // undefined` / `edge_id: undefined` into the public contract.
            const normalized = validateIncomingEdge(edge)
            if (!normalized) {
              log.debug(`get_related: skipping malformed incoming edge on ${factId}`)
              continue
            }
            if (relation && normalized.relation !== relation) continue
            results.push({
              fact_id: normalized.fact_id,
              relation: normalized.relation,
              edge_id: normalized.edge_id,
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