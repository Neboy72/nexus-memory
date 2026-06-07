"""Tests for nexus.graph — SkillGraph v2.2.0 (Qdrant-Payload backend).

Each test requires fact-points to exist in Qdrant first (EdgeStore appends
edges to existing point payloads). The ``store_with_points`` fixture creates
a set of fact-points used across all tests.

Note: Qdrant local mode requires Point IDs to be valid UUIDs (32 hex chars)
or integers. All helper IDs in this file use 32-char zero-padded strings.
"""

import os
import tempfile
import uuid

import pytest
from qdrant_client import QdrantClient, models

from nexus.graph import Edge, EdgeRelation, EdgeStatus, EdgeStore, SkillGraph

# Collection name used in all test fixtures
TEST_COLLECTION = "test-memory"
# Minimal vector config for test points (Qdrant requires vectors)
TEST_VECTOR_SIZE = 2
TEST_VECTOR_CONFIG = models.VectorParams(
    size=TEST_VECTOR_SIZE,
    distance=models.Distance.COSINE,
)


def fact_id(name: str) -> str:
    """Convert a short name to a deterministic UUID string.

    Uses uuid5 (SHA-1 based) on NAMESPACE_DNS + name.
    """
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, name))


# Common fact IDs used across all tests
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
F_Z = fact_id("z")
F_F1 = fact_id("f1")
F_F2 = fact_id("f2")
F_FACT_A = fact_id("fact-a")
F_FACT_B = fact_id("fact-b")
F_FACT_C = fact_id("fact-c")
F_FACT_D = fact_id("fact-d")
F_FACT_E = fact_id("fact-e")
F_FACT_F = fact_id("fact-f")
F_FACT_G = fact_id("fact-g")
F_FACT_H = fact_id("fact-h")
F_FACT_X = fact_id("fact-x")
F_FACT_Y = fact_id("fact-y")
F_FACT_Z = fact_id("fact-z")


def create_point(client: QdrantClient, collection: str, pid: str) -> None:
    """Upsert a minimal fact-point into Qdrant for testing."""
    client.upsert(
        collection_name=collection,
        points=[
            models.PointStruct(
                id=pid,
                vector=[0.0] * TEST_VECTOR_SIZE,
                payload={"content": f"Test fact {pid}"},
            )
        ],
    )


def create_fact_points(client: QdrantClient, collection: str, ids: list[str]) -> None:
    """Create multiple fact-points at once."""
    client.upsert(
        collection_name=collection,
        points=[
            models.PointStruct(
                id=pid,
                vector=[0.0] * TEST_VECTOR_SIZE,
                payload={"content": f"Test fact {pid}"},
            )
            for pid in ids
        ],
    )


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def tmp_qdrant():
    """Temporary directory for Qdrant embedded storage."""
    with tempfile.TemporaryDirectory() as tmp:
        yield tmp


@pytest.fixture
def client(tmp_qdrant):
    """Qdrant embedded client backed by temp dir."""
    return QdrantClient(path=os.path.join(tmp_qdrant, "qdrant"))


@pytest.fixture
def store(client):
    """EdgeStore with empty Qdrant (no fact-points)."""
    client.create_collection(
        collection_name=TEST_COLLECTION,
        vectors_config=TEST_VECTOR_CONFIG,
    )
    s = EdgeStore(client=client, collection=TEST_COLLECTION)
    s.initialize()
    return s


@pytest.fixture
def store_with_points(client):
    """EdgeStore with pre-created fact-points."""
    client.create_collection(
        collection_name=TEST_COLLECTION,
        vectors_config=TEST_VECTOR_CONFIG,
    )
    s = EdgeStore(client=client, collection=TEST_COLLECTION)
    s.initialize()

    create_fact_points(client, TEST_COLLECTION, [
        F_F1, F_F2, F_A, F_B, F_C, F_D, F_E, F_F, F_G, F_H,
        F_X, F_Y, F_Z, F_FACT_A, F_FACT_B, F_FACT_C, F_FACT_D,
        F_FACT_E, F_FACT_F, F_FACT_G, F_FACT_H, F_FACT_X, F_FACT_Y,
        F_FACT_Z,
    ])
    return s


@pytest.fixture
def graph(store_with_points):
    """SkillGraph backed by pre-populated Qdrant."""
    g = SkillGraph(store=store_with_points)
    g.initialize()
    return g


@pytest.fixture
def populated_graph(graph):
    """Graph with sample edges for query tests."""
    g = graph
    g.add_edge(F_FACT_A, F_FACT_B, "supersedes", reason="v1.1")
    g.add_edge(F_FACT_B, F_FACT_C, "supersedes", reason="v2.0")
    g.add_edge(F_FACT_A, F_FACT_D, "contradicts", reason="conflict")
    g.add_edge(F_FACT_E, F_FACT_A, "contradicts", reason="audit")
    g.add_edge(F_FACT_F, F_FACT_C, "supports", reason="evidence")
    g.add_edge(F_FACT_F, F_FACT_G, "supports", reason="evidence2")
    g.add_edge(F_FACT_D, F_FACT_E, "alternative_to")
    g.add_edge(F_FACT_G, F_FACT_H, "depends_on")
    return g


# ── Schema & Edge ──────────────────────────────────────────────────────────


class TestEdge:
    def test_new_creates_active_edge(self):
        e = Edge.new(F_F1, F_F2, EdgeRelation.SUPPORTS.value)
        assert e.source_fact_id == F_F1
        assert e.target_fact_id == F_F2
        assert e.relation == "supports"
        assert e.status == "active"
        assert e.edge_id is not None

    def test_to_dict(self):
        e = Edge.new(F_A, F_B, "contradicts", reason="test")
        d = e.to_dict()
        assert d["source_fact_id"] == F_A
        assert d["relation"] == "contradicts"
        assert d["reason"] == "test"

    def test_from_dict(self):
        d = {
            "edge_id": "abc123",
            "source_fact_id": "s",
            "target_fact_id": "t",
            "relation": "supports",
            "status": "active",
            "created_at": "2026-01-01",
            "updated_at": "2026-01-01",
        }
        e = Edge.from_dict(d)
        assert e.edge_id == "abc123"
        assert e.relation == "supports"

    def test_to_payload_entry_roundtrip(self):
        e = Edge.new(F_F1, F_F2, "contradicts", reason="conflict",
                      metadata={"source": "test"})
        entry = e.to_payload_entry()
        assert "source_fact_id" not in entry  # not in payload entry
        assert entry["target_fact_id"] == F_F2
        assert entry["metadata"]["source"] == "test"

        restored = Edge.from_payload_entry(entry, source_fact_id=F_F1)
        assert restored.source_fact_id == F_F1
        assert restored.relation == "contradicts"
        assert restored.metadata["source"] == "test"


# ── EdgeStore CRUD ─────────────────────────────────────────────────────────


class TestEdgeStore:
    def test_add_edge(self, store_with_points, client):
        s = store_with_points
        e = s.add_edge(F_FACT_A, F_FACT_B, "contradicts", reason="conflict")
        assert e.edge_id is not None
        assert e.relation == "contradicts"
        assert e.reason == "conflict"

        # Verify it's actually in Qdrant
        point = s._scroll_point(F_FACT_A)
        assert point is not None
        edges = s._get_edges_from_payload(point["payload"])
        assert len(edges) == 1

    def test_add_edge_source_not_found(self, store):
        """Edge to non-existent fact raises error."""
        with pytest.raises(Exception, match="not found"):
            store.add_edge("does-not-exist", F_B, "supports")

    def test_add_edge_invalid_relation(self, store_with_points):
        with pytest.raises(ValueError, match="Invalid relation"):
            store_with_points.add_edge(F_FACT_A, F_FACT_B, "invalid")

    def test_get_edge(self, store_with_points):
        s = store_with_points
        created = s.add_edge(F_FACT_A, F_FACT_B, "supports")
        fetched = s.get_edge(created.edge_id)
        assert fetched is not None
        assert fetched.source_fact_id == F_FACT_A

    def test_get_edge_not_found(self, store_with_points):
        assert store_with_points.get_edge("nonexistent") is None

    def test_list_edges_by_fact(self, store_with_points):
        s = store_with_points
        s.add_edge(F_FACT_A, F_FACT_B, "supports")
        s.add_edge(F_FACT_A, F_FACT_C, "contradicts")
        edges = s.list_edges(fact_id=F_FACT_A)
        assert len(edges) == 2

    def test_list_edges_by_relation(self, store_with_points):
        s = store_with_points
        s.add_edge(F_FACT_A, F_FACT_B, "supports")
        s.add_edge(F_FACT_A, F_FACT_C, "contradicts")
        edges = s.list_edges(relation="supports")
        assert len(edges) == 1

    def test_has_active_edge(self, store_with_points):
        s = store_with_points
        s.add_edge(F_FACT_A, F_FACT_B, "supports")
        assert s.has_active_edge(F_FACT_A, F_FACT_B, "supports") is True
        assert s.has_active_edge(F_FACT_A, F_FACT_C, "supports") is False

    def test_reject_edge(self, store_with_points):
        s = store_with_points
        e = s.add_edge(F_FACT_A, F_FACT_B, "supports")
        rejected = s.reject_edge(e.edge_id)
        assert rejected is not None
        assert rejected.status == "rejected"
        assert s.count_edges(status="active") == 0

    def test_deprecate_edge(self, store_with_points):
        s = store_with_points
        e = s.add_edge(F_FACT_A, F_FACT_B, "supports")
        deprecated = s.deprecate_edge(e.edge_id)
        assert deprecated is not None
        assert deprecated.status == "deprecated"

    def test_count_edges(self, store_with_points):
        s = store_with_points
        s.add_edge(F_FACT_A, F_FACT_B, "supports")
        s.add_edge(F_FACT_C, F_FACT_D, "contradicts")
        assert s.count_edges() == 2
        assert s.count_edges(status="active") == 2

    def test_duplicate_active_edge_raises(self, store_with_points):
        s = store_with_points
        s.add_edge(F_FACT_A, F_FACT_B, "supports")
        with pytest.raises(Exception, match="already exists"):
            s.add_edge(F_FACT_A, F_FACT_B, "supports")

    def test_persistence(self, tmp_qdrant):
        """Edges survive across EdgeStore instances using same Qdrant path."""
        qdrant_path = os.path.join(tmp_qdrant, "persist_test")

        # Instance 1
        c1 = QdrantClient(path=qdrant_path)
        c1.create_collection(
            collection_name=TEST_COLLECTION,
            vectors_config=TEST_VECTOR_CONFIG,
        )
        create_point(c1, TEST_COLLECTION, F_F1)
        create_point(c1, TEST_COLLECTION, F_F2)
        s1 = EdgeStore(client=c1, collection=TEST_COLLECTION)
        s1.initialize()
        s1.add_edge(F_F1, F_F2, "supersedes")
        s1.close()

        # Instance 2 (same path)
        c2 = QdrantClient(path=qdrant_path)
        s2 = EdgeStore(client=c2, collection=TEST_COLLECTION)
        s2.initialize()
        assert s2.count_edges() == 1
        s2.close()

    def test_list_edges_incoming(self, store_with_points):
        """Incoming edges are found via Qdrant nested filter."""
        s = store_with_points
        s.add_edge(F_FACT_A, F_FACT_B, "supports")
        s.add_edge(F_FACT_C, F_FACT_B, "contradicts")
        edges_b = s.list_edges(fact_id=F_FACT_B)
        assert len(edges_b) == 2  # a→b + c→b


# ── SkillGraph Queries ─────────────────────────────────────────────────────


class TestSkillGraph:
    def test_add_and_rebuild(self, graph):
        graph.add_edge(F_FACT_A, F_FACT_B, "contradicts")
        assert graph.has_node(F_FACT_A)
        assert graph.has_node(F_FACT_B)

    def test_get_neighbors_outgoing(self, populated_graph):
        neighbors = populated_graph.neighbors(F_FACT_A)
        assert len(neighbors) >= 2
        outgoing = [n for n in neighbors if n["direction"] == "outgoing"]
        assert len(outgoing) >= 2

    def test_get_neighbors_filter_by_relation(self, populated_graph):
        neighbors = populated_graph.neighbors(F_FACT_A, relation="supersedes")
        assert len(neighbors) == 1
        assert neighbors[0]["fact_id"] == F_FACT_B

    def test_find_path_direct(self, populated_graph):
        paths = populated_graph.find_path(F_FACT_A, F_FACT_C)
        assert len(paths) >= 1
        assert paths[0]["source"] == F_FACT_A
        assert paths[0]["relation"] == "supersedes"

    def test_find_path_no_path(self, populated_graph):
        paths = populated_graph.find_path(F_FACT_A, F_FACT_Z)
        assert paths == []

    def test_find_path_max_depth(self, graph):
        graph.add_edge(F_FACT_A, F_FACT_B, "supersedes")
        graph.add_edge(F_FACT_B, F_FACT_C, "supersedes")
        path = graph.find_path(F_FACT_A, F_FACT_C, max_depth=1)
        assert path == []

    def test_find_path_same_node(self, graph):
        graph.add_edge(F_FACT_A, F_FACT_B, "supports")
        assert graph.find_path(F_FACT_A, F_FACT_A) == []

    def test_find_path_missing_node(self, graph):
        assert graph.find_path("does-not-exist", F_FACT_A) == []

    def test_list_edges_on_graph(self, populated_graph):
        edges = populated_graph.list_edges(fact_id=F_FACT_A)
        assert len(edges) >= 3  # outgoing + incoming

    def test_get_edge(self, populated_graph):
        e = populated_graph.add_edge(F_FACT_X, F_FACT_Y, "supports")
        fetched = populated_graph.get_edge(e.edge_id)
        assert fetched is not None
        assert fetched.source_fact_id == F_FACT_X


# ── Contradiction Chain ────────────────────────────────────────────────────


class TestContradictionChain:
    def test_direct_contradictions(self, populated_graph):
        chain = populated_graph.get_contradiction_chain(F_FACT_A)
        assert len(chain) >= 2

    def test_transitive_contradictions(self, graph):
        graph.add_edge(F_FACT_A, F_FACT_B, "contradicts")
        graph.add_edge(F_FACT_B, F_FACT_C, "contradicts")
        chain = graph.get_contradiction_chain(F_FACT_A)
        assert len(chain) == 2
        assert chain[0]["fact_id"] == F_FACT_B
        assert chain[1]["fact_id"] == F_FACT_C

    def test_no_contradictions(self, populated_graph):
        chain = populated_graph.get_contradiction_chain(F_FACT_H)
        assert chain == []


# ── Support Chain ──────────────────────────────────────────────────────────


class TestSupportChain:
    def test_direct_support(self, populated_graph):
        chain = populated_graph.get_support_chain(F_FACT_F)
        assert len(chain) == 2  # f→c, f→g

    def test_no_support(self, graph):
        chain = graph.get_support_chain("orphan")
        assert chain == []


# ── Edge Lifecycle Integration ─────────────────────────────────────────────


class TestLifecycleIntegration:
    def test_deprecated_edge_removed_from_neighbors(self, graph):
        e = graph.add_edge(F_FACT_A, F_FACT_B, "supports")
        graph.add_edge(F_FACT_A, F_FACT_C, "supports")
        graph.deprecate_edge(e.edge_id)

        neighbors = graph.neighbors(F_FACT_A)
        assert len(neighbors) == 1  # only a→c
        assert neighbors[0]["fact_id"] == F_FACT_C

    def test_path_excludes_deprecated(self, graph):
        graph.add_edge(F_FACT_A, F_FACT_B, "supersedes")
        e2 = graph.add_edge(F_FACT_B, F_FACT_C, "supersedes")
        graph.deprecate_edge(e2.edge_id)

        path = graph.find_path(F_FACT_A, F_FACT_C)
        assert path == []

    def test_rejected_edge_removed(self, graph):
        e = graph.add_edge(F_FACT_A, F_FACT_B, "contradicts")
        graph.reject_edge(e.edge_id)

        chain = graph.get_contradiction_chain(F_FACT_A)
        assert chain == []


# ── Stats ──────────────────────────────────────────────────────────────────


class TestStats:
    def test_empty_graph(self, store_with_points):
        """A graph with points but no edges has 0 nodes (NetworkX only tracks nodes with edges)."""
        g = SkillGraph(store=store_with_points)
        g.initialize()
        s = g.stats()
        assert s["nodes"] == 0
        assert s["edges"] == 0

    def test_populated(self, populated_graph):
        s = populated_graph.stats()
        assert s["nodes"] > 0
        assert s["edges"] > 0
        assert "collection" in s

    def test_collection_in_stats(self, populated_graph):
        s = populated_graph.stats()
        assert s["collection"] == TEST_COLLECTION
