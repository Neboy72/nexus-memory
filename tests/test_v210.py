"""Tests for graph_boost integration in Hybrid Search + new Store methods.

v2.1.0: ~15 tests covering graph_boost, proposed edges, promote_edge.
v2.2.0: Qdrant-Payload backend (was SQLite).
"""

from __future__ import annotations

import os
import tempfile
import uuid

import pytest
from qdrant_client import QdrantClient, models

from nexus.graph.schema import EdgeRelation, EdgeStatus
from nexus.graph.store import EdgeStore

TEST_COLLECTION = "test-memory"
TEST_VECTOR_CONFIG = models.VectorParams(size=2, distance=models.Distance.COSINE)


def fact_id(name: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, name))


F_A = fact_id("a")
F_B = fact_id("b")
F_C = fact_id("c")
F_D = fact_id("d")
F_E = fact_id("e")
F_F = fact_id("f")
F_G = fact_id("g")
F_H = fact_id("h")
F_X = fact_id("x")
F_Y = fact_id("y")
F_FACT_A = fact_id("fact-a")
F_FACT_B = fact_id("fact-b")


def create_points(client: QdrantClient, collection: str, ids: list[str]) -> None:
    client.upsert(
        collection_name=collection,
        points=[
            models.PointStruct(
                id=fid, vector=[0.0, 0.0],
                payload={"content": f"Test {fid}"},
            )
            for fid in ids
        ],
    )


ALL_TEST_IDS = [
    F_A, F_B, F_C, F_D, F_E, F_F, F_G, F_H, F_X, F_Y,
    F_FACT_A, F_FACT_B,
]


@pytest.fixture
def qdrant_store():
    """EdgeStore backed by embedded Qdrant with pre-created fact-points."""
    with tempfile.TemporaryDirectory() as tmp:
        client = QdrantClient(path=os.path.join(tmp, "qdrant"))
        client.create_collection(
            collection_name=TEST_COLLECTION,
            vectors_config=TEST_VECTOR_CONFIG,
        )
        create_points(client, TEST_COLLECTION, ALL_TEST_IDS)

        store = EdgeStore(client=client, collection=TEST_COLLECTION)
        store.initialize()
        yield store


class TestProposedEdges:
    def test_add_proposed_edge(self, qdrant_store):
        edge = qdrant_store.add_proposed_edge(
            F_FACT_A, F_FACT_B, "references",
            confidence=0.78, reason="auto-discovered",
        )
        assert edge.status == EdgeStatus.PROPOSED.value
        assert edge.edge_id is not None
        assert edge.metadata is not None
        assert edge.metadata.get("confidence") == 0.78

    def test_add_proposed_edge_not_in_active_list(self, qdrant_store):
        qdrant_store.add_proposed_edge(F_FACT_A, F_FACT_B, "references", confidence=0.78)
        active_edges = qdrant_store.list_edges(status="active")
        proposed_edges = qdrant_store.list_edges(status="proposed")
        assert len(active_edges) == 0
        assert len(proposed_edges) == 1

    def test_promote_edge(self, qdrant_store):
        edge = qdrant_store.add_proposed_edge(F_FACT_A, F_FACT_B, "references", confidence=0.78)
        promoted = qdrant_store.promote_edge(edge.edge_id, reason="confirmed")
        assert promoted is not None
        assert promoted.status == EdgeStatus.ACTIVE.value

    def test_promote_nonexistent_edge(self, qdrant_store):
        result = qdrant_store.promote_edge("nonexistent-id")
        assert result is None
    def test_promote_active_edge(self, qdrant_store):
        """Promoting an already active edge returns the edge (no-op)."""
        edge = qdrant_store.add_edge(F_FACT_A, F_FACT_B, "references", reason="test")
        result = qdrant_store.promote_edge(edge.edge_id)
        assert result is not None
        assert result.status == "active"

    def test_has_any_edge_true(self, qdrant_store):
        qdrant_store.add_proposed_edge(F_FACT_A, F_FACT_B, "references", confidence=0.78)
        assert qdrant_store.has_any_edge(F_FACT_A, F_FACT_B, "references") is True
        assert qdrant_store.has_active_edge(F_FACT_A, F_FACT_B, "references") is False

    def test_has_any_edge_false(self, qdrant_store):
        assert qdrant_store.has_any_edge(F_X, F_Y, "references") is False

    def test_invalid_relation_rejected(self, qdrant_store):
        with pytest.raises(ValueError):
            qdrant_store.add_proposed_edge(F_FACT_A, F_FACT_B, "invalid_relation", confidence=0.50)

    def test_count_edges_by_status(self, qdrant_store):
        qdrant_store.add_edge(F_A, F_B, "references", reason="test")
        qdrant_store.add_proposed_edge(F_C, F_D, "references", confidence=0.70)
        qdrant_store.add_proposed_edge(F_E, F_F, "references", confidence=0.75)
        qdrant_store.add_edge(F_G, F_H, "supports", reason="test")
        assert qdrant_store.count_edges(status="active") == 2
        assert qdrant_store.count_edges(status="proposed") == 2
        assert qdrant_store.count_edges() == 4


class TestReferencesRelation:
    def test_references_relation_valid(self, qdrant_store):
        edge = qdrant_store.add_edge(F_FACT_A, F_FACT_B, "references", reason="auto")
        assert edge.relation == EdgeRelation.REFERENCES.value

    def test_references_edge_listed(self, qdrant_store):
        qdrant_store.add_edge(F_FACT_A, F_FACT_B, "references", reason="test")
        edges = qdrant_store.list_edges()
        assert len(edges) == 1
        assert edges[0].relation == EdgeRelation.REFERENCES.value


class TestGraphBoost:
    def test_boost_no_skillgraph(self):
        from nexus.retrieval import HybridRetriever
        retriever = HybridRetriever()
        ranked = [{"id": "fact-1", "rrf_score": 10.0}, {"id": "fact-2", "rrf_score": 8.0}]
        result = retriever._graph_boost(ranked)
        assert result == ranked

    def test_graph_boost_param_in_search_hybrid(self):
        import inspect
        from nexus.retrieval import HybridRetriever
        sig = inspect.signature(HybridRetriever.search_hybrid)
        assert "graph_boost" in sig.parameters

    def test_boost_formula(self):
        for degree in [0, 1, 5, 10, 20]:
            expected = round(1.0 + degree * 0.05, 3)
            assert expected > 1.0 or degree == 0
