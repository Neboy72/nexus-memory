"""Tests for reranker.py + Hermes-plugin rerank integration (roadmap 1.2).

Covers: config loading (file + env overrides), rerank_points ordering,
fail-open behavior, and the plugin's _recall rerank wiring via mocked
Qdrant points.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import threading

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pt(point_id: str, content: str, score: float) -> SimpleNamespace:
    """Duck-typed Qdrant ScoredPoint."""
    return SimpleNamespace(
        id=point_id, payload={"id": point_id, "content": content}, score=score
    )


def _pool() -> list:
    """6 points whose vector order puts the 'wrong' doc first."""
    return [
        _pt("p1", "completely unrelated text about gardening", 0.92),
        _pt("p2", "fallback routing config for providers", 0.88),
        _pt("p3", "routing: how to route fallback providers", 0.85),
        _pt("p4", "cat pictures collection", 0.80),
        _pt("p5", "deepseek routing table for fallback", 0.77),
        _pt("p6", "", 0.75),  # no content — must keep relative order at end
    ]


def _make_provider():
    """Real provider object without __init__ (avoids config file read)."""
    import plugins.memory.nexus as mod

    return object.__new__(mod.NexusMemoryProvider)


# ---------------------------------------------------------------------------
# load_rerank_config
# ---------------------------------------------------------------------------


class TestLoadRerankConfig:
    def test_defaults_when_no_file(self, tmp_path):
        from nexus_memory.reranker import load_rerank_config

        cfg = load_rerank_config(str(tmp_path / "missing.yaml"))
        assert cfg["enabled"] is False
        assert cfg["reranker"] == "voyage"
        assert cfg["pool_k"] == 20

    def test_reads_config_block(self, tmp_path):
        import yaml

        from nexus_memory.reranker import load_rerank_config

        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(
            yaml.safe_dump(
                {
                    "nexus-memory": {
                        "rerank": True,
                        "reranker": "cross-encoder",
                        "rerank_pool": 12,
                        "voyage_api_key": "pv-123",
                    }
                }
            )
        )
        cfg = load_rerank_config(str(cfg_file))
        assert cfg["enabled"] is True
        assert cfg["reranker"] == "cross-encoder"
        assert cfg["pool_k"] == 12
        assert cfg["voyage_api_key"] == "pv-123"

    def test_env_overrides_config(self, tmp_path, monkeypatch):
        import yaml

        from nexus_memory.reranker import load_rerank_config

        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(yaml.safe_dump({"nexus-memory": {"rerank": True}}))
        monkeypatch.setenv("NEXUS_RERANK", "0")
        monkeypatch.setenv("NEXUS_RERANKER", "cross-encoder")
        cfg = load_rerank_config(str(cfg_file))
        assert cfg["enabled"] is False
        assert cfg["reranker"] == "cross-encoder"

    def test_malformed_file_fails_open(self, tmp_path):
        from nexus_memory.reranker import load_rerank_config

        cfg_file = tmp_path / "broken.yaml"
        cfg_file.write_text("nexus-memory: [`unbalanced")
        cfg = load_rerank_config(str(cfg_file))
        assert cfg["enabled"] is False  # defaults hold


# ---------------------------------------------------------------------------
# rerank_points
# ---------------------------------------------------------------------------


class TestRerankPoints:
    def test_orders_by_fake_reranker(self, monkeypatch):
        import nexus_memory.reranker as rr

        # Fake local reranker: prefers docs containing query terms.
        def fake_local(query, results):
            q_terms = query.lower().split()
            return sorted(
                results,
                key=lambda r: (
                    sum(t in r["text"].lower() for t in q_terms),
                    -r["_idx"],
                ),
                reverse=True,
            )

        monkeypatch.setattr(rr, "_rerank_local", fake_local)
        pts = _pool()
        out = rr.rerank_points(
            "routing fallback", pts, reranker="cross-encoder", pool_k=5
        )
        # p2/p3/p5 all match 2 terms; tie-break keeps original sequence.
        assert [p.id for p in out[:3]] == ["p2", "p3", "p5"]
        assert [p.id for p in out[3:5]] == ["p1", "p4"]
        # p6 (no text) is beyond pool — appended at the end, preserved.
        assert out[-1].id == "p6"

    def test_fail_open_on_exception(self, monkeypatch):
        import nexus_memory.reranker as rr

        monkeypatch.setattr(
            rr, "_rerank_local", MagicMock(side_effect=RuntimeError("boom"))
        )
        pts = _pool()
        out = rr.rerank_points("q", pts, reranker="cross-encoder", pool_k=5)
        # Original order preserved (fail-open).
        assert [p.id for p in out] == [p.id for p in pts]

    def test_unknown_reranker_noop(self):
        from nexus_memory.reranker import rerank_points

        pts = _pool()
        out = rerank_points("q", pts, reranker="nope")
        assert [p.id for p in out] == [p.id for p in pts]

    def test_voyage_rerank(self, monkeypatch):
        import nexus_memory.reranker as rr

        class FakeResp:
            status_code = 200

            def json(self):
                return {
                    "data": [
                        {"index": 2, "relevance_score": 0.99},
                        {"index": 0, "relevance_score": 0.10},
                        {"index": 1, "relevance_score": 0.50},
                    ]
                }

        fake_requests = MagicMock()
        fake_requests.post.return_value = FakeResp()
        monkeypatch.setattr(rr, "_requests", fake_requests)

        pts = _pool()[:3]
        out = rr.rerank_points("q", pts, reranker="voyage", voyage_api_key="k")
        assert [p.id for p in out] == ["p3", "p2", "p1"]

    def test_empty_points_passthrough(self):
        from nexus_memory.reranker import rerank_points

        assert rerank_points("q", [], reranker="voyage") == []


# ---------------------------------------------------------------------------
# Plugin integration: _recall honours rerank config
# ---------------------------------------------------------------------------


class TestRecallRerankIntegration:
    def _provider(self, cfg):
        """Provider instance (skip __init__) with mocked embedder/qdrant."""
        prov = _make_provider()
        prov._rerank_cfg = cfg
        prov._rerank_lock = threading.Lock()
        prov._embedder = SimpleNamespace(embed=lambda q: [0.0], dim=2)
        client = MagicMock()
        client.query_points.side_effect = (
            lambda collection_name, query, limit: SimpleNamespace(
                points=_pool()
            )
        )
        prov._qdrant = client
        prov._collection = "test-collection"
        prov._graph_boost = MagicMock(return_value=[])
        return prov, client

    def test_rerank_off_single_fetch(self):
        prov, client = self._provider({"enabled": False})
        out = prov._recall("fallback routing", limit=3)
        _, kwargs = client.query_points.call_args
        assert kwargs["limit"] == 3  # no pool expansion when disabled
        assert len(out) == 3

    def test_rerank_on_expands_pool_and_reorders(self, monkeypatch):
        import nexus_memory.reranker as rr

        monkeypatch.setattr(
            rr,
            "_rerank_local",
            lambda q, results: sorted(
                results,
                key=lambda r: ("fallback" in r["text"].lower()) * 1,
                reverse=True,
            ),
        )
        prov, client = self._provider(
            {"enabled": True, "reranker": "cross-encoder", "pool_k": 5}
        )
        out = prov._recall("fallback routing", limit=2)
        _, kwargs = client.query_points.call_args
        assert kwargs["limit"] == 5  # pool_k beats limit
        # p2/p3 both match 2 query terms; stable order keeps original seq.
        assert [r["id"] for r in out[:2]] == ["p2", "p3"]

    def test_config_cached_after_first_recall(self):
        prov, _ = self._provider({"enabled": False})
        prov._rerank_cfg = None
        loader = MagicMock(return_value={"enabled": False})
        with patch("nexus_memory.reranker.load_rerank_config", loader):
            prov._recall("q1", limit=1)
            prov._recall("q2", limit=1)
        assert loader.call_count == 1  # cached after first call