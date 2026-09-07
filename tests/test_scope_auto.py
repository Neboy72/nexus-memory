"""Tests for scope_auto — self-organizing memory areas (full automation).

Nebo law 07.09.: the memory must organize itself; no user ever types a scope.
Conservative inference: clear match + clear margin, else 'default' (fail-open).
"""

import importlib
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
scope_auto = importlib.import_module("nexus_memory.scope_auto")


def _unit(n: int, dims: int = 4) -> list[float]:
    v = [0.0] * dims
    v[n % dims] = 1.0
    return v


class TestInferScope:
    def test_empty_centroids_defaults(self):
        assert scope_auto.infer_scope([1.0, 0.0, 0.0, 0.0], {}) == "default"

    def test_empty_vector_defaults(self):
        cents = {"voice": _unit(0)}
        assert scope_auto.infer_scope([], cents) == "default"

    def test_clear_match_inherits(self):
        cents = {"voice": _unit(0), "openclaw-maint": _unit(1)}
        # query exactly on the 'voice' centroid
        assert scope_auto.infer_scope(_unit(0), cents) == "voice"

    def test_no_match_defaults(self):
        cents = {"voice": _unit(0)}
        # orthogonal query → similarity 0 → below threshold
        assert scope_auto.infer_scope(_unit(1), cents) == "default"

    def test_ambiguous_defaults(self):
        # two centroids both near the query → margin too small → default
        cents = {"a": [0.99, 0.0, 0.1, 0.0], "b": [0.99, 0.1, 0.0, 0.0]}
        q = [0.99, 0.05, 0.05, 0.0]
        assert scope_auto.infer_scope(q, cents) == "default"

    def test_margin_respected(self):
        cents = {"voice": _unit(0), "openclaw-maint": _unit(1)}
        # query near voice but with slight lean → margin > 0.05 required
        q = [1.0, 0.03, 0.0, 0.0]
        s_voice = scope_auto._cosine(q, cents["voice"])
        s_open = scope_auto._cosine(q, cents["openclaw-maint"])
        if s_voice - s_open >= scope_auto.SCOPE_MARGIN and s_voice >= scope_auto.SCOPE_MATCH_THRESHOLD:
            assert scope_auto.infer_scope(q, cents) == "voice"
        else:
            assert scope_auto.infer_scope(q, cents) == "default"


class TestCentroids:
    class FakePoint:
        def __init__(self, payload, vector):
            self.payload = payload
            self.vector = vector

    class FakeClient:
        def __init__(self, points):
            self._points = points

        def scroll(self, **kwargs):
            return self._points, None

    def test_centroid_from_scoped_points(self):
        pts = [
            SimpleNamespace(payload={"scope": "voice", "lifecycle_status": "canonical"}, vector=_unit(0)),
            SimpleNamespace(payload={"scope": "voice", "lifecycle_status": "canonical"}, vector=_unit(0)),
            SimpleNamespace(payload={"scope": "default", "lifecycle_status": "canonical"}, vector=_unit(1)),
        ]
        c = scope_auto.ScopeCentroids(self.FakeClient(pts), "nexus")
        # direct fetch call (no cache):
        cents = c._fetch()
        assert "voice" in cents and "default" not in cents
        assert abs(cents["voice"][0] - 1.0) < 1e-6

    def test_fetch_failure_failopen(self):
        class Broken:
            def scroll(self, **_):
                raise RuntimeError("qdrant down")
        c = scope_auto.ScopeCentroids(Broken(), "nexus")
        assert c.get() == {}  # no centroids → no filtering anywhere

    def test_mixed_dims_skipped_safely(self):
        pts = [
            SimpleNamespace(payload={"scope": "voice", "lifecycle_status": "canonical"}, vector=[1.0, 0.0]),
            SimpleNamespace(payload={"scope": "voice", "lifecycle_status": "canonical"}, vector=[1.0, 0.0, 0.0, 0.0]),
        ]
        c = scope_auto.ScopeCentroids(self.FakeClient(pts), "nexus")
        cents = c._fetch()
        assert "voice" in cents  # same-dim pair counted; odd-dim skipped


class TestPrefetchFilterScopes:
    def test_no_match_returns_none(self):
        assert scope_auto.prefetch_filter_scopes(_unit(3), {"voice": _unit(0)}, "") is None

    def test_clear_match_returns_default_plus_scope(self):
        got = scope_auto.prefetch_filter_scopes(_unit(0), {"voice": _unit(0)}, "")
        assert got == {"default", "voice"}

    def test_own_scope_included(self):
        got = scope_auto.prefetch_filter_scopes(_unit(0), {"voice": _unit(0)}, "openclaw-maint")
        assert got == {"default", "voice", "openclaw-maint"}


class TestMcpServerIntegration:
    @pytest.mark.asyncio
    async def test_remember_auto_scope_inherits(self, monkeypatch):
        from nexus_memory import mcp_server as mcp

        class FakeClient:
            def __init__(self):
                self.pts = []
            def upsert(self, **kw):
                self.pts.append(kw)
            def query_points(self, **kw):
                class R:
                    points = []
                return R()
            def set_payload(self, **_):
                pass

        store = object.__new__(mcp.MemoryStore)
        store.client = FakeClient()  # type: ignore[assignment]
        store._skill_graph = None
        # Pre-seed centroids so the auto-inference has an area to find:
        store._scope_centroids = scope_auto.ScopeCentroids(None, "nexus")
        store._scope_centroids._cache = {"voice": _unit(0)}
        store._scope_centroids._cache_at = float("inf")  # never refresh

        async def fake_embed(self, text):
            return _unit(0)
        monkeypatch.setattr(mcp.MemoryStore, "_embed", fake_embed)

        result = await store.remember("Voice server tuning fact", category="fact")
        assert result["scope"] == "voice"  # auto-tagged, caller never passed scope

    @pytest.mark.asyncio
    async def test_explicit_scope_not_overridden(self, monkeypatch):
        from nexus_memory import mcp_server as mcp

        class FakeClient:
            def upsert(self, **_): pass
            def query_points(self, **kw):
                class R:
                    points = []
                return R()
            def set_payload(self, **_):
                pass

        store = object.__new__(mcp.MemoryStore)
        store.client = FakeClient()  # type: ignore[assignment]
        store._skill_graph = None
        store._scope_centroids = scope_auto.ScopeCentroids(None, "nexus")
        store._scope_centroids._cache = {"voice": _unit(0)}
        store._scope_centroids._cache_at = float("inf")

        async def fake_embed(self, text):
            return _unit(0)
        monkeypatch.setattr(mcp.MemoryStore, "_embed", fake_embed)

        result = await store.remember("Explicitly scoped fact", category="fact", scope="openclaw-maint")
        assert result["scope"] == "openclaw-maint"  # explicit wins, auto never overrides

    @pytest.mark.asyncio
    async def test_no_scopes_in_system_behaves_old(self, monkeypatch):
        """Invariant 1: zero scope areas in the system = old behavior."""
        from nexus_memory import mcp_server as mcp

        class FakeClient:
            def upsert(self, **_): pass
            def query_points(self, **kw):
                class R:
                    points = []
                return R()
            def set_payload(self, **_):
                pass

        store = object.__new__(mcp.MemoryStore)
        store.client = FakeClient()  # type: ignore[assignment]
        store._skill_graph = None
        store._scope_centroids = scope_auto.ScopeCentroids(None, "nexus")
        store._scope_centroids._cache = {}  # no scopes anywhere
        store._scope_centroids._cache_at = float("inf")

        async def fake_embed(self, text):
            return _unit(0)
        monkeypatch.setattr(mcp.MemoryStore, "_embed", fake_embed)

        result = await store.remember("Plain fact in a fresh install", category="fact")
        assert result["scope"] == "default"