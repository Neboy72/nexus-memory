/**
 * Nexus Memory — Knowledge Graph Tools for OpenClaw
 *
 * Provides graph traversal, entity search, subgraph, and related-facts queries.
 * Uses the shared Qdrant collection via REST API (same as other OpenClaw tools).
 */

import { qdrantClient } from '../lib/qdrant-client.js';
import { logger } from '../logger.js';

export const graphTraverseTool = {
  name: 'nexus_graph_traverse',
  description:
    'Knowledge Graph: Multi-hop traversal from a starting fact. ' +
    "Answers 'what is connected to X?' across the entity graph.",
  inputSchema: {
    type: 'object',
    properties: {
      fact_id: { type: 'string', description: 'The Qdrant point ID to start traversal from' },
      max_depth: { type: 'integer', description: 'Maximum hops (default 3)', default: 3 },
      relation: { type: 'string', description: "Only follow edges with this relation (e.g. 'manages', 'runs_on')", default: '' },
      target_type: { type: 'string', description: "Only return targets with this entity_type (e.g. 'device', 'service')", default: '' },
    },
    required: ['fact_id'],
  },
  handler: async (args: Record<string, unknown>) => {
    const factId = args.fact_id as string;
    const maxDepth = (args.max_depth as number) || 3;
    const relation = (args.relation as string) || undefined;
    const targetType = (args.target_type as string) || undefined;

    try {
      // Scroll the source point to get its edges
      const point = await qdrantClient.scrollPoint(factId);
      if (!point) {
        return { status: 'error', error: `Fact ${factId} not found` };
      }

      // BFS traversal over edges in Qdrant payloads
      const visited = new Set<string>([factId]);
      const queue: Array<{ id: string; depth: number; path: string[] }> = [{ id: factId, depth: 0, path: [] }];
      const results: Array<Record<string, unknown>> = [];

      while (queue.length > 0) {
        const { id, depth, path } = queue.shift()!;
        if (depth >= maxDepth) continue;

        const pt = await qdrantClient.scrollPoint(id);
        if (!pt) continue;

        const edges = (pt.payload?.edges || []) as Array<Record<string, unknown>>;
        for (const edge of edges) {
          const edgeStatus = edge.status as string;
          if (edgeStatus && edgeStatus !== 'active') continue;

          const targetId = edge.target_fact_id as string;
          const edgeRelation = edge.relation as string;

          if (relation && edgeRelation !== relation) continue;

          if (visited.has(targetId)) continue;
          visited.add(targetId);

          const step: Record<string, unknown> = {
            fact_id: targetId,
            depth: depth + 1,
            relation: edgeRelation,
            path: [...path, targetId],
          };

          // Filter by target_type if specified
          if (targetType) {
            const targetPoint = await qdrantClient.scrollPoint(targetId);
            const entityType = targetPoint?.payload?.entity_type;
            if (entityType !== targetType) {
              queue.push({ id: targetId, depth: depth + 1, path: step.path as string[] });
              continue;
            }
          }

          results.push(step);
          queue.push({ id: targetId, depth: depth + 1, path: step.path as string[] });
        }
      }

      return { results };
    } catch (error) {
      logger.error('Graph traverse failed:', error);
      return { status: 'error', error: String(error) };
    }
  },
};

export const findEntitiesTool = {
  name: 'nexus_find_entities',
  description:
    'Knowledge Graph: Find all entity-typed memories. ' +
    'Returns list of {id, name, entity_type, content, attributes}.',
  inputSchema: {
    type: 'object',
    properties: {
      entity_type: { type: 'string', description: 'Filter by entity type: device, service, person, location, organization, concept, software, protocol', default: '' },
      limit: { type: 'integer', description: 'Max results (default 50)', default: 50 },
    },
    required: [],
  },
  handler: async (args: Record<string, unknown>) => {
    const entityType = (args.entity_type as string) || undefined;
    const limit = (args.limit as number) || 50;

    try {
      const filter: Record<string, unknown> = { must: [{ key: 'category', match: { value: 'entity' } }] };
      if (entityType) {
        (filter.must as Array<Record<string, unknown>>).push({ key: 'entity_type', match: { value: entityType } });
      }

      const points = await qdrantClient.scrollFiltered(filter, limit);
      const entities = points.map((pt: Record<string, unknown>) => {
        const payload = pt.payload as Record<string, unknown>;
        return {
          id: String(pt.id),
          name: payload?.entity_name || '',
          entity_type: payload?.entity_type || '',
          content: String(payload?.content || '').slice(0, 200),
          attributes: payload?.entity_attributes || {},
        };
      });

      return { entities };
    } catch (error) {
      logger.error('Find entities failed:', error);
      return { status: 'error', error: String(error) };
    }
  },
};

export const getSubgraphTool = {
  name: 'nexus_get_subgraph',
  description:
    'Knowledge Graph: Get a subgraph centered on a fact for visualization. ' +
    'Returns {nodes, edges}.',
  inputSchema: {
    type: 'object',
    properties: {
      fact_id: { type: 'string', description: 'The Qdrant point ID to center the subgraph on' },
      max_depth: { type: 'integer', description: 'Maximum hops (default 2)', default: 2 },
    },
    required: ['fact_id'],
  },
  handler: async (args: Record<string, unknown>) => {
    const factId = args.fact_id as string;
    const maxDepth = (args.max_depth as number) || 2;

    try {
      // Reuse graph_traverse logic
      const traverseResult = await graphTraverseTool.handler({ fact_id: factId, max_depth: maxDepth });
      const traverseResults = (traverseResult as Record<string, unknown>).results as Array<Record<string, unknown>> || [];

      const nodes: Array<Record<string, unknown>> = [{ id: factId, depth: 0 }];
      const edges: Array<Record<string, unknown>> = [];
      const nodeSet = new Set<string>([factId]);

      for (const r of traverseResults) {
        const fid = r.fact_id as string;
        if (!nodeSet.has(fid)) {
          nodes.push({ id: fid, depth: r.depth });
          nodeSet.add(fid);
        }
        const path = r.path as string[];
        const source = path.length >= 2 ? path[path.length - 2] : factId;
        edges.push({ source, target: fid, relation: r.relation });
      }

      return { nodes, edges };
    } catch (error) {
      logger.error('Get subgraph failed:', error);
      return { status: 'error', error: String(error) };
    }
  },
};

export const getRelatedTool = {
  name: 'nexus_get_related',
  description:
    'Knowledge Graph: Get directly related facts (1-hop, bidirectional). ' +
    'Returns list of {fact_id, relation, direction}.',
  inputSchema: {
    type: 'object',
    properties: {
      fact_id: { type: 'string', description: 'The Qdrant point ID to find neighbors for' },
      relation: { type: 'string', description: "Only return edges with this relation (e.g. 'manages')", default: '' },
    },
    required: ['fact_id'],
  },
  handler: async (args: Record<string, unknown>) => {
    const factId = args.fact_id as string;
    const relation = (args.relation as string) || undefined;

    try {
      // Get outgoing edges from the point
      const point = await qdrantClient.scrollPoint(factId);
      if (!point) {
        return { results: [] };
      }

      const edges = (point.payload?.edges || []) as Array<Record<string, unknown>>;
      const results: Array<Record<string, unknown>> = [];

      for (const edge of edges) {
        const edgeStatus = edge.status as string;
        if (edgeStatus && edgeStatus !== 'active') continue;

        const edgeRelation = edge.relation as string;
        if (relation && edgeRelation !== relation) continue;

        results.push({
          fact_id: edge.target_fact_id,
          relation: edgeRelation,
          edge_id: edge.edge_id,
          direction: 'outgoing',
        });
      }

      // Also find incoming edges (where target_fact_id == factId)
      const incoming = await qdrantClient.findIncomingEdges(factId, relation);
      for (const edge of incoming) {
        results.push({
          fact_id: edge.source_id,
          relation: edge.relation,
          edge_id: edge.edge_id,
          direction: 'incoming',
        });
      }

      return { results };
    } catch (error) {
      logger.error('Get related failed:', error);
      return { status: 'error', error: String(error) };
    }
  },
};