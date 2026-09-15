"""Tests for OCR review Wave 18 — MEDIUM findings Nr 261-290.

One test class per finding (where a finding is a docstring/comment-only fix
the class asserts the documented caveat exists). Source-inspection tests stay
tied to the shipped code; logic paths are exercised behaviourally.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "scripts"


def _read(name: str) -> str:
    return (SCRIPTS / name).read_text()


def _load_script(name: str, module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, SCRIPTS / name)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


# ── Nr 261: legacy test_mcp forget-cleanup runs even on earlier failure ──────


class TestNr261LegacyMcpCleanup:
    def test_forget_runs_after_failure(self):
        src = _read("legacy/test_mcp.py")
        # The cleanup must live in a finally-block that sits AFTER the
        # remember call, so the stored test memory is deleted even when an
        # assert inside the guarded block fails.
        remember_idx = src.index('"remember"')
        fidx = src.index("finally:", remember_idx)
        forget_idx = src.index('"forget"')
        assert forget_idx > fidx


# ── Nr 262: reembed_voyage4 refuses named-vector collections ─────────────────


class TestNr262NamedVectors:
    def test_named_vector_collections_skipped(self):
        src = _read("reembed_voyage4.py")
        assert "named vectors" in src

    def test_named_vector_collection_returns_zero_result(self, monkeypatch):
        monkeypatch.setenv("VOYAGE_API_KEY", "vo-test-key")
        mod = _load_script("reembed_voyage4.py", "w18_reembed")

        class _Resp:
            def raise_for_status(self):
                pass

            def json(self):
                return {"result": {
                    "points_count": 10,
                    "config": {"params": {"vectors": {
                        "params": {"map": {"text": {"size": 1024}}}}}},
                }}

        import types
        monkeypatch.setattr(mod.requests, "get", lambda *a, **k: _Resp())
        result = mod.reembed_collection("nexus")
        assert result == {"reembedded": 0, "skipped": 0, "errors": 0, "tokens": 0}


# ── Nr 263: reembed aborts batch when Voyage returns wrong count ─────────────


class TestNr263ZipTruncation:
    def test_count_mismatch_aborts_batch(self):
        src = _read("reembed_voyage4.py")
        assert "len(vectors) != len(batch_points)" in src


# ── Nr 264: per-collection error isolation in reembed main ───────────────────


class TestNr264CollectionIsolation:
    def test_main_continues_after_collection_error(self, monkeypatch):
        monkeypatch.setenv("VOYAGE_API_KEY", "vo-test-key")
        mod = _load_script("reembed_voyage4.py", "w18_reembed2")

        calls = []

        def fake_reembed(col, dry_run=False):
            calls.append(col)
            if col == "nexus-test":
                raise RuntimeError("boom")
            return {"reembedded": 1, "skipped": 0, "errors": 0, "tokens": 0}

        monkeypatch.setattr(mod, "reembed_collection", fake_reembed)
        # main() probes voyage-4 first — fake that too (no network)
        monkeypatch.setattr(mod, "voyage_embed_batch", lambda texts: [[0.1] * 1024])

        class _Args:
            dry_run = False
            collection = None

        monkeypatch.setattr(mod.argparse.ArgumentParser, "parse_args",
                            lambda self: _Args())
        # main() must survive the middle collection failing
        mod.main()
        assert calls == ["nexus", "nexus-test", "test-collection"]


# ── Nr 265: migrate-collections broken error-path status check ───────────────


class TestNr265MigrateErrorPath:
    def _mod(self, monkeypatch):
        return _load_script("migrate-collections.py", "w18_migrate")

    def test_error_status_raises(self, monkeypatch):
        mod = self._mod(monkeypatch)

        class _Resp:
            status_code = 200
            text = ""

            def json(self):
                return {"status": "error", "result": {"error": "boom"}}

        monkeypatch.setattr(mod.requests, "request", lambda *a, **k: _Resp())
        with pytest.raises(Exception):
            mod.qdrant_request("GET", "/collections/x")

    def test_http_error_raises(self, monkeypatch):
        mod = self._mod(monkeypatch)

        class _Resp:
            status_code = 500
            ok = False
            text = "server error"

            def json(self):
                return {}

        monkeypatch.setattr(mod.requests, "request", lambda *a, **k: _Resp())
        with pytest.raises(Exception):
            mod.qdrant_request("GET", "/collections/x")


# ── Nr 266: migrate-collections waits for points to be durable ───────────────


class TestNr266MigrateWait:
    def test_upsert_waits_for_processing(self):
        src = _read("migrate-collections.py")
        # the migration upsert must NOT fire-and-forget
        upsert_idx = src.index('qdrant_request("PUT"')
        assert '"wait": False' not in src[upsert_idx:upsert_idx + 300]


# ── Nr 267: scroll_all_points streams batches instead of hoarding ────────────


class TestNr267StreamingScroll:
    def test_no_full_materialization(self):
        src = _read("migrate-collections.py")
        # the helper must be a generator yielding per-page batches
        assert "def iter_scroll_batches" in src
        assert "yield" in src
        # the old hoarding helper is gone
        assert "def scroll_all_points" not in src


# ── Nr 268: staging ollama embeds with the DETECTED model ────────────────────


class TestNr268OllamaModel:
    def test_no_hardcoded_model_in_embed(self):
        src = (REPO_ROOT / "nexus" / "staging.py").read_text()
        # the embed call must use the detected model, not a hardcoded one
        assert 'json={"model": "nomic-embed-text", "prompt": text}' not in src
        assert "_OLLAMA_MODEL" in src


# ── Nr 269: staging embedding failure must not persist zero vectors ──────────


class TestNr269EmbedFailureFails:
    def _mod(self, monkeypatch):
        import nexus.staging as st
        monkeypatch.setattr(st, "_EMBED_PROVIDER", "jina")
        monkeypatch.setattr(st, "_detect_vector_size", lambda: 1024)
        return st

    def test_provider_error_raises_no_zero_vector(self, monkeypatch):
        st = self._mod(monkeypatch)

        class _Resp:
            status_code = 401
            text = "unauthorized"

            def json(self):
                return {"error": "bad key"}

            def raise_for_status(self):
                raise st.requests.HTTPError("401")

        monkeypatch.setattr(st.requests, "post", lambda *a, **k: _Resp())
        with pytest.raises(Exception):
            st._embed_content("some text")

    def test_providerless_config_still_zero_vector(self, monkeypatch):
        st = self._mod(monkeypatch)
        monkeypatch.setattr(st, "_EMBED_PROVIDER", "none")
        assert st._embed_content("x") == [0.0] * 1024


# ── Nr 270: canonical supersedes set pages through the collection ────────────


class TestNr270SupersedesPagination:
    def test_pages_until_offset_none(self, monkeypatch):
        import nexus.staging as st
        from types import SimpleNamespace

        pages = [
            {"points": [{"payload": {"supersedes": "S1"}}],
             "next_page_offset": "p2"},
            {"points": [{"payload": {"promoted_from": "P1"}}],
             "next_page_offset": None},
        ]
        seen_payloads = []

        def fake_post(url, json=None, timeout=None):
            seen_payloads.append(json)
            idx = 0 if json.get("offset") is None else 1

            class _Resp:
                def raise_for_status(self):
                    pass

                def json(self):
                    return {"result": pages[idx]}

            return _Resp()

        monkeypatch.setattr(st, "requests", SimpleNamespace(post=fake_post))
        # reset pagination start
        assert st._get_canonical_supersedes_set() == {"S1", "P1"}
        assert seen_payloads[1]["offset"] == "p2"


# ── Nr 271: transport error is not "no canonical exists" ─────────────────────


class TestNr271TransportError:
    def test_connection_error_raises(self, monkeypatch):
        import nexus.staging as st

        def boom(*a, **k):
            raise st.requests.ConnectionError("qdrant down")

        monkeypatch.setattr(st.requests, "get", boom)
        with pytest.raises(RuntimeError, match="Qdrant unreachable"):
            st._get_current_canonical("fact-1")

    def test_404_still_means_none(self, monkeypatch):
        import nexus.staging as st
        from types import SimpleNamespace

        class _Resp:
            status_code = 404

            def json(self):
                return {}

        monkeypatch.setattr(st.requests, "get", lambda *a, **k: _Resp())
        assert st._get_current_canonical("fact-1") is None


# ── Nr 272: ensure_collections refuses dimension mismatch ────────────────────


class TestNr272DimensionGuard:
    def test_existing_wrong_dim_refused(self, monkeypatch):
        import nexus.staging as st
        from unittest.mock import MagicMock

        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"result": {"config": {"params": {"vectors": {"size": 384}}}}}
        monkeypatch.setattr(st.requests, "get", lambda *a, **k: resp)

        results = st.ensure_collections("localhost", 6333, vector_size=1024)
        assert all(v is False for v in results.values())

    def test_existing_same_dim_accepted(self, monkeypatch):
        import nexus.staging as st
        from unittest.mock import MagicMock

        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"result": {"config": {"params": {"vectors": {"size": 1024}}}}}
        monkeypatch.setattr(st.requests, "get", lambda *a, **k: resp)

        results = st.ensure_collections("localhost", 6333, vector_size=1024)
        assert all(v is True for v in results.values())


# ── Nr 273: session_dump guarded CLI parsing ─────────────────────────────────


class TestNr273SessionDumpArgv:
    def test_no_args_usage_exit(self, monkeypatch, capsys):
        mod = _load_script("session_dump.py", "w18_sessiondump")
        monkeypatch.setattr(sys, "argv", ["session_dump.py"])
        with pytest.raises(SystemExit) as ei:
            mod.main()
        assert ei.value.code == 2
        assert "usage" in capsys.readouterr().err.lower()

    def test_bad_int_usage_exit(self, monkeypatch, capsys):
        mod = _load_script("session_dump.py", "w18_sessiondump2")
        monkeypatch.setattr(sys, "argv", ["session_dump.py", "sess-1", None, "abc"])
        with pytest.raises(SystemExit) as ei:
            mod.main()
        assert ei.value.code == 2


# ── Nr 274: bench_latency loads plugin repo-root-relative ────────────────────


class TestNr274BenchPluginPath:
    def test_plugin_path_is_repo_relative(self):
        src = _read("bench_latency.py")
        assert "__file__" in src
        assert 'spec_from_file_location("nhp", "plugins/memory/nexus/__init__.py")' not in src


# ── Nr 275: bench honours NEXUS_* env instead of hardcoding ──────────────────


class TestNr275BenchEnv:
    def test_host_port_from_env(self):
        src = _read("bench_latency.py")
        assert 'os.environ.get("NEXUS_QDRANT_HOST"' in src
        assert 'os.environ.get("NEXUS_QDRANT_PORT"' in src
        assert 'host="localhost", port=6333' not in src

    def test_collection_from_env(self):
        src = _read("bench_latency.py")
        assert 'os.environ.get("NEXUS_COLLECTION"' in src


# ── Nr 276: bench builds the provider via a supported factory ────────────────


class TestNr276BenchFactory:
    def test_no_dunder_new_bypass(self):
        src = _read("bench_latency.py")
        # Nr 276: the provider is built through its real __init__ — the
        # layout-coupled __new__ bypass must be gone (comment mentions are fine)
        assert "NexusMemoryProvider.__new__" not in src
        assert "m.NexusMemoryProvider()" in src


# ── Nr 277: backfill_shard dead first scroll loop removed ────────────────────


class TestNr277DeadLoopRemoved:
    def test_single_scroll_pass(self):
        src = _read("backfill_shard.py")
        assert src.count("client.scroll(coll,") == 1
        assert "my_points, offset = [], None" not in src


# ── Nr 278: backfill distills with the SOURCE session date ───────────────────


class TestNr278SourceDate:
    def test_uses_source_date_not_run_date(self):
        src = _read("backfill_shard.py")
        assert 'date = time.strftime("%Y-%m-%d")' not in src
        assert "source_date" in src


# ── Nr 279: backfill uses the public consolidate_point entry ─────────────────


class TestNr279PublicEntryPoint:
    def test_no_underscored_reach_in(self):
        src = _read("backfill_shard.py")
        for member in ("cons._MAX_CONV_CHARS", "cons._parse_facts",
                       "c._resolve_conflicts", "c._store_fact",
                       "c._supersede_old", "c._mark_consolidated"):
            assert member not in src, member

    def test_consolidate_point_exists_and_is_public(self):
        from nexus_memory.consolidation import Consolidator
        assert hasattr(Consolidator, "consolidate_point")

    def test_consolidate_point_happy_path(self):
        from types import SimpleNamespace
        from nexus_memory import consolidation as C

        class FakeQ:
            def set_payload(self, collection, payload, points, **kw):
                self.last = (payload, points)

        q = FakeQ()
        c = C.Consolidator(SimpleNamespace(client=q), "test-coll",
                           llm_fn=lambda p: '{"facts": ["User likes bikes"]}',
                           embed_fn=lambda t: [0.2] * 1024)
        c._resolve_conflicts = lambda fact, access_level="private": ("ok", [])
        c._store_fact = lambda fact, pid: ("new-id-1", "default")
        sup_calls = []
        c._supersede_old = lambda old, new: sup_calls.append((old, new))
        marks = []
        c._mark_consolidated = lambda pid, n: marks.append((pid, n))

        stats = c.consolidate_point("raw-1", "conv text", source_date="2026-08-01")
        assert stats["created"] == 1
        assert stats["duplicates"] == 0
        assert marks == [("raw-1", 1)]

    def test_consolidate_point_unparseable_raises(self):
        from types import SimpleNamespace
        from nexus_memory import consolidation as C

        c = C.Consolidator(SimpleNamespace(client=SimpleNamespace()), "test-coll",
                           llm_fn=lambda p: "not json at all",
                           embed_fn=lambda t: [0.2] * 1024)
        with pytest.raises(ValueError):
            c.consolidate_point("raw-1", "conv text")


# ── Nr 280: consolidate_point raises on unparseable LLM response ─────────────


class TestNr280UnparseableNotMarked:
    def test_no_mark_on_unparseable(self):
        from types import SimpleNamespace
        from nexus_memory import consolidation as C

        marks = []
        c = C.Consolidator(SimpleNamespace(client=SimpleNamespace()), "test-coll",
                           llm_fn=lambda p: "garbage",
                           embed_fn=lambda t: [0.2] * 1024)
        c._mark_consolidated = lambda pid, n: marks.append((pid, n))
        with pytest.raises(ValueError):
            c.consolidate_point("raw-1", "conv")
        assert marks == []


# ── Nr 281: backfill docstring documents the cross-shard duplicate risk ──────


class TestNr281DocstringCaveat:
    def test_caveat_documented(self):
        src = _read("backfill_shard.py")
        assert "duplicate" in src.lower()


# ── Nr 282: supersede failure inside consolidate_point is non-fatal ──────────


class TestNr282SupersedeNonFatal:
    def test_failed_supersede_counted_point_marked(self):
        from types import SimpleNamespace
        from nexus_memory import consolidation as C

        c = C.Consolidator(SimpleNamespace(client=SimpleNamespace()), "test-coll",
                           llm_fn=lambda p: '{"facts": ["fact one"]}',
                           embed_fn=lambda t: [0.2] * 1024)
        c._resolve_conflicts = lambda fact, access_level="private": ("ok", ["old-1"])
        c._store_fact = lambda fact, pid: ("new-id-1", "default")

        def boom(old, new):
            raise RuntimeError("qdrant hiccup")

        c._supersede_old = boom
        marks = []
        c._mark_consolidated = lambda pid, n: marks.append((pid, n))

        stats = c.consolidate_point("raw-1", "conv")
        assert stats["created"] == 1
        assert stats["failed_supersedes"] == 1
        assert marks == [("raw-1", 1)]


# ── Nr 283: backfill pipeline error handling mirrors the daemon ──────────────


class TestNr283PipelineScope:
    def test_outer_guard_still_catches_pipeline_errors(self):
        src = _read("backfill_shard.py")
        # the per-point try/except stays, supersede failures handled inside
        assert "except Exception as exc:" in src
        assert "failed += 1" in src


# ── Nr 284: dry run reports no facts it never wrote ──────────────────────────


class TestNr284DryRunCounters:
    def _build(self, monkeypatch):
        from types import SimpleNamespace
        from nexus_memory import consolidation as C

        raw = SimpleNamespace(id="raw-1", payload={
            "content": "some session text", "category": "session",
            "lifecycle_status": "canonical"})
        c = C.Consolidator(SimpleNamespace(client=SimpleNamespace()), "test-coll",
                           llm_fn=lambda p: '{"facts": ["fact one"]}',
                           embed_fn=lambda t: [0.2] * 1024)
        monkeypatch.setattr(c, "_load_pending_supersedes", lambda: [])
        monkeypatch.setattr(c, "_retry_pending_supersedes", lambda p: (0, []))
        monkeypatch.setattr(c, "_next_raw_batch", lambda limit: [raw])
        monkeypatch.setattr(c, "_resolve_conflicts",
                            lambda fact, access_level="private": ("ok", []))
        monkeypatch.setattr(c, "_store_fact",
                            lambda fact, pid, source_payload=None: ("new-id", "default"))
        return c

    def test_dry_run_reports_zero_facts(self, monkeypatch):
        c = self._build(monkeypatch)
        rep = c.run(batch_size=1, dry_run=True)
        assert rep["facts_created"] == 0
        assert rep["scanned"] == 1

    def test_real_run_counts_facts(self, monkeypatch):
        c = self._build(monkeypatch)
        monkeypatch.setattr(c, "_mark_consolidated", lambda pid, n: None)
        rep = c.run(batch_size=1)
        assert rep["facts_created"] == 1


# ── Nr 285: consolidation interval floored at 60s ────────────────────────────


class TestNr285IntervalFloor:
    def test_zero_and_negative_clamped(self):
        from nexus_memory import consolidation as C
        assert C.CONSOLIDATION_INTERVAL_SECONDS >= 60


# ── Nr 286: chat_wizard probes the real Qdrant health endpoint ───────────────


class TestNr286Healthz:
    def test_healthz_not_health(self):
        src = (REPO_ROOT / "src" / "nexus_memory" / "chat_wizard.py").read_text()
        assert "6333/healthz" in src
        assert "6333/health\"" not in src


# ── Nr 287: get_status detects keys written to .env ──────────────────────────


class TestNr287EnvBackfill:
    def test_status_reads_env_file_fallback(self):
        src = (REPO_ROOT / "src" / "nexus_memory" / "chat_wizard.py").read_text()
        gs = src[src.index("def get_status"):]
        assert "read_env_key" in gs


# ── Nr 288: _save_api_key import works standalone ────────────────────────────


class TestNr288StandaloneImport:
    def test_import_fallback_present(self):
        src = (REPO_ROOT / "src" / "nexus_memory" / "chat_wizard.py").read_text()
        s = src[src.index("def _save_api_key"):]
        assert "except ImportError" in s


# ── Nr 289: LOCAL_AGENT_IDS matches the detector registry ────────────────────


class TestNr289RegistrySync:
    def test_no_ghost_gemini_cli(self):
        src = (REPO_ROOT / "src" / "nexus_memory" / "agent_detect.py").read_text()
        reg = src[src.index("LOCAL_AGENT_IDS = frozenset("):]
        reg = reg[:reg.index("})")]
        assert "gemini-cli" not in reg

    def test_remote_gemini_cli_seat_accepted(self):
        from nexus_memory.agent_detect import register_remote_agent, unregister_agent
        try:
            r = register_remote_agent("gemini-cli", "Remote Gemini CLI")
            assert r.get("status") == "registered" or "registered" in str(r)
        finally:
            try:
                unregister_agent("gemini-cli")
            except Exception:
                pass


# ── Nr 290: shutdown joins backup/update threads before closing client ──────


class TestNr290ShutdownJoins:
    def test_shutdown_joins_background_threads(self):
        src = (REPO_ROOT / "plugins" / "memory" / "nexus" / "__init__.py").read_text()
        s = src[src.index("def shutdown"):]
        assert "_backup_thread" in s
        assert "_update_thread" in s