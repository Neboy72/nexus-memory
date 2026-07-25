"""Graph Traversal — multi-hop queries over the entity graph.

Builds on SkillGraph (NetworkX) to answer questions like:
- "Was haengt mit der Wallbox zusammen?" (multi-hop traversal)
- "Welche Services laufen auf dem Mac Mini?" (filtered traversal)
- "Welche Geraete verwaltet Home Assistant?" (relation-filtered)

Usage::

    from nexus.graph.traversal import GraphTraversal

    gt = GraphTraversal(skill_graph)
    gt.initialize()

    # All entities connected to "Wallbox ABL eMH3" (any depth)
    neighbors = gt.traverse("Wallbox ABL eMH3", max_depth=3)

    # Only devices that Home Assistant manages
    managed = gt.traverse(
        "Home Assistant",
        relation="manages",
        target_type="device",
    )
"""

from __future__ import annotations

import logging
from collections import deque
from typing import Any, Dict, List, Optional, Set

from nexus.graph.graph import SkillGraph
from nexus.graph.schema import EdgeRelation

logger = logging.getLogger(__name__)

# Relations that indicate a Knowledge Graph entity relationship
KG_RELATIONS = {
    EdgeRelation.INSTALLED_AT.value,
    EdgeRelation.CONNECTED_TO.value,
    EdgeRelation.MANAGES.value,
    EdgeRelation.RUNS_ON.value,
    EdgeRelation.PART_OF.value,
    EdgeRelation.OWNS.value,
    EdgeRelation.LOCATED_AT.value,
    EdgeRelation.DEPENDS_ON_SERVICE.value,
    EdgeRelation.USES.value,
    EdgeRelation.PROVIDES.value,
    EdgeRelation.CONTROLS.value,
}


class GraphTraversal:
    """Multi-hop graph traversal over the SkillGraph.

    Wraps SkillGraph (NetworkX-backed) to provide higher-level queries
    optimized for the Knowledge Graph Layer.
    """

    def __init__(self, graph: SkillGraph):
        self._graph = graph

    def initialize(self) -> None:
        """Ensure the underlying SkillGraph is initialized."""
        self._graph.initialize()

    def traverse(
        self,
        start_fact_id: str,
        max_depth: int = 3,
        relation: Optional[str] = None,
        target_type: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Multi-hop traversal from a starting fact.

        Args:
            start_fact_id: The Qdrant point ID to start from.
            max_depth: Maximum hops (default 3).
            relation: Only follow edges with this relation (e.g. "manages").
            target_type: Only return targets with this entity_type in payload.

        Returns:
            List of {fact_id, depth, relation, path} dicts.
        """
        if not self._graph.has_node(start_fact_id):
            return []

        results: List[Dict[str, Any]] = []
        visited: Set[str] = {start_fact_id}

        # BFS with depth tracking
        queue: deque = deque([(start_fact_id, 0, [])])

        while queue:
            current, depth, path = queue.popleft()

            if depth >= max_depth:
                continue

            neighbors = self._graph.neighbors(current, relation=relation)

            for neighbor in neighbors:
                neighbor_id = neighbor["fact_id"]
                if neighbor_id in visited:
                    continue
                visited.add(neighbor_id)

                step = {
                    "fact_id": neighbor_id,
                    "depth": depth + 1,
                    "relation": neighbor["relation"],
                    "path": path + [neighbor_id],
                }

                # Filter by target_type if specified
                if target_type:
                    point = self._graph.store._scroll_point(neighbor_id)
                    if point:
                        payload = point.get("payload", {})
                        if payload.get("entity_type") != target_type:
                            # Skip but continue traversing
                            queue.append((neighbor_id, depth + 1, step["path"]))
                            continue

                results.append(step)
                queue.append((neighbor_id, depth + 1, step["path"]))

        return results

    def find_entities(
        self,
        entity_type: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """Find all entity-typed facts in the graph.

        Scans Qdrant for points with category="entity".
        """
        from qdrant_client import models as qm

        try:
            client = self._graph.store.client
            collection = self._graph.store._collection

            # Filter by entity_type if specified
            must_conditions: list[qm.Condition] = [
                qm.FieldCondition(
                    key="category",
                    match=qm.MatchValue(value="entity"),
                ),
            ]
            if entity_type:
                must_conditions.append(
                    qm.FieldCondition(
                        key="entity_type",
                        match=qm.MatchValue(value=entity_type),
                    ),
                )

            filter_ = qm.Filter(must=must_conditions)

            points, _ = client.scroll(
                collection_name=collection,
                scroll_filter=filter_,
                limit=limit,
                with_payload=True,
                with_vectors=False,
            )

            results = []
            for pt in points:
                payload = pt.payload or {}
                results.append({
                    "id": str(pt.id),
                    "name": payload.get("entity_name", ""),
                    "entity_type": payload.get("entity_type", ""),
                    "content": payload.get("content", "")[:200],
                    "attributes": payload.get("entity_attributes", {}),
                })

            return results

        except Exception as exc:
            logger.warning("find_entities failed: %s", exc)
            return []

    def get_subgraph(
        self,
        start_fact_id: str,
        max_depth: int = 2,
    ) -> Dict[str, Any]:
        """Get a subgraph centered on start_fact_id.

        Returns nodes and edges for visualization.
        """
        nodes: Dict[str, Dict[str, Any]] = {}
        edges: List[Dict[str, Any]] = []

        # Add start node
        if not self._graph.has_node(start_fact_id):
            return {"nodes": [], "edges": []}

        nodes[start_fact_id] = {"id": start_fact_id, "depth": 0}

        # Traverse and collect
        results = self.traverse(start_fact_id, max_depth=max_depth)

        for r in results:
            fid = r["fact_id"]
            if fid not in nodes:
                nodes[fid] = {"id": fid, "depth": r["depth"]}
            # Add edge from path
            if r["path"]:
                source = r["path"][-1] if len(r["path"]) > 1 else start_fact_id
                # Actually the last element in path is the current node
                # and the second-to-last is where we came from
                if len(r["path"]) >= 2:
                    source = r["path"][-2]
                else:
                    source = start_fact_id
                edges.append({
                    "source": source,
                    "target": fid,
                    "relation": r["relation"],
                })

        return {
            "nodes": list(nodes.values()),
            "edges": edges,
        }

    def get_related(
        self,
        fact_id: str,
        relation: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Get directly related facts (1-hop, bidirectional).

        Simpler than traverse() — just immediate neighbors.
        """
        return self._graph.neighbors(fact_id, relation=relation)

    def stats(self) -> Dict[str, Any]:
        """Graph statistics including KG-specific counts."""
        base_stats = self._graph.stats()

        # Count KG-specific relations
        kg_edge_count = 0
        all_edges = self._graph.list_edges(status="active")
        for edge in all_edges:
            if edge.relation in KG_RELATIONS:
                kg_edge_count += 1

        return {
            **base_stats,
            "kg_edges": kg_edge_count,
            "total_relations": len(all_edges),
        }