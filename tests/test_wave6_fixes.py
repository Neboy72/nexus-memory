"""OCR review wave 6 — regression tests for fixes G1–G10.

One test class per finding; each test fails on the pre-fix code and passes
after the surgical fix. See /tmp/ocr-fix-prompts/groupG-tasks.md.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from nexus.discovery.classifier import _check_supersedes, classify_relation
from nexus.discovery import AutoDiscovery
import nexus.discovery.matcher as matcher
import nexus.discovery as discovery
import nexus.events as events
import nexus.health as health
import nexus.retrieval as retrieval
from nexus.graph.graph import SkillGraph
from nexus.graph.schema import Edge

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC = str(_REPO_ROOT / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

# Load the integrations copy of the Hermes plugin point-blank (the repo also
# ships plugins/memory/nexus/ and a top-level `nexus` package).
_PLUGIN_PATH = _REPO_ROOT / "integrations" / "hermes-plugin" / "__init__.py"
_spec = importlib.util.spec_from_file_location(
    "nexus_hermes_plugin_integrations", str(_PLUGIN_PATH)
)
assert _spec is not None, f"Could not load {_PLUGIN_PATH}"
hermes_plugin = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hermes_plugin)
NexusMemoryProvider = hermes_plugin.NexusMemoryProvider


# ── Test doubles ──────────────────────────────────────────────────────────


class _Resp:
    """Minimal requests.Response stand-in that records raise_for_status()."""

    def __init__(self, payload=None, raise_exc=None, status_code=200):
        self._payload = payload if payload is not None else {}
        self._raise_exc = raise_exc
        self.status_code = status_code
        self.text = ""
        self.raise_called = False

    def raise_for_status(self):
        self.raise_called = True
        if self._raise_exc is not None:
            raise self._raise_exc

    def json(self):
        return self._payload


class _RetrieverStub:
    """Only the attributes HybridRetriever's network methods touch."""

    qdrant_url = "http://localhost:6333"
    collection = "test-collection"


class _FailLock:
    """Lock whose acquire() always fails (contention)."""

    def acquire(self, blocking=False):
        return False

    def release(self):
        raise AssertionError("release() must not be called on a failed acquire")


# ── G1: no-category supersedes guard (#40) ────────────────────────────────


class TestG1NoCategorySupersedes:
    def test_empty_categories_no_supersedes(self):
        assert _check_supersedes("v2.0.1 newer", "v2.0.0 older", "", "") is None

    def test_one_side_empty_no_supersedes(self):
        assert _check_supersedes("v2.0.1", "v2.0.0", "", "release") is None
        assert _check_supersedes("v2.0.1", "v2.0.0", "release", "") is None

    def test_classify_relation_never_supersedes_without_category(self):
        result = classify_relation(
            source_content="v2.0.1 is newer",
            target_content="v2.0.0 is older",
            source_category="",
            target_category="",
            source_id="a",
            target_id="b",
            similarity_score=0.80,
        )
        assert result is None or result["relation"] != "supersedes"

    def test_same_category_still_supersedes(self):
        # W31-4: conjunction — category match alone is not enough; the pair
        # needs version strings AND direction language (see TestG2VersionMarker).
        result = _check_supersedes(
            "release v2.0.1 is the latest", "release v2.0.0 is older", "release", "release"
        )
        assert result is not None and result["relation"] == "supersedes"


# ── G2: version marker too broad (#41) ────────────────────────────────────


class TestG2VersionMarker:
    def test_plain_decimals_no_longer_match(self):
        assert _check_supersedes("latency is 2.5 ms", "latency is 1.75 ms", "cfg", "cfg") is None

    def test_real_version_string_matches(self):
        # W31-4: conjunction — version PLUS direction language; bare version
        # alone no longer emits (old test asserted the removed disjunction).
        assert _check_supersedes("release v2.0.1", "release v2.0.0", "release", "release") is None

    def test_version_and_language_both_present_matches(self):
        # W31-4: both signals together still classify as supersedes.
        result = _check_supersedes(
            "release v2.0.1 — the latest build", "release v2.0.0 is now old", "release", "release",
        )
        assert result is not None and result["relation"] == "supersedes"

    def test_bare_semver_without_language_does_not_match(self):
        # W31-4: semver without any newer/older wording no longer emits.
        assert _check_supersedes("build 1.2.3", "build 1.2.2", "rel", "rel") is None

    def test_word_marker_still_matches(self):
        # review #41: explicit newer/older language remains a signal; W31-4
        # narrows it to require a parseable version on BOTH sides.
        assert _check_supersedes(
            "the newer approach v1.1.0", "the old approach v1.0.0", "c", "c",
        ) is not None

    def test_two_component_decimal_not_a_version(self):
        assert _check_supersedes("ratio 3.14 here", "ratio 2.71 here", "c", "c") is None

    def test_same_version_pair_never_supersedes(self):
        # W31-4: identical versions must not produce a supersedes edge.
        assert _check_supersedes(
            "release v2.0.0 latest", "release v2.0.0 older notes", "c", "c",
        ) is None


# ── G3: dedup hash before lock (#42) ──────────────────────────────────────


class TestG3DedupHashOrdering:
    def test_lock_contention_does_not_mark_hash_seen(self, monkeypatch):
        p = NexusMemoryProvider()
        monkeypatch.setattr(p, "_entity_extract_lock", _FailLock())
        p._enqueue_entity_extraction("z" * 120)
        assert getattr(p, "_extract_hashes", set()) == set()

    def test_successful_acquire_marks_hash_seen(self, monkeypatch):
        p = NexusMemoryProvider()
        monkeypatch.setattr(p, "_entity_extract_lock", threading.Lock())
        monkeypatch.setattr(p, "_extract_entities_from_text", lambda *a, **k: {})
        p._enqueue_entity_extraction("y" * 120)
        assert len(p._extract_hashes) == 1

    def test_repeated_call_after_failure_is_retried(self, monkeypatch):
        p = NexusMemoryProvider()
        monkeypatch.setattr(p, "_entity_extract_lock", _FailLock())
        p._enqueue_entity_extraction("x" * 120)
        # Second call must still be able to run once the lock is free.
        extracted = []
        monkeypatch.setattr(p, "_entity_extract_lock", threading.Lock())
        monkeypatch.setattr(
            p, "_extract_entities_from_text",
            lambda *a, **k: extracted.append(1) or {},
        )
        p._enqueue_entity_extraction("x" * 120)
        assert extracted == [1]


# ── G4: session-end hardcoded public (#43) ────────────────────────────────


class TestG4DefaultAccessLevel:
    def _provider_with_config(self, tmp_path, config):
        p = NexusMemoryProvider()
        p._hermes_home = str(tmp_path)
        nexus_dir = tmp_path / "nexus"
        nexus_dir.mkdir()
        (nexus_dir / "config.json").write_text(json.dumps(config))
        return p

    def test_default_is_private_without_config(self, tmp_path):
        p = NexusMemoryProvider()
        p._hermes_home = str(tmp_path)
        assert p._load_default_access_level() == "private"

    def test_config_private_read(self, tmp_path):
        p = self._provider_with_config(tmp_path, {"access_level": "private"})
        assert p._load_default_access_level() == "private"

    def test_config_override_public_read(self, tmp_path):
        p = self._provider_with_config(tmp_path, {"access_level": "public"})
        assert p._load_default_access_level() == "public"

    def test_session_end_uses_default_access_level(self, monkeypatch):
        p = NexusMemoryProvider()
        p._default_access_level = "private"
        p._qdrant = object()
        p._write_stop = threading.Event()

        upserts = []
        entity_calls = []
        monkeypatch.setattr(p, "_upsert", lambda **kw: upserts.append(kw))
        monkeypatch.setattr(
            p, "_extract_entities_from_text",
            lambda *a, **k: entity_calls.append(k) or {"entities": 0, "edges": 0},
        )

        import nexus_memory.extractor as extractor
        monkeypatch.setattr(
            extractor, "extract_facts",
            lambda messages, hermes_home=None: [
                {"text": "durable fact", "category": "fact", "confidence": 0.9}
            ],
        )

        p.on_session_end([{"role": "user", "content": "hello there"}])

        assert upserts and upserts[0]["access_level"] == "private"
        assert entity_calls and entity_calls[0]["access_level"] == "private"

    def test_on_memory_write_uses_default_access_level(self, monkeypatch):
        p = NexusMemoryProvider()
        p._default_access_level = "private"
        seen = []
        monkeypatch.setattr(p, "_upsert", lambda **kw: seen.append(kw))
        p.on_memory_write("add", "memory", "some content")
        assert seen and seen[0]["access_level"] == "private"


# ── G5: missing status checks (#44) ───────────────────────────────────────


class TestG5StatusChecks:
    def test_health_scroll_raises_on_bad_status(self, monkeypatch):
        detector = health.DriftDetector(collection_name="test-collection")
        resp = _Resp(raise_exc=RuntimeError("HTTP 500"))
        monkeypatch.setattr(
            health, "requests", SimpleNamespace(post=lambda *a, **k: resp)
        )
        with pytest.raises(RuntimeError):
            detector._scroll_all()
        assert resp.raise_called

    def test_retrieval_scroll_checks_status(self, monkeypatch):
        resp = _Resp(raise_exc=RuntimeError("HTTP 500"))
        monkeypatch.setattr(
            retrieval, "requests", SimpleNamespace(post=lambda *a, **k: resp)
        )
        with pytest.raises(RuntimeError):
            retrieval.HybridRetriever.index_memories(_RetrieverStub())
        assert resp.raise_called

    def test_retrieval_search_checks_status(self, monkeypatch):
        post_resp = _Resp(raise_exc=RuntimeError("HTTP 500"))
        get_resp = _Resp(raise_exc=RuntimeError("offline"))
        monkeypatch.setattr(
            retrieval, "requests",
            SimpleNamespace(post=lambda *a, **k: post_resp, get=lambda *a, **k: get_resp),
        )
        with pytest.raises(RuntimeError):
            retrieval.HybridRetriever.search_vector(_RetrieverStub(), [0.1, 0.2])
        assert post_resp.raise_called

    def test_retrieval_dim_guard_checks_status(self, monkeypatch):
        post_resp = _Resp(raise_exc=RuntimeError("HTTP 500"))
        get_resp = _Resp(raise_exc=RuntimeError("offline"))
        monkeypatch.setattr(
            retrieval, "requests",
            SimpleNamespace(post=lambda *a, **k: post_resp, get=lambda *a, **k: get_resp),
        )
        with pytest.raises(RuntimeError):
            retrieval.HybridRetriever.search_vector(_RetrieverStub(), [0.1, 0.2])
        assert get_resp.raise_called


# ── G6: missing ingested_at index (#45) ───────────────────────────────────


class TestG6EventIndices:
    def test_ensure_collection_indexes_datetime_fields(self, monkeypatch):
        created_indices = []

        def _fake_put(url, json=None, timeout=None):
            if url.endswith("/index"):
                created_indices.append(json["field_name"])
            return _Resp(status_code=200)

        def _fake_get(url, timeout=None):
            return SimpleNamespace(status_code=404, text="not found")

        monkeypatch.setattr(
            events, "requests",
            SimpleNamespace(get=_fake_get, put=_fake_put),
        )
        assert events.ensure_collection() is True
        assert "ingested_at" in created_indices
        assert "event_time" in created_indices


# ── G7: errors mistaken for empty data (#46) ──────────────────────────────


class TestG7MatcherErrors:
    def test_search_raises_runtime_error(self, monkeypatch):
        import requests as _requests

        def _boom(*a, **k):
            raise _requests.ConnectionError("connection refused")

        monkeypatch.setattr(matcher.requests, "post", _boom)
        with pytest.raises(RuntimeError):
            matcher.search_similar_facts([0.1], collection="test-collection")

    def test_scroll_raises_runtime_error(self, monkeypatch):
        import requests as _requests

        def _boom(*a, **k):
            raise _requests.ConnectionError("connection refused")

        monkeypatch.setattr(matcher.requests, "post", _boom)
        with pytest.raises(RuntimeError):
            matcher.scroll_facts(collection="test-collection")

    def test_discover_all_collects_search_errors(self, monkeypatch):
        monkeypatch.setattr(
            discovery, "scroll_facts",
            lambda **k: [{"id": "a", "vector": [0.1, 0.2], "payload": {}}],
        )

        def _boom(**k):
            raise RuntimeError("Qdrant search failed: refused")

        monkeypatch.setattr(discovery, "search_similar_facts", _boom)
        ad = AutoDiscovery(store=MagicMock(), collection="test-collection")
        summary = ad.discover_all()
        assert summary["errors"]
        assert any("Qdrant search failed" in e for e in summary["errors"])

    def test_discover_all_reports_scroll_error(self, monkeypatch):
        def _boom(**k):
            raise RuntimeError("Qdrant scroll failed: refused")

        monkeypatch.setattr(discovery, "scroll_facts", _boom)
        ad = AutoDiscovery(store=MagicMock(), collection="test-collection")
        summary = ad.discover_all()
        assert summary["status"] == "scroll_failed"
        assert "Qdrant scroll failed" in summary["errors"][0]


# ── G8: BFS visited cap (#47) ─────────────────────────────────────────────


class TestG8BFSVisitedCap:
    def test_find_path_on_wide_graph(self):
        graph = SkillGraph(store=MagicMock())
        for i in range(40):
            graph._graph.add_edge("src", f"c{i}", relation="references", edge_id=f"e{i}")
        graph._graph.add_edge("c39", "target", relation="references", edge_id="ex")

        # max_depth=3 → old code capped visited at 30 and returned [].
        path = graph.find_path("src", "target", max_depth=3)
        assert len(path) == 2
        assert path[0]["source"] == "src"
        assert path[-1]["target"] == "target"


# ── G9: remove without edge identity (#48) ────────────────────────────────


class TestG9EdgeIdentity:
    def test_wrong_edge_id_keeps_cached_edge(self):
        graph = SkillGraph(store=MagicMock())
        real = Edge.new("u", "v", "references")
        graph._add_edge_to_graph(real)
        assert graph._graph.has_edge("u", "v")

        other = Edge.new("u", "v", "references")  # different edge_id
        graph._remove_edge_from_graph(other)
        assert graph._graph.has_edge("u", "v")

    def test_matching_edge_id_removes_cached_edge(self):
        graph = SkillGraph(store=MagicMock())
        real = Edge.new("u", "v", "references")
        graph._add_edge_to_graph(real)
        graph._remove_edge_from_graph(real)
        assert not graph._graph.has_edge("u", "v")

    def test_contradicts_wrong_edge_id_keeps_reverse_edge(self):
        graph = SkillGraph(store=MagicMock())
        real = Edge.new("u", "v", "contradicts")
        graph._add_edge_to_graph(real)
        assert graph._graph.has_edge("v", "u")

        other = Edge.new("u", "v", "contradicts")  # different edge_id
        graph._remove_edge_from_graph(other)
        assert graph._graph.has_edge("v", "u")


# ── G10: naive vs aware datetime (#49) ────────────────────────────────────


class TestG10PruneUnusedTimezone:
    def test_aware_recent_timestamp_not_unused(self, monkeypatch):
        detector = health.DriftDetector(collection_name="test-collection")
        recent = datetime.now(timezone.utc).isoformat()
        monkeypatch.setattr(detector, "_load_usage", lambda: {"m1": recent})
        assert detector.prune_unused(days=90) == []

    def test_aware_old_timestamp_is_unused(self, monkeypatch):
        detector = health.DriftDetector(collection_name="test-collection")
        old = (datetime.now(timezone.utc) - timedelta(days=120)).isoformat()
        monkeypatch.setattr(detector, "_load_usage", lambda: {"m1": old})
        assert detector.prune_unused(days=90) == ["m1"]

    def test_naive_old_timestamp_is_unused(self, monkeypatch):
        detector = health.DriftDetector(collection_name="test-collection")
        old = (datetime.now() - timedelta(days=120)).isoformat()
        monkeypatch.setattr(detector, "_load_usage", lambda: {"m1": old})
        assert detector.prune_unused(days=90) == ["m1"]
