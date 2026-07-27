"""Tests for the SICA Self-Improvement Cycle module."""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

# Path setup
_REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC = str(_REPO_ROOT / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from nexus.sica import (
    SICAResult,
    run_sica,
    _detect_stale_temp,
    _detect_low_confidence,
    _detect_contradictions,
    LOW_CONFIDENCE_THRESHOLD,
    STALE_TEMP_DAYS,
)


# ── Fixtures ──────────────────────────────────────────────────────────────

@pytest.fixture
def qdrant_client():
    """Return a QdrantClient connected to the local Qdrant instance."""
    from qdrant_client import QdrantClient
    client = QdrantClient(host="localhost", port=6333)
    yield client
    client.close()


@pytest.fixture
def test_collection(qdrant_client):
    """Create a temporary test collection, yield its name, then delete it."""
    from qdrant_client import models as qm

    coll = f"sica_test_{uuid.uuid4().hex[:8]}"
    qdrant_client.create_collection(
        collection_name=coll,
        vectors_config=qm.VectorParams(size=1024, distance=qm.Distance.COSINE),
    )
    yield coll
    try:
        qdrant_client.delete_collection(collection_name=coll)
    except Exception:
        pass


@pytest.fixture
def populated_collection(qdrant_client, test_collection):
    """Populate the test collection with known test points."""
    from qdrant_client import models as qm

    now = datetime.now(timezone.utc)
    stale_ts = (now - timedelta(days=STALE_TEMP_DAYS + 5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    fresh_ts = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    points = [
        # Stale temp memory (should be detected + auto-deleted)
        qm.PointStruct(
            id=str(uuid.uuid4()),
            vector=[0.0] * 1024,
            payload={
                "content": "stale temp memory",
                "category": "temp",
                "created_at": stale_ts,
                "provenance": {"confidence": 0.8},
            },
        ),
        # Low confidence fact (should be detected, not auto-fixed)
        qm.PointStruct(
            id=str(uuid.uuid4()),
            vector=[0.0] * 1024,
            payload={
                "content": "low confidence fact",
                "category": "fact",
                "created_at": fresh_ts,
                "provenance": {"confidence": 0.1},
            },
        ),
        # Normal fact (should not be flagged)
        qm.PointStruct(
            id=str(uuid.uuid4()),
            vector=[0.0] * 1024,
            payload={
                "content": "normal fact",
                "category": "fact",
                "created_at": fresh_ts,
                "provenance": {"confidence": 0.9},
            },
        ),
        # Contradiction edge (should be detected)
        qm.PointStruct(
            id=str(uuid.uuid4()),
            vector=[0.0] * 1024,
            payload={
                "content": "fact with contradiction",
                "category": "fact",
                "created_at": fresh_ts,
                "provenance": {"confidence": 0.8},
                "edges": [
                    {
                        "edge_id": str(uuid.uuid4()),
                        "target_fact_id": str(uuid.uuid4()),
                        "relation": "contradicts",
                        "status": "active",
                    }
                ],
            },
        ),
    ]

    qdrant_client.upsert(collection_name=test_collection, points=points)
    return test_collection


# ── Tests ─────────────────────────────────────────────────────────────────


class TestSICAResult:
    """Tests for the SICAResult class."""

    def test_is_silent_when_no_issues(self):
        result = SICAResult()
        assert result.is_silent is True

    def test_not_silent_when_issues(self):
        result = SICAResult()
        result.issues_found = 3
        assert result.is_silent is False

    def test_not_silent_when_errors(self):
        result = SICAResult()
        result.errors.append("something failed")
        assert result.is_silent is False

    def test_to_dict(self):
        result = SICAResult()
        result.issues_found = 2
        result.suggestions.append({"type": "test", "priority": "low"})
        d = result.to_dict()
        assert d["issues_found"] == 2
        assert len(d["suggestions"]) == 1
        assert "run_id" in d
        assert "timestamp" in d


class TestDetection:
    """Tests for the individual detection functions."""

    def test_detect_stale_temp_finds_old_temp(self):
        stale_ts = (datetime.now(timezone.utc) - timedelta(days=STALE_TEMP_DAYS + 3)).isoformat()
        points = [
            {"id": "p1", "payload": {"category": "temp", "created_at": stale_ts}},
        ]
        issues = _detect_stale_temp(points)
        assert len(issues) == 1
        assert issues[0]["type"] == "stale_temp"
        assert issues[0]["auto_fixable"] is True
        assert issues[0]["action"] == "delete"

    def test_detect_stale_temp_ignores_fresh_temp(self):
        fresh_ts = datetime.now(timezone.utc).isoformat()
        points = [
            {"id": "p1", "payload": {"category": "temp", "created_at": fresh_ts}},
        ]
        issues = _detect_stale_temp(points)
        assert len(issues) == 0

    def test_detect_stale_temp_ignores_non_temp(self):
        stale_ts = (datetime.now(timezone.utc) - timedelta(days=STALE_TEMP_DAYS + 3)).isoformat()
        points = [
            {"id": "p1", "payload": {"category": "fact", "created_at": stale_ts}},
        ]
        issues = _detect_stale_temp(points)
        assert len(issues) == 0

    def test_detect_low_confidence_finds_low(self):
        points = [
            {"id": "p1", "payload": {"provenance": {"confidence": 0.1}}},
            {"id": "p2", "payload": {"provenance": {"confidence": 0.9}}},
        ]
        issues = _detect_low_confidence(points)
        assert len(issues) == 1
        assert issues[0]["id"] == "p1"
        assert issues[0]["auto_fixable"] is False

    def test_detect_low_confidence_ignores_missing(self):
        points = [
            {"id": "p1", "payload": {}},
        ]
        issues = _detect_low_confidence(points)
        assert len(issues) == 0

    def test_detect_contradictions_finds_edge(self):
        points = [
            {
                "id": "p1",
                "payload": {
                    "edges": [
                        {"relation": "contradicts", "status": "active", "target_fact_id": "p2"},
                    ]
                },
            },
        ]
        issues = _detect_contradictions(points)
        assert len(issues) == 1
        assert issues[0]["type"] == "contradiction"
        assert issues[0]["auto_fixable"] is False

    def test_detect_contradictions_ignores_other_relations(self):
        points = [
            {
                "id": "p1",
                "payload": {
                    "edges": [
                        {"relation": "supports", "status": "active", "target_fact_id": "p2"},
                    ]
                },
            },
        ]
        issues = _detect_contradictions(points)
        assert len(issues) == 0

    def test_detect_contradictions_ignores_inactive(self):
        points = [
            {
                "id": "p1",
                "payload": {
                    "edges": [
                        {"relation": "contradicts", "status": "rejected", "target_fact_id": "p2"},
                    ]
                },
            },
        ]
        issues = _detect_contradictions(points)
        assert len(issues) == 0


class TestRunSICA:
    """Integration tests for the full SICA cycle."""

    def test_run_sica_on_empty_collection(self, qdrant_client, test_collection):
        """SICA on an empty collection returns silent result."""
        result = run_sica(client=qdrant_client, collection=test_collection)
        assert result.total_scanned == 0
        assert result.issues_found == 0
        assert result.is_silent is True

    def test_run_sica_finds_all_issues(self, qdrant_client, populated_collection):
        """SICA finds stale temp, low confidence, and contradictions."""
        result = run_sica(client=qdrant_client, collection=populated_collection, auto_patch=False)
        assert result.total_scanned == 4
        assert result.issues_found == 3  # stale_temp + low_confidence + contradiction

        types = {s["type"] for s in result.suggestions}
        # stale_temp won't be in suggestions if auto_patch=False (it's auto_fixable but we disabled)
        # Actually with auto_patch=False, auto_fixable issues become suggestions too
        assert "stale_temp" in types or any(p["type"] == "stale_temp" for p in result.auto_patches)

    def test_run_sica_auto_patches_stale_temp(self, qdrant_client, populated_collection):
        """SICA auto-deletes stale temp memories."""
        result = run_sica(client=qdrant_client, collection=populated_collection, auto_patch=True)
        # At least one auto-patch (the stale temp)
        assert len(result.auto_patches) >= 1
        assert all(p["action"] == "deleted" for p in result.auto_patches)

    def test_run_sica_does_not_auto_patch_low_confidence(self, qdrant_client, populated_collection):
        """Low confidence issues become suggestions, not auto-patches."""
        result = run_sica(client=qdrant_client, collection=populated_collection, auto_patch=True)
        # Low confidence should be in suggestions, not auto_patches
        low_conf_suggestions = [s for s in result.suggestions if s["type"] == "low_confidence"]
        assert len(low_conf_suggestions) >= 1

    def test_run_sica_does_not_auto_patch_contradictions(self, qdrant_client, populated_collection):
        """Contradictions become suggestions, not auto-patches."""
        result = run_sica(client=qdrant_client, collection=populated_collection, auto_patch=True)
        contra_suggestions = [s for s in result.suggestions if s["type"] == "contradiction"]
        assert len(contra_suggestions) >= 1

    def test_run_sica_stores_session(self, qdrant_client, populated_collection):
        """SICA stores a session memory after finding issues."""
        result = run_sica(client=qdrant_client, collection=populated_collection, auto_patch=True)
        assert result.issues_found > 0

        # Verify a sica_session memory was stored
        from qdrant_client import models as qm
        points, _ = qdrant_client.scroll(
            collection_name=populated_collection,
            scroll_filter=qm.Filter(
                must=[qm.FieldCondition(key="category", match=qm.MatchValue(value="sica_session"))]
            ),
            limit=5,
            with_payload=True,
        )
        assert len(points) >= 1
        payload = points[0].payload or {}
        assert "sica_run_id" in payload
        assert payload["sica_issues"] == result.issues_found