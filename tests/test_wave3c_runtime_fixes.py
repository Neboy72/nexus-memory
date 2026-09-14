"""Wave 3c (Päckchen 3) runtime fixes — regression tests.

Covers the 12 findings from the runtime review of
embeddings / migrate-collections / plugin-edge / hooks.json:

- E1  ``_backend`` is set on both sentence-transformers success paths.
- E2  Drift-guard matches untagged names against tagged Ollama inventory.
- E3  ``NEXUS_ALLOWED_CLOUD_FALLBACK`` honours a comma-separated provider
      whitelist (a non-empty value no longer allows *any* cloud provider).
- M1  Textless points are never collapsed onto sha256("").
- M2  ID collisions across source collections no longer silently overwrite.
- M3  ``upsert_points`` counts failures and ``main()`` exits non-zero.
- P1  ``_scope_centroids`` exists on fresh plugin instances (prefetch path).
- P2  ``EdgeStore`` receives an ``http://`` URL.
- C1  Hook matcher + ``destructive_tools`` agree on the real tool names.
- C2  PreToolUse hook timeout is large enough for the guardrail check.
- N1  ``nexus_update`` raises on non-2xx HTTP responses.
- N2  Short point ids still use the id-based scroll filter.

Everything is mocked — no network, no real Qdrant.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import sys
import threading
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC = str(_REPO_ROOT / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import nexus  # noqa: E402
import nexus_memory.embeddings as embeddings_module  # noqa: E402
from nexus_memory.embeddings import (  # noqa: E402
    EmbeddingProvider,
    _allowed_cloud_fallback,
    _model_in_inventory,
)

# Hermes plugin loaded straight from file (avoids the top-level ``nexus`` clash).
_PLUGIN_PATH = _REPO_ROOT / "plugins" / "memory" / "nexus" / "__init__.py"
_spec = importlib.util.spec_from_file_location("nexus_hermes_plugin_wave3c", str(_PLUGIN_PATH))
_nexus_plugin = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_nexus_plugin)
NexusMemoryProvider = _nexus_plugin.NexusMemoryProvider

_PLUGIN_DIR = _REPO_ROOT / "plugins"
_HOOKS_JSON = _PLUGIN_DIR / "claude-code" / "hooks" / "nexus-hooks.json"
_GUARDRAIL_PY = _PLUGIN_DIR / "claude-code" / "scripts" / "guardrail_check.py"


def _load_migrate():
    """Load scripts/migrate-collections.py as a module (hyphenated name)."""
    path = _REPO_ROOT / "scripts" / "migrate-collections.py"
    spec = importlib.util.spec_from_file_location("nexus_migrate_collections_wave3c", str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _fake_st_module(dim: int):
    """Install-able fake ``sentence_transformers`` module (no torch/HF)."""
    mod = types.ModuleType("sentence_transformers")

    class SentenceTransformer:
        def __init__(self, name):
            self.name = name

        def encode(self, text):
            return [0.0] * dim

    mod.SentenceTransformer = SentenceTransformer
    return mod


# ===========================================================================
# E1 — _backend on the sentence-transformers paths
# ===========================================================================


class TestE1SentenceTransformersBackend:
    def test_minilm_path_sets_backend(self, monkeypatch):
        monkeypatch.delenv("NEXUS_HF_BGE3", raising=False)
        monkeypatch.setitem(sys.modules, "sentence_transformers", _fake_st_module(384))
        ep = EmbeddingProvider(preferred="local")
        assert ep.name == "all-MiniLM-L6-v2"
        assert ep.backend == "sentence-transformers"
        assert ep.provider_type == "local"

    def test_bge_m3_path_sets_backend(self, monkeypatch):
        monkeypatch.setenv("NEXUS_HF_BGE3", "1")
        monkeypatch.setitem(sys.modules, "sentence_transformers", _fake_st_module(1024))
        ep = EmbeddingProvider(preferred="sentence-transformers")
        assert ep.name == "BAAI/bge-m3"
        assert ep.dim == 1024
        assert ep.backend == "sentence-transformers"


# ===========================================================================
# E2 — drift-guard inventory matching (tag normalization)
# ===========================================================================


class TestE2ModelInventoryMatching:
    def test_untagged_name_matches_tagged_inventory(self):
        assert _model_in_inventory("bge-m3", ["bge-m3:latest"]) is True

    def test_tagged_name_matches_untagged_inventory(self):
        assert _model_in_inventory("bge-m3:latest", ["bge-m3"]) is True

    def test_different_tag_is_not_a_match(self):
        assert _model_in_inventory("qwen3-embedding:0.6b", ["qwen3-embedding:8b"]) is False

    def test_absent_model_is_not_a_match(self):
        assert _model_in_inventory("bge-m3", ["nomic-embed-text:latest"]) is False


# ===========================================================================
# E3 — NEXUS_ALLOWED_CLOUD_FALLBACK whitelist semantics
# ===========================================================================


class TestE3CloudFallbackWhitelist:
    def test_whitelist_allows_listed_provider(self, monkeypatch):
        monkeypatch.setenv("NEXUS_ALLOWED_CLOUD_FALLBACK", "voyage")
        assert _allowed_cloud_fallback("voyage") is True

    def test_whitelist_rejects_unlisted_provider(self, monkeypatch):
        monkeypatch.setenv("NEXUS_ALLOWED_CLOUD_FALLBACK", "voyage")
        assert _allowed_cloud_fallback("openai") is False

    def test_comma_separated_whitelist(self, monkeypatch):
        monkeypatch.setenv("NEXUS_ALLOWED_CLOUD_FALLBACK", "jina, openai")
        assert _allowed_cloud_fallback("openai") is True
        assert _allowed_cloud_fallback("jina") is True
        assert _allowed_cloud_fallback("voyage") is False

    @pytest.mark.parametrize("value", ["1", "true", "yes"])
    def test_truthy_value_allows_any_provider(self, value, monkeypatch):
        monkeypatch.setenv("NEXUS_ALLOWED_CLOUD_FALLBACK", value)
        assert _allowed_cloud_fallback("openai") is True
        assert _allowed_cloud_fallback("voyage") is True

    def test_unset_disallows(self, monkeypatch):
        monkeypatch.delenv("NEXUS_ALLOWED_CLOUD_FALLBACK", raising=False)
        assert _allowed_cloud_fallback("voyage") is False

    def test_provider_not_in_whitelist_fails_closed(self, monkeypatch):
        """Integration: a whitelist that omits the preferred provider must not
        open the fallback door for that provider."""
        monkeypatch.setattr(embeddings_module, "VOYAGE_API_KEY", "")
        monkeypatch.setenv("NEXUS_ALLOWED_CLOUD_FALLBACK", "openai")
        with pytest.raises(RuntimeError):
            EmbeddingProvider(preferred="voyage")

    def test_provider_in_whitelist_allows_fallback(self, monkeypatch):
        monkeypatch.setattr(embeddings_module, "VOYAGE_API_KEY", "")
        monkeypatch.setenv("NEXUS_ALLOWED_CLOUD_FALLBACK", "voyage")
        # Auto-detect runs; with everything blocked it lands on MiniLM.
        monkeypatch.setattr(embeddings_module, "OPENAI_API_KEY", "")
        monkeypatch.setattr(embeddings_module, "GOOGLE_API_KEY", "")
        monkeypatch.delenv("JINA_API_KEY", raising=False)
        monkeypatch.setattr(
            "requests.get",
            lambda *a, **k: (_ for _ in ()).throw(ConnectionError("no ollama")),
        )
        monkeypatch.setitem(sys.modules, "sentence_transformers", _fake_st_module(384))
        ep = EmbeddingProvider(preferred="voyage")
        assert ep.name == "all-MiniLM-L6-v2"


# ===========================================================================
# M1 / M2 / M3 — migrate-collections.py
# ===========================================================================


class TestM1TextlessPoints:
    def test_two_textless_points_stay_two(self):
        mod = _load_migrate()
        pts = [
            {"id": "a", "payload": {"_source_collection": "hermes-memory"}},
            {"id": "b", "payload": {"_source_collection": "hermes-memory"}},
        ]
        out = mod.deduplicate(pts)
        assert len(out) == 2

    def test_same_text_still_deduplicates(self):
        mod = _load_migrate()
        pts = [
            {"id": "a", "payload": {"text": "same"}},
            {"id": "b", "payload": {"text": "same"}},
        ]
        assert len(mod.deduplicate(pts)) == 1

    def test_text_and_textless_do_not_collide(self):
        mod = _load_migrate()
        pts = [
            {"id": "a", "payload": {}},
            {"id": "b", "payload": {"text": ""}},
            {"id": "c", "payload": {"text": "real"}},
        ]
        assert len(mod.deduplicate(pts)) == 3


class TestM2IdCollision:
    def test_cross_collection_collision_keeps_both(self):
        mod = _load_migrate()
        all_points: dict = {}
        p1 = {"id": "same-id", "payload": {"_source_collection": "hermes-memory", "content": "x"}}
        p2 = {"id": "same-id", "payload": {"_source_collection": "openclaw-memory", "content": "y"}}
        assert mod.merge_points(all_points, {"k1": p1}) == 1
        assert mod.merge_points(all_points, {"k2": p2}) == 1
        assert len(all_points) == 2
        ids = sorted(str(p["id"]) for p in all_points.values())
        assert ids == ["same-id", "same-id"]
        migrated = [p for p in all_points.values()
                    if p["payload"].get("_migrated_from") == "openclaw-memory"]
        assert len(migrated) == 1

    def test_same_collection_duplicate_id_skipped(self):
        mod = _load_migrate()
        all_points: dict = {}
        p1 = {"id": "same-id", "payload": {"_source_collection": "hermes-memory"}}
        p2 = {"id": "same-id", "payload": {"_source_collection": "hermes-memory"}}
        assert mod.merge_points(all_points, {"k1": p1}) == 1
        assert mod.merge_points(all_points, {"k2": p2}) == 0
        assert len(all_points) == 1


class TestM3UpsertFailures:
    def test_upsert_points_returns_zero_when_all_batches_fail(self, monkeypatch):
        mod = _load_migrate()
        calls = {"n": 0}

        def boom(*a, **k):
            calls["n"] += 1
            raise RuntimeError("qdrant down")

        monkeypatch.setattr(mod, "qdrant_request", boom)
        pts = [{"id": i, "vector": [], "payload": {}} for i in range(mod.BATCH_SIZE * 2)]
        assert mod.upsert_points(pts) == 0
        assert calls["n"] == 2

    def test_upsert_points_counts_successes(self, monkeypatch):
        mod = _load_migrate()
        monkeypatch.setattr(mod, "qdrant_request", lambda *a, **k: {"status": "ok"})
        pts = [{"id": i, "vector": [], "payload": {}} for i in range(mod.BATCH_SIZE + 1)]
        assert mod.upsert_points(pts) == len(pts)

    def test_main_exits_nonzero_on_incomplete_migration(self, monkeypatch):
        mod = _load_migrate()
        monkeypatch.setattr(mod, "get_collection_info", lambda name: {"points_count": 1})
        by_collection = {
            "hermes-memory": [{"id": "p1", "payload": {"text": "a"}, "vector": []}],
            "openclaw-memory": [{"id": "p2", "payload": {"text": "b"}, "vector": []}],
        }
        monkeypatch.setattr(mod, "scroll_all_points", lambda col: list(by_collection[col]))
        monkeypatch.setattr(mod, "upsert_points", lambda pts, dry_run=False: 1)
        monkeypatch.setattr(mod.time, "sleep", lambda s: None)
        monkeypatch.setattr(sys, "argv", ["migrate-collections.py"])
        with pytest.raises(SystemExit) as exc:
            mod.main()
        assert exc.value.code == 1


# ===========================================================================
# P1 / P2 — Hermes plugin
# ===========================================================================


class TestP1ScopeCentroids:
    def test_fresh_instance_has_attribute(self):
        p = NexusMemoryProvider()
        assert hasattr(p, "_scope_centroids")
        assert p._scope_centroids is None

    def test_prefetch_reaches_scope_path_without_attribute_error(self, monkeypatch):
        p = NexusMemoryProvider()
        created = {}

        class _FakeCentroids:
            def __init__(self, client, collection):
                created["args"] = (client, collection)

            def get(self):
                return {}

        import nexus_memory.scope_auto as sa
        monkeypatch.setattr(sa, "ScopeCentroids", _FakeCentroids)
        monkeypatch.setattr(sa, "prefetch_filter_scopes", lambda vec, cents, scope: None)

        p._embedder = MagicMock()
        p._embed_cached = lambda q: [0.0] * 4
        p._rewrite_if_enabled = lambda q: q
        p._graph_boost = lambda *a, **k: []
        p._qdrant = MagicMock()
        p._qdrant.query_points.return_value = SimpleNamespace(points=[])

        p._do_prefetch("irgendeine frage")

        # Reaching the ScopeCentroids branch proves no AttributeError was
        # swallowed by the fail-open except-clause.
        assert created["args"] == (p._qdrant, p._collection)
        assert isinstance(p._scope_centroids, _FakeCentroids)


class TestP2EdgeStoreUrl:
    def test_edgestore_receives_http_url(self, monkeypatch):
        import nexus_memory.entity_extractor as ee
        import nexus.graph.store as gs

        p = NexusMemoryProvider()
        p._collection = "c"
        p._hermes_home = ""
        p._write_stop = threading.Event()
        p._qdrant = MagicMock()
        p._upsert_entity = lambda e, **kw: {"id": "ent-" + e.name}

        captured = {}

        class _FakeEdgeStore:
            def __init__(self, **kw):
                captured.update(kw)

            def add_edge(self, **kw):
                pass

            def close(self):
                pass

        fake = SimpleNamespace(
            entities=[ee.Entity("A", "device", {}), ee.Entity("B", "device", {})],
            relationships=[ee.Relationship("A", "B", "connected_to")],
        )
        fake.is_empty = lambda: False

        monkeypatch.setattr(gs, "EdgeStore", _FakeEdgeStore)
        monkeypatch.setattr(
            "nexus_memory.entity_extractor.extract_entities",
            lambda t, hermes_home=None: fake,
        )

        p._extract_entities_from_text("A connects to B via connected_to.")

        assert captured["qdrant_url"].startswith("http")


# ===========================================================================
# C1 / C2 — Claude Code hooks
# ===========================================================================


class TestC1HookToolMatcher:
    _EXPECTED = ["Bash", "Write", "Edit", "MultiEdit", "NotebookEdit"]

    def test_pretooluse_matcher_uses_real_tool_names(self):
        hooks = json.loads(_HOOKS_JSON.read_text())
        matcher = hooks["hooks"]["PreToolUse"][0]["matcher"]
        assert matcher == "Bash|Write|Edit|MultiEdit|NotebookEdit"
        assert "Terminal" not in matcher and "Delete" not in matcher

    def test_guardrail_script_lists_same_tools(self):
        tree = ast.parse(_GUARDRAIL_PY.read_text())
        found = None
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == "destructive_tools":
                        found = [e.value for e in node.value.elts]
        assert found == self._EXPECTED

    def test_matcher_and_script_agree(self):
        hooks = json.loads(_HOOKS_JSON.read_text())
        matcher = hooks["hooks"]["PreToolUse"][0]["matcher"].split("|")
        assert matcher == self._EXPECTED


class TestC2HookTimeout:
    def test_pretooluse_timeout_is_15s(self):
        hooks = json.loads(_HOOKS_JSON.read_text())
        assert hooks["hooks"]["PreToolUse"][0]["hooks"][0]["timeout"] == 15


# ===========================================================================
# N1 / N2 — root nexus_update()
# ===========================================================================


def _resp(status_code, payload=None, text=""):
    r = MagicMock()
    r.status_code = status_code
    r.text = text
    r.json.return_value = payload if payload is not None else {}
    return r


class TestN1NexusUpdateStatusChecks:
    def test_raises_on_scroll_http_error(self, monkeypatch):
        monkeypatch.setattr("requests.post", lambda *a, **k: _resp(500, {}, "boom"))
        with pytest.raises(RuntimeError, match="scroll failed"):
            nexus.nexus_update("abcdefgh", new_content="x", collection_name="c")

    def test_raises_on_point_lookup_http_error(self, monkeypatch):
        monkeypatch.setattr(
            "requests.post", lambda *a, **k: _resp(200, {"result": {"points": []}})
        )
        monkeypatch.setattr("requests.get", lambda *a, **k: _resp(404, {}, "nf"))
        with pytest.raises(RuntimeError, match="lookup failed"):
            nexus.nexus_update("abcdefgh", new_content="x", collection_name="c")

    def test_raises_on_upsert_http_error(self, monkeypatch):
        point = {"id": "abcdefgh", "payload": {"content": "old"}, "vector": [0.1]}
        monkeypatch.setattr(
            "requests.post",
            lambda *a, **k: _resp(200, {"result": {"points": [point]}}),
        )
        monkeypatch.setattr("requests.put", lambda *a, **k: _resp(500, {}, "boom"))
        with pytest.raises(RuntimeError, match="upsert failed"):
            nexus.nexus_update("abcdefgh", new_content="x", collection_name="c")

    def test_success_path_returns_json(self, monkeypatch):
        point = {"id": "abcdefgh", "payload": {"content": "old"}, "vector": [0.1]}
        monkeypatch.setattr(
            "requests.post",
            lambda *a, **k: _resp(200, {"result": {"points": [point]}}),
        )
        monkeypatch.setattr(
            "requests.put", lambda *a, **k: _resp(200, {"result": {"status": "ok"}})
        )
        out = nexus.nexus_update("abcdefgh", new_content="new", collection_name="c")
        assert out == {"result": {"status": "ok"}}


class TestN2ShortIdFilter:
    def test_short_id_still_uses_id_filter(self, monkeypatch):
        captured = {}

        def fake_post(url, json=None, timeout=None, **kwargs):
            captured["body"] = json
            return _resp(
                200,
                {"result": {"points": [
                    {"id": "short-id", "payload": {"content": "old"}, "vector": [0.1]}
                ]}},
            )

        monkeypatch.setattr("requests.post", fake_post)
        monkeypatch.setattr(
            "requests.put", lambda *a, **k: _resp(200, {"result": {"status": "ok"}})
        )
        nexus.nexus_update("short-id", new_content="new", collection_name="c")

        body = captured["body"]
        assert "filter" in body, "ids <= 20 chars must still use the id filter"
        assert body["filter"]["must"][0]["match"]["value"] == "short-id"
        assert body["limit"] == 1

    def test_long_id_uses_id_filter(self, monkeypatch):
        captured = {}

        def fake_post(url, json=None, timeout=None, **kwargs):
            captured["body"] = json
            return _resp(200, {"result": {"points": []}})

        monkeypatch.setattr("requests.post", fake_post)
        monkeypatch.setattr("requests.get", lambda *a, **k: _resp(404, {}, "nf"))
        long_id = "a" * 36
        with pytest.raises(RuntimeError, match="lookup failed"):
            nexus.nexus_update(long_id, new_content="x", collection_name="c")
        assert captured["body"]["filter"]["must"][0]["match"]["value"] == long_id
