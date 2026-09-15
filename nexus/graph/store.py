"""EdgeStore — Qdrant-Payload-backed CRUD for SkillGraph edges.

v2.2.0: SQLite removed. Edges live directly in Qdrant point payloads
of the canonical facts (Collection ``nexus``).

Each fact point carries an ``edges`` field:
  ``edges: [{"edge_id", "target_fact_id", "relation", "status", ...}, ...]``

Queries:
  - Outgoing edges: direkt aus dem Payload des Source-Points.
  - Incoming edges: Qdrant Nested-Filter auf ``edges[].target_fact_id``.
  - Mutations: ``set_payload()`` auf den Source-Point (merged ins bestehende Payload).

Single Source of Truth: Qdrant. Kein SQLite, kein Sync.
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from qdrant_client import QdrantClient, models

from nexus.graph.schema import (
    Edge,
    EdgeRelation,
    EdgeStatus,
    EDGES_PAYLOAD_KEY,
)

_logger = logging.getLogger(__name__)

from nexus.config import get_collection

DEFAULT_QDRANT_URL = "http://localhost:6333"
DEFAULT_COLLECTION: str = get_collection()
MAX_PAGE_SIZE = 1000


def _default_vector_size() -> int:
    """Resolve the canonical embedding dimension for this collection.

    Mirrors ``nexus.staging._detect_vector_size()`` (the canonical-collection
    bootstrap) so an edge collection created here can never disagree with the
    size used for the real embeddings. Falls back to 1024 (Voyage) when the
    provider cannot be detected, matching ``nexus/events.py``.
    """
    try:
        from nexus.staging import _detect_vector_size
        return int(_detect_vector_size())
    except Exception:
        return 1024


class EdgeStoreError(Exception):
    """Base exception for EdgeStore operations."""


class DuplicateEdgeError(EdgeStoreError):
    """Raised when trying to add a duplicate active edge."""


class EdgeNotFoundError(EdgeStoreError):
    """Raised when an edge is not found."""


class InvalidRelationError(EdgeStoreError, ValueError):
    """Raised for invalid relation strings."""


class EdgeStore:
    """Qdrant-Payload-persisted edge store — the single Source of Truth.

    Usage::

        store = EdgeStore()
        store.initialize()
        edge = store.add_edge("fact-a", "fact-b", "supports", reason="confirmed")
        edge = store.get_edge(edge.edge_id)
        edges = store.list_edges("fact-a")
        store.reject_edge(edge.edge_id, reason="false positive")
    """

    def __init__(
        self,
        qdrant_url: str | None = None,
        collection: str | None = None,
        client: QdrantClient | None = None,
    ):
        self._qdrant_url = qdrant_url or DEFAULT_QDRANT_URL
        self._collection = collection or DEFAULT_COLLECTION
        self._client: QdrantClient | None = client
        self._valid_relations = {e.value for e in EdgeRelation}
        # Serializes read-modify-write cycles on the ``edges`` payload array.
        # Threads sharing ONE EdgeStore no longer lose concurrent appends to
        # the same source fact. Concurrent processes are NOT covered — Qdrant
        # has no conditional-set / CAS primitive, so cross-process writers
        # remain last-write-wins by design (single-writer assumption).
        self._edges_lock = threading.Lock()

    # ── Connection ──────────────────────────────────────────────────────────

    @property
    def client(self) -> QdrantClient:
        if self._client is None:
            self._client = QdrantClient(url=self._qdrant_url)
        return self._client

    @property
    def collection(self) -> str:
        """Name of the Qdrant collection backing this store."""
        return self._collection

    @client.setter
    def client(self, value: QdrantClient) -> None:
        self._client = value

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    # ── Validation ──────────────────────────────────────────────────────────

    def _validate_relation(self, relation: str) -> None:
        if relation not in self._valid_relations:
            raise InvalidRelationError(
                f"Invalid relation '{relation}'. "
                f"Must be one of: {', '.join(sorted(self._valid_relations))}"
            )

    # ── Initialization ──────────────────────────────────────────────────────

    def initialize(self) -> bool:
        """Verify the Qdrant connection and ensure the collection exists.

        H225: this used to only *warn* that the collection "will be created on
        first write" — while nothing in this module ever created it, so a
        caller believed ``initialize()`` succeeded and the first
        ``set_payload``/``scroll`` failed with a Qdrant "collection not
        found". The collection is now created here (idempotent) with the same
        vector config as the canonical bootstrap path.

        Returns:
            ``True`` if the collection exists / was created, ``False`` if it
            could not be created.

        Raises:
            EdgeStoreError: If Qdrant is unreachable.
        """
        try:
            exists = self.client.collection_exists(self._collection)
        except Exception as e:
            raise EdgeStoreError(f"Failed to connect to Qdrant: {e}") from e

        if not exists:
            size = _default_vector_size()
            try:
                self.client.create_collection(
                    collection_name=self._collection,
                    vectors_config=models.VectorParams(
                        size=size, distance=models.Distance.COSINE,
                    ),
                )
                _logger.info(
                    "Created Qdrant collection '%s' (%dd Cosine)",
                    self._collection, size,
                )
            except Exception as e:
                _logger.error(
                    "Failed to create collection '%s': %s", self._collection, e,
                )
                return False

        _logger.info(
            "EdgeStore initialized (Qdrant=%s, collection=%s)",
            self._qdrant_url, self._collection,
        )
        return True

    # ── Payload helpers ─────────────────────────────────────────────────────

    @staticmethod
    def _get_edges_from_payload(payload: dict) -> list[dict]:
        """Extract the edges array from a point payload."""
        return payload.get(EDGES_PAYLOAD_KEY, []) or []

    @staticmethod
    def _edge_to_entry(edge: Edge) -> dict:
        """Serialize an Edge to a Qdrant-payload dict entry."""
        return edge.to_payload_entry()

    def _write_edges_back(
        self,
        point_id: str,
        edges: list[dict],
    ) -> None:
        """Replace the edges array on a point via set_payload."""
        self.client.set_payload(
            collection_name=self._collection,
            payload={EDGES_PAYLOAD_KEY: edges},
            points=[point_id],
        )

    def scroll_point(self, point_id: str) -> dict | None:
        """Public accessor for :meth:`_scroll_point`.

        Delegation wrapper so external callers (e.g. ``SkillGraph.get_point``)
        can fetch a point payload without touching the private API.
        """
        return self._scroll_point(point_id)

    def _scroll_point(
        self,
        point_id: str,
    ) -> dict | None:
        """Retrieve a single point by its ID (UUID string)."""
        points, _ = self.client.scroll(
            collection_name=self._collection,
            limit=1,
            with_payload=True,
            with_vectors=False,
            scroll_filter=models.Filter(
                must=[models.HasIdCondition(has_id=[point_id])],
            ),
        )
        if points:
            return {
                "id": points[0].id,
                "payload": points[0].payload or {},
            }
        return None

    # ── CRUD ────────────────────────────────────────────────────────────────

    def add_edge(
        self,
        source_fact_id: str,
        target_fact_id: str,
        relation: str,
        reason: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Edge:
        """Create a new active edge between two facts.

        Args:
            source_fact_id: From-fact (Qdrant Point ID).
            target_fact_id: To-fact.
            relation: One of ``EdgeRelation`` values.
            reason: Optional human-readable explanation.
            metadata: Optional dict (stored as Qdrant-native JSON).

        Returns:
            The newly created ``Edge``.

        Raises:
            InvalidRelationError: If the relation string is not valid.
            DuplicateEdgeError: If a duplicate active edge already exists.
            EdgeStoreError: If the source point does not exist.
        """
        self._validate_relation(relation)

        with self._edges_lock:
            # Check source point exists
            point = self._scroll_point(source_fact_id)
            if point is None:
                raise EdgeStoreError(
                    f"Source fact '{source_fact_id}' not found in Qdrant"
                )

            # Check for duplicates
            existing_edges = self._get_edges_from_payload(point["payload"])
            for entry in existing_edges:
                if (
                    entry.get("target_fact_id") == target_fact_id
                    and entry.get("relation") == relation
                    and entry.get("status") == EdgeStatus.ACTIVE.value
                ):
                    raise DuplicateEdgeError(
                        f"Active edge already exists: "
                        f"{source_fact_id} --[{relation}]--> {target_fact_id}"
                    )

            # Create edge
            edge = Edge.new(
                source_fact_id=source_fact_id,
                target_fact_id=target_fact_id,
                relation=relation,
                reason=reason,
                metadata=metadata,
            )

            # Append to payload edges array
            entry = self._edge_to_entry(edge)
            existing_edges.append(entry)
            self._write_edges_back(source_fact_id, existing_edges)

        _logger.info(
            "Edge added: %s (%s) --[%s]--> %s",
            source_fact_id, relation, edge.edge_id, target_fact_id,
        )
        return edge

    def add_proposed_edge(
        self,
        source_fact_id: str,
        target_fact_id: str,
        relation: str,
        reason: str | None = None,
        confidence: float | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Edge:
        """Create a proposed edge (auto-discovered, needs confirmation).

        ``confidence`` is stored in ``metadata['confidence']``.

        Raises:
            DuplicateEdgeError: If ANY edge (any status) already exists
                between source-target-relation.
        """
        self._validate_relation(relation)

        with self._edges_lock:
            point = self._scroll_point(source_fact_id)
            if point is None:
                raise EdgeStoreError(
                    f"Source fact '{source_fact_id}' not found in Qdrant"
                )

            # Check for ANY existing edge (dedup: don't rediscover)
            existing_edges = self._get_edges_from_payload(point["payload"])
            for entry in existing_edges:
                if (
                    entry.get("target_fact_id") == target_fact_id
                    and entry.get("relation") == relation
                ):
                    raise DuplicateEdgeError(
                        f"Edge already exists (any status): "
                        f"{source_fact_id} --[{relation}]--> {target_fact_id}"
                    )

            meta = dict(metadata or {})
            if confidence is not None:
                meta["confidence"] = confidence

            now = datetime.now(timezone.utc).isoformat()
            edge_id = str(uuid.uuid4())

            entry = {
                "edge_id": edge_id,
                "target_fact_id": target_fact_id,
                "relation": relation,
                "status": EdgeStatus.PROPOSED.value,
                "created_at": now,
                "updated_at": now,
                "deprecated_at": None,
                "reason": reason,
                "metadata": meta,
            }

            existing_edges.append(entry)
            self._write_edges_back(source_fact_id, existing_edges)

        edge = Edge(
            edge_id=edge_id,
            source_fact_id=source_fact_id,
            target_fact_id=target_fact_id,
            relation=relation,
            status=EdgeStatus.PROPOSED.value,
            created_at=now,
            updated_at=now,
            reason=reason,
            metadata=meta,
        )

        _logger.info(
            "Proposed edge added: %s (%s) --[%s]--> %s (confidence=%s)",
            source_fact_id, relation, edge_id, target_fact_id, confidence,
        )
        return edge

    def get_edge(self, edge_id: str) -> Edge | None:
        """Fetch a single edge by ID, scanning all points with edges.

        Returns ``None`` if not found.
        """
        # We need to find which point holds this edge
        # Strategy: scroll through the collection looking for edge_id
        # Limited: Qdrant has no direct nested-scroll-filter on edge_id
        next_offset: Any = None
        while True:
            points, next_offset = self.client.scroll(
                collection_name=self._collection,
                limit=MAX_PAGE_SIZE,
                offset=next_offset or None,
                with_payload=True,
                with_vectors=False,
            )
            for pt in points:
                edges = self._get_edges_from_payload(pt.payload or {})
                for entry in edges:
                    if entry.get("edge_id") == edge_id:
                        return Edge.from_payload_entry(
                            entry,
                            source_fact_id=str(pt.id),
                        )
            if next_offset is None:
                break

        return None

    def list_edges(
        self,
        fact_id: str | None = None,
        relation: str | None = None,
        status: str | None = "active",
    ) -> list[Edge]:
        """List edges, optionally filtered.

        If ``fact_id`` is set, returns edges where the fact is EITHER
        source OR target (bidirectional).

        ``contradicts`` edges are inherently symmetric via the
        bidirectional listing.
        """
        if fact_id is None:
            return self._list_all_edges(relation=relation, status=status)

        # 1. Outgoing: edges from this fact
        outgoing_edges: list[Edge] = []
        point = self._scroll_point(fact_id)
        if point:
            entries = self._get_edges_from_payload(point["payload"])
            for entry in entries:
                edge = Edge.from_payload_entry(entry, source_fact_id=fact_id)
                if self._matches_filters(edge, relation=relation, status=status):
                    outgoing_edges.append(edge)

        # 2. Incoming: edges where target_fact_id == fact_id
        incoming_edges: list[Edge] = self._find_incoming_edges(
            target_fact_id=fact_id,
            relation=relation,
            status=status,
        )

        # Merge outgoing first, then incoming — de-duplicated by edge_id.
        # Nr 439: a SELF-EDGE (source == target) matches BOTH queries, so the
        # old "no dedup needed" claim returned it twice. dict.setdefault keeps
        # the first occurrence (outgoing) and preserves order.
        merged: dict[str, Edge] = {}
        for edge in outgoing_edges + incoming_edges:
            merged.setdefault(edge.edge_id, edge)
        return list(merged.values())

    def _list_all_edges(
        self,
        relation: str | None = None,
        status: str | None = "active",
    ) -> list[Edge]:
        """Scan all points and collect matching edges."""
        results: list[Edge] = []
        next_offset: Any = None
        while True:
            points, next_offset = self.client.scroll(
                collection_name=self._collection,
                limit=MAX_PAGE_SIZE,
                offset=next_offset or None,
                with_payload=True,
                with_vectors=False,
            )
            for pt in points:
                edges = self._get_edges_from_payload(pt.payload or {})
                for entry in edges:
                    edge = Edge.from_payload_entry(
                        entry, source_fact_id=str(pt.id),
                    )
                    if self._matches_filters(edge, relation=relation, status=status):
                        results.append(edge)
            if next_offset is None:
                break
        return results

    def _find_incoming_edges(
        self,
        target_fact_id: str,
        relation: str | None = None,
        status: str | None = "active",
    ) -> list[Edge]:
        """Find edges where target_fact_id matches, using Qdrant filter."""
        results: list[Edge] = []

        # Qdrant nested filter on edges[].target_fact_id
        must_conditions: list[models.Condition] = [
            models.FieldCondition(
                key=f"{EDGES_PAYLOAD_KEY}[].target_fact_id",
                match=models.MatchValue(value=target_fact_id),
            ),
        ]

        filter_ = models.Filter(must=must_conditions)
        next_offset: Any = None
        while True:
            points, next_offset = self.client.scroll(
                collection_name=self._collection,
                limit=MAX_PAGE_SIZE,
                offset=next_offset or None,
                with_payload=True,
                with_vectors=False,
                scroll_filter=filter_,
            )
            for pt in points:
                edges = self._get_edges_from_payload(pt.payload or {})
                source_id = str(pt.id)
                for entry in edges:
                    if entry.get("target_fact_id") != target_fact_id:
                        continue
                    edge = Edge.from_payload_entry(entry, source_fact_id=source_id)
                    if self._matches_filters(edge, relation=relation, status=status):
                        results.append(edge)
            if next_offset is None:
                break

        return results

    @staticmethod
    def _matches_filters(
        edge: Edge,
        relation: str | None = None,
        status: str | None = None,
    ) -> bool:
        """Check if an edge matches optional relation/status filters."""
        if relation is not None and edge.relation != relation:
            return False
        if status is not None and edge.status != status:
            return False
        return True

    # ── Edge existence checks ───────────────────────────────────────────────

    def has_active_edge(
        self,
        source_fact_id: str,
        target_fact_id: str,
        relation: str,
    ) -> bool:
        """Check if an active edge already exists between these facts."""
        point = self._scroll_point(source_fact_id)
        if point is None:
            return False
        edges = self._get_edges_from_payload(point["payload"])
        return any(
            e.get("target_fact_id") == target_fact_id
            and e.get("relation") == relation
            and e.get("status") == EdgeStatus.ACTIVE.value
            for e in edges
        )

    def has_any_edge(
        self,
        source_fact_id: str,
        target_fact_id: str,
        relation: str,
    ) -> bool:
        """Check if ANY edge exists (any status) between these facts.

        Used by dedup to avoid re-discovering already-known relations.
        """
        point = self._scroll_point(source_fact_id)
        if point is None:
            return False
        edges = self._get_edges_from_payload(point["payload"])
        return any(
            e.get("target_fact_id") == target_fact_id
            and e.get("relation") == relation
            for e in edges
        )

    # ── Lifecycle transitions ───────────────────────────────────────────────

    def _update_edge_status(
        self,
        edge_id: str,
        new_status: str,
        reason: str | None = None,
        from_statuses: set[str] | None = None,
    ) -> Edge | None:
        """Find an edge by ID and update its status.

        Args:
            edge_id: The edge to transition.
            new_status: Target status value.
            reason: Optional replacement reason.
            from_statuses: Only transition an edge whose CURRENT status is in
                this set. ``None`` accepts any current status.

        Returns ``None`` if the edge was not found, or if it exists but its
        current status is not an allowed source status for this transition.
        """
        with self._edges_lock:
            now = datetime.now(timezone.utc).isoformat()

            # Scroll all points to find the edge
            next_offset: Any = None
            while True:
                points, next_offset = self.client.scroll(
                    collection_name=self._collection,
                    limit=MAX_PAGE_SIZE,
                    offset=next_offset or None,
                    with_payload=True,
                    with_vectors=False,
                )
                for pt in points:
                    edges = self._get_edges_from_payload(pt.payload or {})
                    for i, entry in enumerate(edges):
                        if entry.get("edge_id") != edge_id:
                            continue
                        if from_statuses is not None and entry.get("status") not in from_statuses:
                            # Wrong source status — not this kind of edge.
                            return None
                        if entry.get("status") == new_status:
                            # Already in target status — no-op
                            return Edge.from_payload_entry(
                                entry, source_fact_id=str(pt.id),
                            )

                        # Update
                        edges[i]["status"] = new_status
                        edges[i]["updated_at"] = now
                        edges[i]["deprecated_at"] = now
                        if reason:
                            edges[i]["reason"] = reason
                        self._write_edges_back(str(pt.id), edges)

                        entry["source_fact_id"] = str(pt.id)
                        return Edge.from_dict(entry)

                if next_offset is None:
                    break
            return None

    def reject_edge(self, edge_id: str, reason: str | None = None) -> Edge | None:
        """Reject (soft-delete) an active/proposed edge.

        The edge stays in the payload (append-only principle).

        Returns ``None`` if no active/proposed edge was found with that ID.
        """
        return self._update_edge_status(
            edge_id,
            EdgeStatus.REJECTED.value,
            reason=reason,
            from_statuses={EdgeStatus.ACTIVE.value, EdgeStatus.PROPOSED.value},
        )

    def deprecate_edge(self, edge_id: str, reason: str | None = None) -> Edge | None:
        """Deprecate an active edge (softer than reject).

        Returns ``None`` if no active edge was found with that ID.
        """
        return self._update_edge_status(
            edge_id,
            EdgeStatus.DEPRECATED.value,
            reason=reason,
            from_statuses={EdgeStatus.ACTIVE.value},
        )

    def promote_edge(self, edge_id: str, reason: str | None = None) -> Edge | None:
        """Promote a proposed edge to active status.

        Returns ``None`` if no proposed edge was found with that ID.
        Raises ``DuplicateEdgeError`` if promoting would create a duplicate.
        """
        with self._edges_lock:
            now = datetime.now(timezone.utc).isoformat()

            next_offset: Any = None
            while True:
                points, next_offset = self.client.scroll(
                    collection_name=self._collection,
                    limit=MAX_PAGE_SIZE,
                    offset=next_offset or None,
                    with_payload=True,
                    with_vectors=False,
                )
                for pt in points:
                    edges = self._get_edges_from_payload(pt.payload or {})
                    for i, entry in enumerate(edges):
                        if entry.get("edge_id") != edge_id:
                            continue
                        if entry.get("status") != EdgeStatus.PROPOSED.value:
                            # Not a proposed edge — contract says return None.
                            return None

                        # Check for duplicate active before promoting
                        for j, other in enumerate(edges):
                            if i == j:
                                continue
                            if (
                                other.get("target_fact_id") == entry.get("target_fact_id")
                                and other.get("relation") == entry.get("relation")
                                and other.get("status") == EdgeStatus.ACTIVE.value
                            ):
                                raise DuplicateEdgeError(
                                    f"Cannot promote — active edge already exists: "
                                    f"{pt.id} --[{entry['relation']}]--> {entry['target_fact_id']}"
                                )

                        edges[i]["status"] = EdgeStatus.ACTIVE.value
                        edges[i]["updated_at"] = now
                        if reason:
                            edges[i]["reason"] = reason
                        self._write_edges_back(str(pt.id), edges)

                        entry["source_fact_id"] = str(pt.id)
                        entry["status"] = EdgeStatus.ACTIVE.value
                        _logger.info("Edge promoted to active: %s", edge_id)
                        return Edge.from_dict(entry)

                if next_offset is None:
                    break
            return None

    # ── Count / Stats ───────────────────────────────────────────────────────

    def count_edges(self, status: str | None = None) -> int:
        """Count edges, optionally filtered by status.

        Note: This scrolls through the collection — use sparingly.
        """
        count = 0
        next_offset: Any = None
        while True:
            points, next_offset = self.client.scroll(
                collection_name=self._collection,
                limit=MAX_PAGE_SIZE,
                offset=next_offset or None,
                with_payload=True,
                with_vectors=False,
            )
            for pt in points:
                edges = self._get_edges_from_payload(pt.payload or {})
                for entry in edges:
                    if status is None or entry.get("status") == status:
                        count += 1
            if next_offset is None:
                break
        return count

    # ── Legacy backwards compat ─────────────────────────────────────────────

    @property
    def conn(self):
        """Legacy: raises a clear error instead of silently breaking."""
        raise EdgeStoreError(
            "EdgeStore no longer uses SQLite. "
            "Use EdgeStore.client for Qdrant access."
        )
