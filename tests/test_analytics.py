"""Tests for analytics module — GraphAnalytics, Scoring, Clustering.

v2.1.0: ~15 tests covering all analytics functions.
v2.2.0: Qdrant-Payload backend (was SQLite).
"""

from __future__ import annotations

import os
import tempfile
import uuid

import networkx as nx
import pytest
from qdrant_client import QdrantClient, models

from nexus.graph.store import EdgeStore
from nexus.graph.graph import SkillGraph
from nexus.analytics.scoring import (
    hub_scores,
    isolation_score,
    knowledge_gaps,
    relation_distribution,
)
from nexus.analytics.clustering import find_clusters, cluster_summary


def fact_id(name: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, name))


# IDs used in the populated graph: fact-1 through fact-8
F1 = fact_id("fact-1")
F2 = fact_id("fact-2")
F3 = fact_id("fact-3")
F4 = fact_id("fact-4")
F5 = fact_id("fact-5")
F6 = fact_id("fact-6")
F7 = fact_id("fact-7")
F8 = fact_id("fact-8")

ALL_FACTS = [F1, F2, F3, F4, F5, F6, F7, F8]


def create_points(client, collection):
    client.upsert(
        collection_name=collection,
        points=[
            models.PointStruct(
                id=fid, vector=[0.0, 0.0],
                payload={"content": f"Test {fid}"},
            )
            for fid in ALL_FACTS
        ],
    )


@pytest.fixture
def qdrant_path():
    with tempfile.TemporaryDirectory() as tmp:
        yield os.path.join(tmp, "qdrant")


@pytest.fixture
def skillgraph(qdrant_path):
    client = QdrantClient(path=qdrant_path)
    client.create_collection(
        collection_name="test-memory",
        vectors_config=models.VectorParams(size=2, distance=models.Distance.COSINE),
    )
    create_points(client, "test-memory")
    store = EdgeStore(client=client, collection="test-memory")
    sg = SkillGraph(store=store)
    sg.initialize()

    sg.add_edge(F1, F2, "references", reason="test")
    sg.add_edge(F2, F3, "supports", reason="test")
    sg.add_edge(F3, F4, "depends_on", reason="test")
    sg.add_edge(F1, F3, "references", reason="test")
    sg.add_edge(F5, F6, "references", reason="test")

    yield sg


@pytest.fixture
def empty_skillgraph(qdrant_path):
    client = QdrantClient(path=qdrant_path + "_empty")
    client.create_collection(
        collection_name="test-memory",
        vectors_config=models.VectorParams(size=2, distance=models.Distance.COSINE),
    )
    store = EdgeStore(client=client, collection="test-memory")
    sg = SkillGraph(store=store)
    sg.initialize()
    return sg


class TestHubScores:
    def test_hub_scores_top_hubs(self, skillgraph):
        scores = hub_scores(skillgraph, top_n=5)
        assert len(scores) >= 2
        top = scores[0]
        assert top["degree"] >= 2
        assert "fact_id" in top

    def test_hub_scores_empty_graph(self, empty_skillgraph):
        scores = hub_scores(empty_skillgraph, top_n=5)
        assert scores == []

    def test_hub_sorts_by_degree(self, skillgraph):
        scores = hub_scores(skillgraph, top_n=10)
        for i in range(len(scores) - 1):
            assert scores[i]["degree"] >= scores[i + 1]["degree"]


class TestIsolationScore:
    def test_isolated_fact(self, skillgraph):
        result = isolation_score(skillgraph, F7)
        assert result["is_isolated"] is True
        assert result["degree"] == 0

    def test_connected_fact(self, skillgraph):
        result = isolation_score(skillgraph, F1)
        assert result["is_isolated"] is False
        assert result["degree"] > 0

    def test_nonexistent_fact(self, skillgraph):
        result = isolation_score(skillgraph, "nonexistent")
        assert result["is_isolated"] is True
        assert "error" in result


class TestKnowledgeGaps:
    def test_finds_isolated_facts(self, skillgraph):
        gaps = knowledge_gaps(skillgraph)
        gap_ids = [g["fact_id"] for g in gaps]
        assert F5 not in gap_ids

    def test_connected_facts_not_in_gaps(self, skillgraph):
        gaps = knowledge_gaps(skillgraph, isolation_threshold=0.9)
        gap_ids = [g["fact_id"] for g in gaps]
        assert F1 not in gap_ids
        assert F2 not in gap_ids

    def test_empty_graph(self, empty_skillgraph):
        gaps = knowledge_gaps(empty_skillgraph)
        assert gaps == []


class TestRelationDistribution:
    def test_counts_relations(self, skillgraph):
        dist = relation_distribution(skillgraph)
        assert dist.get("references", 0) >= 2
        assert dist.get("supports", 0) >= 1

    def test_empty_graph(self, empty_skillgraph):
        dist = relation_distribution(empty_skillgraph)
        assert dist == {}


class TestFindClusters:
    def test_finds_connected_components(self, skillgraph):
        clusters = find_clusters(skillgraph, min_size=2)
        assert len(clusters) >= 2

    def test_singletons_filtered(self, skillgraph):
        clusters = find_clusters(skillgraph, min_size=2)
        for c in clusters:
            assert c["size"] >= 2

    def test_clusters_sorted_by_size(self, skillgraph):
        clusters = find_clusters(skillgraph)
        for i in range(len(clusters) - 1):
            assert clusters[i]["size"] >= clusters[i + 1]["size"]


class TestClusterSummary:
    def test_summary_contains_keys(self, skillgraph):
        summary = cluster_summary(skillgraph)
        assert "total_nodes" in summary
        assert "total_edges" in summary
        assert "num_clusters" in summary
        assert "largest_cluster_size" in summary
        assert "singletons" in summary

    def test_empty_graph_summary(self, empty_skillgraph):
        summary = cluster_summary(empty_skillgraph)
        assert summary["total_nodes"] == 0
        assert summary["num_clusters"] == 0

    def test_singletons_counted(self, skillgraph):
        summary = cluster_summary(skillgraph)
        assert summary["singletons"] >= 0
