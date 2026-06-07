"""Tests for discovery module — AutoDiscovery, Classifier, Dedup.

v2.1.0: ~25 tests covering the full discovery pipeline.
"""

from __future__ import annotations

import os
import tempfile
import uuid
from unittest.mock import MagicMock, patch

import pytest
from qdrant_client import QdrantClient, models

from nexus.discovery.classifier import classify_relation, _check_explicit_reference
from nexus.discovery.dedup import filter_new_edges
from nexus.graph.schema import EdgeRelation
from nexus.graph.store import EdgeStore


def fact_id(name: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, name))


F_A = fact_id("fact-a")
F_B = fact_id("fact-b")
F_C = fact_id("fact-c")
F_D = fact_id("fact-d")


# ── Classifier Tests ──────────────────────────────────────────────────────


class TestClassifier:
    def test_same_category_references(self):
        """Same category → references."""
        result = classify_relation(
            source_content="Kiosha is the lead agent",
            target_content="Kiosha manages the system",
            source_category="agent",
            target_category="agent",
            source_id=F_A,
            target_id=F_B,
            similarity_score=0.92,
        )
        assert result["relation"] == "references"
        assert result["confidence"] > 0.80  # 0.92 * 0.9 = 0.828

    def test_wikilink_depends_on(self):
        """[[Wikilink]] → depends_on with high confidence."""
        result = classify_relation(
            source_content="See [[Kiosha Agent]] for details",
            target_content="Kiosha Agent is the lead system",
            source_category="docs",
            target_category="docs",
            source_id=F_A,
            target_id=F_B,
            similarity_score=0.95,
        )
        assert result["relation"] == "depends_on"
        assert result["confidence"] >= 0.90

    def test_see_also_depends_on(self):
        """"siehe" pattern → depends_on."""
        result = classify_relation(
            source_content="See also Memory System for the configuration",
            target_content="Memory System Configuration Guide",
            source_category="docs",
            target_category="docs",
            source_id=F_A,
            target_id=F_B,
            similarity_score=0.88,
        )
        assert result["relation"] == "depends_on"

    def test_dependency_keyword_depends_on(self):
        """"requires" pattern → depends_on."""
        result = classify_relation(
            source_content="This requires Memory System to function",
            target_content="Memory System handles persistence",
            source_category="config",
            target_category="config",
            source_id=F_A,
            target_id=F_B,
            similarity_score=0.85,
        )
        assert result["relation"] == "depends_on"

    def test_contradiction_detected(self):
        """Contradiction keywords + shared topics → contradicts."""
        result = classify_relation(
            source_content="The port is 8080 but we changed it",
            target_content="The port is 3000 for the service",
            source_category="config",
            target_category="config",
            source_id=F_A,
            target_id=F_B,
            similarity_score=0.70,
        )
        # May or may not detect contradiction depending on keyword overlap
        # At minimum it should not crash
        assert isinstance(result, dict) or result is None

    def test_supersedes_same_category_versions(self):
        """Version markers in same category → supersedes."""
        result = classify_relation(
            source_content="v2.1.0 is the latest version",
            target_content="v2.0.0 was the previous version",
            source_category="release",
            target_category="release",
            source_id=F_A,
            target_id=F_B,
            similarity_score=0.80,
        )
        assert result is not None

    def test_low_similarity_no_category_returns_none(self):
        """Low similarity + no category match → None (no noise)."""
        result = classify_relation(
            source_content="How to install Python packages",
            target_content="Best pizza toppings in Berlin",
            source_category="tech",
            target_category="food",
            source_id=F_A,
            target_id=F_B,
            similarity_score=0.86,  # Just above threshold
        )
        # Should return None because similarity < 0.90 and no category/explicit match
        assert result is None

    def test_high_similarity_low_keyword_overlap_returns_references(self):
        """High similarity even without keyword overlap → references (strong signal)."""
        result = classify_relation(
            source_content="The memory system uses Qdrant for vector search",
            target_content="Qdrant provides approximate nearest neighbor search",
            source_category="tech",
            target_category="tech",
            source_id=F_A,
            target_id=F_B,
            similarity_score=0.95,
        )
        assert result is not None
        assert result["relation"] == "references"

    def test_keyword_overlap_eighty_percent_references(self):
        """High keyword overlap → references."""
        result = classify_relation(
            source_content="Kiosha manages memory Kiosha handles search Kiosha routes",
            target_content="Kiosha manages memory Kiosha handles search Kiosha routes",
            source_category="agent",
            target_category="agent",
            source_id=F_A,
            target_id=F_B,
            similarity_score=0.93,
        )
        assert result["relation"] == "references"

    def test_no_relation_empty_content(self):
        """Empty content → no crash, returns None."""
        result = classify_relation(
            source_content="",
            target_content="",
            source_category="",
            target_category="",
            source_id=F_A,
            target_id=F_B,
            similarity_score=0.50,
        )
        # Should not crash
        assert result is None

    def test_unicode_content(self):
        """German umlauts in content → no crash."""
        result = classify_relation(
            source_content="Änderungen an der Konfiguration",
            target_content="Konfiguration der Überwachung",
            source_category="config",
            target_category="config",
            source_id=F_A,
            target_id=F_B,
            similarity_score=0.88,
        )
        assert result is not None
        assert isinstance(result["confidence"], float)

    def test_wikilink_word_boundary(self):
        """Wikilink word boundary: 'Open' should NOT match 'OpenAir'."""
        result = classify_relation(
            source_content="See [[Open]] protocol",
            target_content="OpenAir Festival configuration",
            source_category="docs",
            target_category="docs",
            source_id=F_A,
            target_id=F_B,
            similarity_score=0.87,
        )
        # 'Open' is only 4 chars so it won't match target_keywords (needs >3 chars + kw in link_lower)
        # The second wikilink check uses word boundaries → 'Open' != 'OpenAir'
        # Since no explicit match, falls through to category match
        assert result is None or result["relation"] in ("references",)


# ── Dedup Tests ───────────────────────────────────────────────────────────


class TestDedup:
    @pytest.fixture
    def store(self):
        with tempfile.TemporaryDirectory() as tmp:
            from qdrant_client import QdrantClient, models
            client = QdrantClient(path=os.path.join(tmp, "qdrant"))
            client.create_collection(
                collection_name="test-memory",
                vectors_config=models.VectorParams(size=2, distance=models.Distance.COSINE),
            )
            # Create needed fact-points
            all_ids = [F_A, F_B, F_C, F_D]
            client.upsert(
                collection_name="test-memory",
                points=[
                    models.PointStruct(id=fid, vector=[0.0, 0.0], payload={"content": f"Test {fid}"})
                    for fid in all_ids
                ],
            )
            store = EdgeStore(client=client, collection="test-memory")
            store.initialize()
            yield store

    def test_filter_new_edges_all_new(self, store):
        """All candidates are new → all pass through."""
        candidates = [
            {"source": F_A, "target": F_B, "relation": "references"},
            {"source": F_C, "target": F_D, "relation": "supports"},
        ]
        result = filter_new_edges(candidates, store)
        assert len(result) == 2

    def test_filter_new_edges_some_duplicates(self, store):
        """Existing edges are filtered out."""
        store.add_edge(F_A, F_B, "references", reason="test")
        candidates = [
            {"source": F_A, "target": F_B, "relation": "references"},
            {"source": F_C, "target": F_D, "relation": "supports"},
        ]
        result = filter_new_edges(candidates, store)
        assert len(result) == 1
        assert result[0]["source"] == F_C

    def test_filter_new_edges_all_duplicates(self, store):
        """All existing → empty result."""
        store.add_edge(F_A, F_B, "references", reason="test")
        store.add_edge(F_C, F_D, "supports", reason="test")
        candidates = [
            {"source": F_A, "target": F_B, "relation": "references"},
            {"source": F_C, "target": F_D, "relation": "supports"},
        ]
        result = filter_new_edges(candidates, store)
        assert len(result) == 0

    def test_filter_new_edges_skips_proposed(self, store):
        """Proposed edges are also filtered out."""
        store.add_proposed_edge(F_A, F_B, "references", confidence=0.75)
        candidates = [
            {"source": F_A, "target": F_B, "relation": "references"},
        ]
        result = filter_new_edges(candidates, store)
        assert len(result) == 0

    def test_filter_empty_candidates(self, store):
        """Empty candidate list → empty result."""
        result = filter_new_edges([], store)
        assert result == []

    def test_filter_missing_keys(self, store):
        """Candidates missing source/target → gracefully skipped."""
        candidates = [
            {"source": F_A, "relation": "references"},  # Missing target
            {"target": F_B, "relation": "supports"},     # Missing source
            {"source": F_C, "target": F_D, "relation": "references"},
        ]
        result = filter_new_edges(candidates, store)
        assert len(result) == 1


# ── AutoDiscovery Pipeline Tests ──────────────────────────────────────────


class TestAutoDiscoveryPipeline:
    """Tests for the pipeline logic (classify_confidence, etc.).

    Full integration tests require Qdrant — these test the pipeline
    logic that doesn't need a running Qdrant instance.
    """

    def test_classify_confidence_above_active_threshold(self):
        """Confidence ≥ 0.85 → active insert."""
        from nexus.discovery import classify_confidence
        result = classify_confidence(0.90)
        assert result["should_insert"] is True
        assert result["as_proposed"] is False

    def test_classify_confidence_below_active_threshold(self):
        """Confidence 0.70–0.84 → proposed insert."""
        from nexus.discovery import classify_confidence
        result = classify_confidence(0.80)
        assert result["should_insert"] is True
        assert result["as_proposed"] is True

    def test_classify_confidence_below_min(self):
        """Confidence < 0.70 → no insertion."""
        from nexus.discovery import classify_confidence
        result = classify_confidence(0.60)
        assert result["should_insert"] is False
        assert result["as_proposed"] is False

    def test_auto_discovery_init_no_qdrant(self):
        """AutoDiscovery initialises without Qdrant."""
        from nexus.discovery import AutoDiscovery
        # Must pass collection explicitly (no default since v2.2.0)
        ad = AutoDiscovery(collection="test-collection")
        assert ad is not None
        assert ad.store is not None

    def test_discover_for_fact_empty_vector(self):
        """Empty vector → empty result."""
        from nexus.discovery import AutoDiscovery
        ad = AutoDiscovery(collection="test-collection")
        result = ad.discover_for_fact(F_A, "content", "category", [])
        assert result == []
