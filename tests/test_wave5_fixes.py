"""Wave 5 (Päckchen 5 / group F) fixes — regression tests.

Covers the F1–F7 HIGH findings:

- F1 ``knowledge_gaps`` default threshold contradicted its docstring: ``1/(1+1)=0.5``
  can never reach the old default ``0.9``, so degree-1 facts were never reported.
- F2 ``GroundingScorer._signal_factual`` matched entities as raw substrings
  ("rag" in "storage", "ppo" in "support") → false positives.
- F3 ``nexus/cli.py`` imported ``nexus.config`` at module level, before
  ``_ensure_path()`` had run → ``ModuleNotFoundError`` on direct execution.
- F4 dashboard ``--host`` defaulted to ``0.0.0.0`` (unauthenticated exposure).
- F5 blocking urllib scroll ran directly on the FastAPI event loop.
- F6 auto-discovery classified the relation fact→hit but stored it sorted,
  inverting the direction whenever ``hit_id < fact_id``.
- F7 ``export_skill`` joined an unvalidated ``name`` into filesystem paths.

No network, no import side effects (dashboard/server.py is inspected via AST).
"""

from __future__ import annotations

import ast
import importlib
import subprocess
import sys
from pathlib import Path

import networkx as nx
import pytest

from nexus.analytics.scoring import knowledge_gaps
from nexus.confidence import GroundingScorer
from nexus.export import _safe_skill_name, export_skill

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SERVER_PY = _REPO_ROOT / "dashboard" / "server.py"
_CLI_PY = _REPO_ROOT / "nexus" / "cli.py"


# ===========================================================================
# F1 — degree-1 facts are knowledge gaps at the default threshold
# ===========================================================================


class _GraphStub:
    """Minimal ``SkillGraph`` stand-in — scoring only touches ``_graph``."""

    def __init__(self, graph):  # noqa: ANN001
        self._graph = graph


def _graph_with_degree_one():
    """degree0 (isolated), degree1 (single edge), hub (degree 3)."""
    g = nx.DiGraph()
    g.add_nodes_from(["f-degree0", "f-degree1", "f-hub"])
    g.add_edge("f-degree1", "f-hub")
    g.add_edge("f-hub", "f-x")
    g.add_edge("f-hub", "f-y")
    return _GraphStub(g), "f-degree1"


class TestF1KnowledgeGapThreshold:
    def _graph_with_degree_one(self):  # noqa: ANN202
        return _graph_with_degree_one()

    def test_degree_one_is_reported_with_default(self):
        sg, degree_one = self._graph_with_degree_one()
        gaps = knowledge_gaps(sg)
        ids = [g["fact_id"] for g in gaps]
        assert degree_one in ids, "degree-1 fact must be a gap at the default threshold"

    def test_degree_zero_is_reported_with_default(self):
        sg, _ = self._graph_with_degree_one()
        ids = [g["fact_id"] for g in knowledge_gaps(sg)]
        assert "f-degree0" in ids

    def test_scores_match_docstring(self):
        """score(0)=1.0, score(1)=0.5 — both ≥ default 0.5."""
        sg, _ = self._graph_with_degree_one()
        by_id = {g["fact_id"]: g["isolation_score"] for g in knowledge_gaps(sg)}
        assert by_id["f-degree0"] == 1.0
        assert by_id["f-degree1"] == 0.5

    def test_well_connected_fact_not_reported(self):
        sg, _ = self._graph_with_degree_one()
        ids = [g["fact_id"] for g in knowledge_gaps(sg)]
        assert "f-hub" not in ids

    def test_old_threshold_still_available(self):
        """Passing 0.9 explicitly keeps the old degree-0-only behaviour."""
        sg, degree_one = self._graph_with_degree_one()
        ids = [g["fact_id"] for g in knowledge_gaps(sg, isolation_threshold=0.9)]
        assert degree_one not in ids


# ===========================================================================
# F2 — word-boundary entity matching
# ===========================================================================


class TestF2EntityWordBoundaries:
    def test_substring_rag_in_storage_is_not_an_entity(self):
        # "rag" would be found by the old raw `in` check → 0.0; correct is
        # "no technical entity at all" → neutral 1.0.
        assert GroundingScorer._signal_factual("storage layer", ["unrelated text"]) == 1.0

    def test_substring_ppo_in_support_is_not_an_entity(self):
        assert GroundingScorer._signal_factual("support ticket", ["unrelated text"]) == 1.0

    def test_substring_sft_in_sftp_is_not_an_entity(self):
        assert GroundingScorer._signal_factual("sftp upload", ["unrelated text"]) == 1.0

    def test_standalone_entity_present_in_chunk_matches(self):
        assert GroundingScorer._signal_factual(
            "we use rag here", ["rag improves answers"]
        ) == 1.0

    def test_standalone_entity_absent_from_chunk_scores_zero(self):
        assert GroundingScorer._signal_factual(
            "we use qdrant", ["rag improves answers"]
        ) == 0.0

    def test_entity_inside_a_chunk_word_does_not_match(self):
        """Chunk side needs word boundaries too: "rag" ⊄ "storage"."""
        assert GroundingScorer._signal_factual("we use rag", ["storage backend"]) == 0.0

    def test_partial_match_across_analysis(self):
        """One of two entities matching → 0.5, not full credit."""
        score = GroundingScorer._signal_factual(
            "qdrant and gpt", ["qdrant handles vectors"]
        )
        assert score == 0.5


# ===========================================================================
# F3 — nexus/cli.py is importable without a sys.path trick
# ===========================================================================


class TestF3CliImport:
    def test_module_imports_cleanly(self):
        mod = importlib.import_module("nexus.cli")
        assert hasattr(mod, "main")

    def test_no_module_level_nexus_config_import(self):
        tree = ast.parse(_CLI_PY.read_text(encoding="utf-8"))
        for node in tree.body:  # module-level statements only
            if isinstance(node, ast.ImportFrom) and node.module == "nexus.config":
                pytest.fail(
                    "nexus/cli.py imports nexus.config at module level — this "
                    "runs before _ensure_path() and breaks `python nexus/cli.py`"
                )
            if isinstance(node, ast.Import):
                assert all(a.name != "nexus.config" for a in node.names)

    def test_direct_execution_does_not_raise_module_not_found(self):
        proc = subprocess.run(
            [sys.executable, str(_CLI_PY), "--help"],
            cwd=str(_REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=60,
        )
        combined = proc.stdout + proc.stderr
        assert "ModuleNotFoundError" not in combined
        assert "No module named 'nexus'" not in combined


# ===========================================================================
# F4 — dashboard binds loopback by default
# ===========================================================================


def _host_arg_default() -> object:
    tree = ast.parse(_SERVER_PY.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "add_argument"):
            continue
        if not (node.args and isinstance(node.args[0], ast.Constant)):
            continue
        if node.args[0].value != "--host":
            continue
        for kw in node.keywords:
            if kw.arg == "default" and isinstance(kw.value, ast.Constant):
                return kw.value.value
    pytest.fail("could not locate the --host argument in dashboard/server.py")


class TestF4DashboardHostDefault:
    def test_default_host_is_loopback(self):
        assert _host_arg_default() == "127.0.0.1"

    def test_default_is_not_wildcard(self):
        assert _host_arg_default() != "0.0.0.0"


# ===========================================================================
# F5 — blocking scroll is offloaded to a thread
# ===========================================================================


def _server_tree() -> ast.Module:
    return ast.parse(_SERVER_PY.read_text(encoding="utf-8"))


def _async_func(name: str) -> ast.AsyncFunctionDef:
    for node in ast.walk(_server_tree()):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
            return node
    pytest.fail(f"async function {name} not found in dashboard/server.py")


def _uses_to_thread(func: ast.AsyncFunctionDef) -> bool:
    for node in ast.walk(func):
        if not isinstance(node, ast.Await):
            continue
        call = node.value
        if not isinstance(call, ast.Call):
            continue
        func_name = call.func
        if isinstance(func_name, ast.Attribute) and func_name.attr == "to_thread":
            return True
        if isinstance(func_name, ast.Name) and func_name.id == "to_thread":
            return True
    return False


class TestF5ScrollOffEventLoop:
    @pytest.mark.parametrize("handler", ["get_memory_stats", "get_memories", "get_full_stats"])
    def test_scroll_handler_uses_to_thread(self, handler):
        assert _uses_to_thread(_async_func(handler)), (
            f"{handler} calls the blocking _scroll_all_memories on the event loop"
        )

    def test_asyncio_imported_at_module_level(self):
        tree = _server_tree()
        for node in tree.body:
            if isinstance(node, ast.Import):
                if any(a.name == "asyncio" for a in node.names):
                    return
        pytest.fail("dashboard/server.py must import asyncio at module level")


# ===========================================================================
# F6 — discovery classifies and stores in the same (sorted) direction
# ===========================================================================


def _make_discovery(monkeypatch, hits):
    import nexus.discovery as disc

    monkeypatch.setattr(disc, "search_similar_facts", lambda **kw: hits)
    captured: list[dict] = []

    def fake_classify(**kw):
        captured.append(kw)
        return {"relation": "references", "confidence": 0.9, "reason": "test"}

    monkeypatch.setattr(disc, "classify_relation", fake_classify)
    ad = disc.AutoDiscovery(collection="test-collection")
    return ad, captured


class TestF6DiscoveryDirection:
    def test_discover_for_fact_sorts_before_classify(self, monkeypatch):
        """hit_id < fact_id → hit must become the classified source."""
        hits = [{"id": "aaa", "payload": {"content": "hit body", "category": "hc"}, "score": 0.9}]
        ad, captured = _make_discovery(monkeypatch, hits)

        result = ad.discover_for_fact("zzz", "fact body", "fc", [1.0])

        assert len(captured) == 1
        call = captured[0]
        assert call["source_id"] == "aaa"
        assert call["target_id"] == "zzz"
        assert call["source_content"] == "hit body"
        assert call["target_content"] == "fact body"
        assert result[0]["source"] == "aaa"
        assert result[0]["target"] == "zzz"

    def test_discover_for_fact_content_direction_matches_ids(self, monkeypatch):
        """When fact_id < hit_id the fact stays the source."""
        hits = [{"id": "zzz", "payload": {"content": "hit body", "category": "hc"}, "score": 0.9}]
        ad, captured = _make_discovery(monkeypatch, hits)

        result = ad.discover_for_fact("aaa", "fact body", "fc", [1.0])

        call = captured[0]
        assert call["source_id"] == "aaa"
        assert call["source_content"] == "fact body"
        assert call["target_content"] == "hit body"
        assert result[0]["source"] == "aaa"
        assert result[0]["target"] == "zzz"

    def test_discover_all_sorts_before_classify(self, monkeypatch):
        import nexus.discovery as disc

        facts = [{"id": "zzz", "payload": {"content": "fact body", "category": "fc"}, "vector": [1.0]}]
        monkeypatch.setattr(disc, "scroll_facts", lambda **kw: facts)
        monkeypatch.setattr(disc, "filter_new_edges", lambda candidates, store: [])
        hits = [{"id": "aaa", "payload": {"content": "hit body", "category": "hc"}, "score": 0.9}]
        ad, captured = _make_discovery(monkeypatch, hits)

        summary = ad.discover_all()

        assert summary["candidates_found"] == 1
        assert len(captured) == 1
        call = captured[0]
        assert call["source_id"] == "aaa"
        assert call["target_id"] == "zzz"
        assert call["source_content"] == "hit body"
        assert call["target_content"] == "fact body"

    def test_candidate_relation_direction_stable(self, monkeypatch):
        """A relation classified source→target is stored source→target."""
        import nexus.discovery as disc

        facts = [{"id": "zzz", "payload": {"content": "fact body", "category": "fc"}, "vector": [1.0]}]
        monkeypatch.setattr(disc, "scroll_facts", lambda **kw: facts)
        captured_candidates: list[dict] = []
        monkeypatch.setattr(
            disc, "filter_new_edges",
            lambda candidates, store: captured_candidates.extend(candidates) or [],
        )
        hits = [{"id": "aaa", "payload": {"content": "hit body", "category": "hc"}, "score": 0.9}]
        ad, _ = _make_discovery(monkeypatch, hits)

        ad.discover_all()

        assert captured_candidates[0]["source"] == "aaa"
        assert captured_candidates[0]["target"] == "zzz"


# ===========================================================================
# F7 — skill name is validated before any path join
# ===========================================================================


class TestF7SafeSkillName:
    @pytest.mark.parametrize("bad", ["../..", "/etc/x", "a b", "..", ".", "", "a/b", "a\\b", "x" * 65])
    def test_unsafe_names_raise(self, bad):
        with pytest.raises(ValueError):
            _safe_skill_name(bad)

    @pytest.mark.parametrize("good", ["code-review", "nexus.export", "a_b-1", "A" * 64])
    def test_safe_names_pass(self, good):
        assert _safe_skill_name(good) == good

    @pytest.mark.parametrize("bad", ["../..", "/etc/x", "a b"])
    def test_export_skill_rejects_before_any_io(self, bad, monkeypatch):
        # Even if search/network is reachable, export_skill must refuse first.
        import nexus.export as export_mod

        def _boom(*a, **kw):
            pytest.fail("search_knowledge ran before name validation")

        monkeypatch.setattr(export_mod, "search_knowledge", _boom)
        with pytest.raises(ValueError):
            export_skill(bad)

    def test_valid_name_writes_inside_output_dir(self, tmp_path, monkeypatch):
        import nexus.export as export_mod

        monkeypatch.setattr(export_mod, "search_knowledge", lambda *a, **kw: [])
        result = export_skill("code-review", output_dir=str(tmp_path))

        out = Path(result["output_path"]).resolve()
        assert out.parent == tmp_path.resolve()
        assert out.name == "code-review.md"

    def test_traversal_cannot_escape_output_dir(self, tmp_path, monkeypatch):
        import nexus.export as export_mod

        monkeypatch.setattr(export_mod, "search_knowledge", lambda *a, **kw: [])
        with pytest.raises(ValueError):
            export_skill("../escape", output_dir=str(tmp_path))
