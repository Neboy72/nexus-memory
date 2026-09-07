"""Tests for scope (project/agent areas, unreleased feature).

Core principle (Nebo 07.09.): scope labels steer AUTOMATIC prefetch only —
explicit recall() is NEVER scope-filtered. Fail-open everywhere: invalid
values degrade to 'default'; missing NEXUS_SCOPE means old behavior.
"""
import os
import sys
import types
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest


# ── _normalize_scope ──────────────────────────────────────────────

class TestNormalizeScope:
    def test_valid_scope_kept(self):
        from nexus_memory.mcp_server import _normalize_scope
        assert _normalize_scope("nexus-project") == "nexus-project"

    def test_uppercase_and_whitespace_normalized(self):
        from nexus_memory.mcp_server import _normalize_scope
        assert _normalize_scope("  Nexus-Project ") == "nexus-project"

    def test_empty_string_degrades_to_default(self):
        from nexus_memory.mcp_server import _normalize_scope
        assert _normalize_scope("") == "default"
        assert _normalize_scope("   ") == "default"

    def test_none_degrades_to_default(self):
        from nexus_memory.mcp_server import _normalize_scope
        assert _normalize_scope(None) == "default"

    def test_non_string_degrades_to_default(self):
        from nexus_memory.mcp_server import _normalize_scope
        assert _normalize_scope(123) == "default"

    def test_too_long_degrades_to_default(self):
        from nexus_memory.mcp_server import _normalize_scope
        assert _normalize_scope("a" * 41) == "default"
        assert _normalize_scope("a" * 40) == "a" * 40

    def test_invalid_chars_degrade_to_default(self):
        from nexus_memory.mcp_server import _normalize_scope
        assert _normalize_scope("voice/de") == "default"
        assert _normalize_scope("haushalt_2") == "default"  # underscore not allowed
        assert _normalize_scope("-leading-dash") == "default"


# ── remember() stores scope in payload ────────────────────────────

class TestRememberStoresScope:
    @pytest.mark.asyncio
    async def test_scope_lands_in_payload(self, monkeypatch):
        from nexus_memory import mcp_server as mcp
        captured = {}

        class FakeClient:
            def upsert(self, collection_name, points):
                captured["payload"] = points[0].payload

            def query_points(self, **kw):  # auto-supersession probe
                class R: points = []
                return R()

        store = object.__new__(mcp.MemoryStore)
        store.client = FakeClient()  # type: ignore[assignment]
        store._skill_graph = None  # scope tests don't exercise the graph

        async def fake_embed(self, text):
            return [0.0] * 8
        monkeypatch.setattr(mcp.MemoryStore, "_embed", fake_embed)

        await store.remember("test fact", "public", "fact", scope="voice")
        assert captured["payload"]["scope"] == "voice"

    @pytest.mark.asyncio
    async def test_invalid_scope_stored_as_default(self, monkeypatch):
        from nexus_memory import mcp_server as mcp
        captured = {}

        class FakeClient:
            def upsert(self, collection_name, points):
                captured["payload"] = points[0].payload

            def query_points(self, **kw):
                class R: points = []
                return R()

        store = object.__new__(mcp.MemoryStore)
        store.client = FakeClient()  # type: ignore[assignment]
        store._skill_graph = None  # scope tests don't exercise the graph

        async def fake_embed(self, text):
            return [0.0] * 8
        monkeypatch.setattr(mcp.MemoryStore, "_embed", fake_embed)

        await store.remember("test fact", "public", "fact", scope="BAD SCOPE!")
        assert captured["payload"]["scope"] == "default"

    @pytest.mark.asyncio
    async def test_no_scope_arg_stores_default(self, monkeypatch):
        from nexus_memory import mcp_server as mcp
        captured = {}

        class FakeClient:
            def upsert(self, collection_name, points):
                captured["payload"] = points[0].payload

            def query_points(self, **kw):
                class R: points = []
                return R()

        store = object.__new__(mcp.MemoryStore)
        store.client = FakeClient()  # type: ignore[assignment]
        store._skill_graph = None  # scope tests don't exercise the graph

        async def fake_embed(self, text):
            return [0.0] * 8
        monkeypatch.setattr(mcp.MemoryStore, "_embed", fake_embed)

        await store.remember("test fact", "public", "fact")
        assert captured["payload"]["scope"] == "default"


# ── Plugin prefetch scope gating (Nebo's acceptance scenario) ────

class _FakePoint:
    def __init__(self, payload, score=0.9):
        self.payload = payload
        self.score = score


class _FakeQdrant:
    def __init__(self, points):
        self._points = points

    def query_points(self, collection_name, query, limit):
        class R:
            points = self._points
        return R()


def _make_plugin(monkeypatch, points, my_scope):
    """Build a bare NexusMemoryProvider-like object for _do_prefetch."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "nexus_plugin_scope_test",
        os.path.join(os.path.dirname(__file__), "..", "plugins", "memory", "nexus", "__init__.py"),
    )
    plug = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("nexus_plugin_scope_test", plug)
    spec.loader.exec_module(plug)

    monkeypatch.setenv("NEXUS_SCOPE", my_scope)
    monkeypatch.setenv("NEXUS_PREFETCH_CHARS", "2400")

    prov = object.__new__(plug.NexusMemoryProvider)
    prov._prefetch_result = ""
    prov._prefetch_lock = __import__("threading").Lock()
    prov._collection = "nexus"  # scope tests don't exercise collection resolution

    class FakeEmbedder:
        def embed_cached(self, text):
            return [0.0] * 8

        def embed(self, text):
            return [0.0] * 8
    prov._embedder = FakeEmbedder()
    prov._qdrant = _FakeQdrant(points)
    prov._graph_boost = lambda pts, max_boost=3, max_depth=2: []
    return prov


class TestPrefetchScopeGating:
    def test_scoped_agent_does_not_see_foreign_scope(self, monkeypatch):
        pts = [
            _FakePoint({"content": "nexus project fact", "category": "fact",
                        "scope": "nexus-project"}),
            _FakePoint({"content": "general fact", "category": "fact",
                        "scope": "default"}),
        ]
        prov = _make_plugin(monkeypatch, pts, my_scope="openclaw-maint")
        prov._do_prefetch("any query")
        with prov._prefetch_lock:
            result = prov._prefetch_result
        assert "general fact" in result
        assert "nexus project fact" not in result

    def test_scoped_agent_sees_own_scope(self, monkeypatch):
        pts = [
            _FakePoint({"content": "own area fact", "category": "fact",
                        "scope": "openclaw-maint"}),
        ]
        prov = _make_plugin(monkeypatch, pts, my_scope="openclaw-maint")
        prov._do_prefetch("any query")
        with prov._prefetch_lock:
            result = prov._prefetch_result
        assert "own area fact" in result

    def test_no_scope_env_sees_everything_fail_open(self, monkeypatch):
        pts = [
            _FakePoint({"content": "scoped fact", "category": "fact",
                        "scope": "voice"}),
            _FakePoint({"content": "general fact", "category": "fact",
                        "scope": "default"}),
        ]
        prov = _make_plugin(monkeypatch, pts, my_scope="")  # no NEXUS_SCOPE
        prov._do_prefetch("any query")
        with prov._prefetch_lock:
            result = prov._prefetch_result
        assert "scoped fact" in result and "general fact" in result

    def test_unscoped_old_memory_treated_as_default(self, monkeypatch):
        # Pre-scope memories have NO scope field — must behave as 'default'.
        pts = [
            _FakePoint({"content": "old memory without scope field", "category": "fact"}),
        ]
        prov = _make_plugin(monkeypatch, pts, my_scope="openclaw-maint")
        prov._do_prefetch("any query")
        with prov._prefetch_lock:
            result = prov._prefetch_result
        assert "old memory without scope field" in result