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

        # BFS level by level. Each frontier is a list of (node, path-to-node)
        # so the target_type payloads can be fetched in ONE batched scroll per
        # level (H247) instead of one scroll per neighbor.
        frontier: List[tuple] = [(start_fact_id, [])]
        depth = 0

        while frontier and depth < max_depth:
            # (neighbor_id, relation, path, is_new)
            candidates: List[tuple] = []
            for current, path in frontier:
                for neighbor in self._graph.neighbors(current, relation=relation):
                    neighbor_id = neighbor["fact_id"]
                    if neighbor_id in visited:
                        continue
                    visited.add(neighbor_id)
                    candidates.append(
                        (neighbor_id, neighbor["relation"], path + [neighbor_id])
                    )

            if not candidates:
                break

            if target_type:
                # H248: strict type filtering. A missing payload (point
                # deleted) or a point without ``entity_type`` is SKIPPED, not
                # appended — an unknown type must not silently satisfy the
                # type constraint ("unknown type included" is not documented).
                payloads = self._fetch_payloads([c[0] for c in candidates])
                for neighbor_id, rel, path in candidates:
                    payload = payloads.get(neighbor_id)
                    if payload and payload.get("entity_type") == target_type:
                        results.append({
                            "fact_id": neighbor_id,
                            "depth": depth + 1,
                            "relation": rel,
                            "path": path,
                        })
                # Traversal continues through non-matching nodes (original
                # "skip but continue traversing" semantics).
            else:
                for neighbor_id, rel, path in candidates:
                    results.append({
                        "fact_id": neighbor_id,
                        "depth": depth + 1,
                        "relation": rel,
                        "path": path,
                    })

            frontier = [(c[0], c[2]) for c in candidates]
            depth += 1

        return results

    def _fetch_payloads(self, point_ids: List[str]) -> Dict[str, dict]:
        """Fetch payloads for many points in one batched Qdrant scroll (H247).

        Replaces the previous one-scroll-per-neighbor N+1 pattern. Uses the
        store's public client + collection (same access path as
        ``find_entities``). Points that no longer exist are simply absent from
        the result; callers must treat a missing entry as "unknown".
        """
        from qdrant_client import models as qm

        if not point_ids:
            return {}
        client = self._graph.store.client
        collection = self._graph.store._collection
        points, _ = client.scroll(
            collection_name=collection,
            scroll_filter=qm.Filter(must=[qm.HasIdCondition(has_id=point_ids)]),
            limit=len(point_ids),
            with_payload=True,
            with_vectors=False,
        )
        return {str(pt.id): (pt.payload or {}) for pt in points}

    def find_entities(
        self,
        entity_type: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """Find all entity-typed facts in the graph.

        Scans Qdrant for points with category="entity".
        """
        from qdrant_client import models as qm
        from qdrant_client.http.exceptions import UnexpectedResponse

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

        except (ConnectionError, TimeoutError, UnexpectedResponse) as exc:
            # Expected Qdrant/network failures — fail-open (empty result) is
            # the documented contract for callers.
            logger.warning("find_entities failed (Qdrant/network): %s", exc)
            return []
        except Exception as exc:
            # H249: unexpected (programming) errors keep their traceback so
            # they are diagnosable instead of silently becoming "empty".
            logger.warning("find_entities failed: %s", exc, exc_info=True)
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

        # Nr 442: no second full Qdrant scroll just to count KG relations — the
        # NetworkX cache already holds every active edge. The cache is only
        # populated after initialize(); an empty graph therefore falls back to
        # the old scroll-based path so behavior is unchanged when it is cold.
        # ``edges(data=True)`` also yields the ``contradicts`` reverse-edges
        # (same edge_id), hence total_relations counts DISTINCT edge_ids, not
        # len(edges). KG_RELATIONS never contains contradicts, so kg_edges is
        # not affected by those duplicates.
        graph = self._graph.graph
        kg_edge_count = 0
        if graph.number_of_nodes():
            edge_ids = set()
            # W30-4: SkillGraph.graph is a MultiDiGraph — with keys=False this
            # still yields (u, v, data), once per parallel edge.
            for _u, _v, data in graph.edges(data=True):
                if data.get("relation") in KG_RELATIONS:
                    kg_edge_count += 1
                edge_id = data.get("edge_id")
                if edge_id:
                    edge_ids.add(edge_id)
            total_relations = len(edge_ids)
        else:
            all_edges = self._graph.list_edges(status="active")
            for edge in all_edges:
                if edge.relation in KG_RELATIONS:
                    kg_edge_count += 1
            total_relations = len(all_edges)

        return {
            **base_stats,
            "kg_edges": kg_edge_count,
            "total_relations": total_relations,
        }
