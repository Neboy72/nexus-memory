from __future__ import annotations

"""SkillGraph — NetworkX-accelerated edge queries over Qdrant-Payloads.

v2.2.0: Qdrant-Payload statt SQLite. Der EdgeStore speichert Edges direkt
in den Qdrant-Point-Payloads. NetworkX bleibt als Read-Layer/Cache.

Every mutation (``add_edge``, ``reject_edge``) goes to Qdrant-Payload first,
then the local NetworkX graph is rebuilt from the store.

Non-goals for v2.0.0 (unchanged):
  - Auto-discovery (v2.1.0)
  - Weighted / ranked path search
  - Graph analytics
"""

import logging
from collections import deque
from typing import Any, Optional

import networkx as nx

from nexus.graph.schema import Edge, EdgeRelation, EdgeStatus
from nexus.graph.store import EdgeStore

_logger = logging.getLogger(__name__)


class SkillGraph:
    """NetworkX-backed query layer over the Qdrant-Payload edge store.

    Usage::

        sg = SkillGraph()
        sg.initialize()           # connects to Qdrant + builds NetworkX cache
        sg.add_edge("fact-a", "fact-b", "supports")
        sg.add_edge("fact-b", "fact-c", "depends_on")
        path = sg.find_path("fact-a", "fact-c")  # BFS
    """

    def __init__(
        self,
        store: EdgeStore | None = None,
        qdrant_url: str | None = None,
        collection: str | None = None,
    ):
        self._store = store or EdgeStore(
            qdrant_url=qdrant_url,
            collection=collection,
        )
        self._graph: nx.DiGraph = nx.DiGraph()

    # ── Store access ────────────────────────────────────────────────────────

    @property
    def store(self) -> EdgeStore:
        return self._store

    # ── Delegated store queries ─────────────────────────────────────────────

    def get_edge(self, edge_id: str) -> Edge | None:
        """Fetch a single edge by ID (delegates to Qdrant store)."""
        return self._store.get_edge(edge_id)

    def list_edges(
        self,
        fact_id: str | None = None,
        relation: str | None = None,
        status: str | None = "active",
    ) -> list[Edge]:
        """List edges from Qdrant-Payload store with optional filters.

        See ``EdgeStore.list_edges()`` for details on bidirectional listing.
        """
        return self._store.list_edges(fact_id=fact_id, relation=relation, status=status)

    # ── Setup ───────────────────────────────────────────────────────────────

    def initialize(self) -> None:
        """Connect to Qdrant and build the NetworkX cache."""
        self._store.initialize()
        self._rebuild()

    def _rebuild(self) -> None:
        """Re-read all active edges from Qdrant into NetworkX."""
        self._graph.clear()
        edges = self._store.list_edges(status="active")

        for edge in edges:
            source = edge.source_fact_id
            target = edge.target_fact_id
            rel = edge.relation

            # Ensure nodes exist
            self._graph.add_node(source)
            self._graph.add_node(target)

            # Directed edge with relation as attribute
            self._graph.add_edge(source, target, relation=rel, edge_id=edge.edge_id)

            # Symmetric contradicts: also add reverse edge
            if rel == EdgeRelation.CONTRADICTS.value:
                self._graph.add_edge(target, source, relation=rel, edge_id=edge.edge_id)

        _logger.debug(
            "SkillGraph rebuilt: %d nodes, %d edges",
            self._graph.order(), self._graph.size(),
        )

    # ── Queries (operate on NetworkX cache) ─────────────────────────────────

    def has_node(self, fact_id: str) -> bool:
        return self._graph.has_node(fact_id)

    def get_neighbors(self, fact_id: str, relation: str | None = None) -> list[dict]:
        """List adjacent facts and edge attributes.

        Alias for ``neighbors()``.
        """
        return self.neighbors(fact_id, relation=relation)

    def neighbors(self, fact_id: str, relation: str | None = None) -> list[dict]:
        """List adjacent facts and edge attributes.

        Args:
            fact_id: The fact to find neighbors for.
            relation: Optional filter (only return edges with this relation).

        Returns:
            List of ``{"fact_id": ..., "relation": ..., "edge_id": ...}``.
        """
        if not self._graph.has_node(fact_id):
            return []

        results = []
        for _, target, data in self._graph.edges(fact_id, data=True):
            rel = data.get("relation", "")
            if relation is None or rel == relation:
                results.append({
                    "fact_id": target,
                    "relation": rel,
                    "edge_id": data.get("edge_id", ""),
                    "direction": "outgoing",
                })

        # Also walk incoming edges for full picture
        for source, _, data in self._graph.in_edges(fact_id, data=True):
            rel = data.get("relation", "")
            if relation is None or rel == relation:
                # Check not already included (symmetric contradicts)
                if not any(r["fact_id"] == source and r["relation"] == rel for r in results):
                    results.append({
                        "fact_id": source,
                        "relation": rel,
                        "edge_id": data.get("edge_id", ""),
                        "direction": "incoming",
                    })

        return results

    def find_path(
        self,
        source_fact_id: str,
        target_fact_id: str,
        max_depth: int = 10,
    ) -> list[dict]:
        """Simple BFS between two facts.

        Returns the shortest directed path as a list of edge-dicts:
        ``[{"source": ..., "target": ..., "relation": ...}, ...]``.
        Returns empty list if no path exists.

        v2.0.0: pure BFS — no ranking, no weights, no heuristics.
        """
        if not self._graph.has_node(source_fact_id):
            return []
        if not self._graph.has_node(target_fact_id):
            return []
        if source_fact_id == target_fact_id:
            return []

        # BFS
        visited: set[str] = {source_fact_id}
        queue: deque[tuple[str, list[dict]]] = deque()
        queue.append((source_fact_id, []))

        while queue and len(visited) < max_depth * 10:
            current, path = queue.popleft()

            for _, neighbor, data in self._graph.edges(current, data=True):
                step = {
                    "source": current,
                    "target": neighbor,
                    "relation": data.get("relation", ""),
                    "edge_id": data.get("edge_id", ""),
                }

                if neighbor == target_fact_id:
                    return path + [step]

                if neighbor not in visited:
                    visited.add(neighbor)
                    new_path = path + [step]
                    if len(new_path) < max_depth:
                        queue.append((neighbor, new_path))

        return []  # No path found

    # ── Mutations (Qdrant-Payload first, NetworkX incremental update) ───────

    def _add_edge_to_graph(self, edge: Edge) -> None:
        """Add a single active edge to the NetworkX cache."""
        self._graph.add_node(edge.source_fact_id)
        self._graph.add_node(edge.target_fact_id)
        rel = edge.relation
        self._graph.add_edge(
            edge.source_fact_id, edge.target_fact_id,
            relation=rel, edge_id=edge.edge_id,
        )
        # Symmetric contradicts
        if rel == EdgeRelation.CONTRADICTS.value:
            self._graph.add_edge(
                edge.target_fact_id, edge.source_fact_id,
                relation=rel, edge_id=edge.edge_id,
            )

    def _remove_edge_from_graph(self, edge: Edge) -> None:
        """Remove a single edge from the NetworkX cache."""
        if not self._graph.has_node(edge.source_fact_id):
            return
        if self._graph.has_edge(edge.source_fact_id, edge.target_fact_id):
            self._graph.remove_edge(edge.source_fact_id, edge.target_fact_id)
        if edge.relation == EdgeRelation.CONTRADICTS.value:
            if self._graph.has_edge(edge.target_fact_id, edge.source_fact_id):
                self._graph.remove_edge(edge.target_fact_id, edge.source_fact_id)

    def add_edge(
        self,
        source_fact_id: str,
        target_fact_id: str,
        relation: str,
        reason: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Edge:
        """Add an edge via Qdrant-Payload, then update NetworkX incrementally."""
        edge = self._store.add_edge(
            source_fact_id=source_fact_id,
            target_fact_id=target_fact_id,
            relation=relation,
            reason=reason,
            metadata=metadata,
        )
        self._add_edge_to_graph(edge)
        return edge

    def reject_edge(self, edge_id: str, reason: str | None = None) -> Edge | None:
        """Reject an edge via Qdrant-Payload, then update NetworkX incrementally."""
        edge = self._store.reject_edge(edge_id, reason=reason)
        if edge:
            self._remove_edge_from_graph(edge)
        return edge

    def deprecate_edge(self, edge_id: str, reason: str | None = None) -> Edge | None:
        """Deprecate an edge via Qdrant-Payload, then update NetworkX incrementally."""
        edge = self._store.deprecate_edge(edge_id, reason=reason)
        if edge:
            self._remove_edge_from_graph(edge)
        return edge

    # ── Stats ───────────────────────────────────────────────────────────────

    def stats(self) -> dict:
        return {
            "nodes": self._graph.order(),
            "edges": self._graph.size(),
            "stored_edges": self._store.count_edges(status="active"),
            "collection": self._store._collection,
        }

    # ── Chain Queries ───────────────────────────────────────────────────

    def get_contradiction_chain(
        self,
        fact_id: str,
        max_depth: int = 3,
    ) -> list[dict]:
        """Find all facts that contradict *fact_id*, directly or transitively.

        Traverses ``contradicts`` edges outward.

        Returns list of ``{"fact_id", "path", "relation", "edge_id"}``.
        """
        if not self._graph.has_node(fact_id):
            return []

        results: list[dict] = []
        visited: set[str] = set()

        def _dfs(current: str, path: list[str], depth: int) -> None:
            if depth > max_depth:
                return
            for _, neighbor, data in self._graph.edges(current, data=True):
                if data.get("relation") == EdgeRelation.CONTRADICTS.value and neighbor not in visited:
                    visited.add(neighbor)
                    new_path = path + [neighbor]
                    results.append({
                        "fact_id": neighbor,
                        "path": list(new_path),
                        "relation": EdgeRelation.CONTRADICTS.value,
                        "edge_id": data.get("edge_id", ""),
                    })
                    _dfs(neighbor, new_path, depth + 1)

        visited.add(fact_id)
        _dfs(fact_id, [fact_id], 1)
        return results

    def get_support_chain(
        self,
        fact_id: str,
        max_depth: int = 3,
    ) -> list[dict]:
        """Find supporting evidence chain for a fact.

        Traverses ``supports`` edges outward from *fact_id*.

        Returns list of ``{"fact_id", "path", "relation", "edge_id"}``.
        """
        if not self._graph.has_node(fact_id):
            return []

        results: list[dict] = []

        def _dfs(current: str, path: list[str], depth: int) -> None:
            if depth > max_depth:
                return
            for _, neighbor, data in self._graph.edges(current, data=True):
                if data.get("relation") == EdgeRelation.SUPPORTS.value and neighbor not in {n for n in path}:
                    new_path = path + [neighbor]
                    results.append({
                        "fact_id": neighbor,
                        "path": list(new_path),
                        "relation": EdgeRelation.SUPPORTS.value,
                        "edge_id": data.get("edge_id", ""),
                    })
                    _dfs(neighbor, new_path, depth + 1)

        _dfs(fact_id, [fact_id], 1)
        return results
