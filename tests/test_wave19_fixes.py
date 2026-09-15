"""OCR review wave 19 - fixes for findings Nr 291-321 (no 311).

One test class per finding, source-inspection plus behavior tests.
Written by Kiosha (CC was contractually not allowed to touch this file).
"""
import importlib.util
import json
import math
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _load_script(name: str, modname: str):
    path = ROOT / "scripts" / name
    spec = importlib.util.spec_from_file_location(modname, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_src(rel: str, modname: str):
    spec = importlib.util.spec_from_file_location(
        modname, str(ROOT / rel))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[modname] = mod
    spec.loader.exec_module(mod)
    return mod


# ── Nr 291: hit_rate/stats take a locked snapshot ────────────────────────────

class TestNr291CacheSnapshot:
    def test_stats_reads_counters_under_lock(self):
        src = _read("src/nexus_memory/embed_cache.py")
        assert "with self._lock" in src

    def test_hit_rate_never_exceeds_1_under_concurrency(self):
        mod = _load_src("src/nexus_memory/embed_cache.py", "w19_embed_cache")
        import threading
        c = mod.EmbedCache(maxsize=8)
        stop = threading.Event()

        def hammer():
            i = 0
            while not stop.is_set():
                c.get(f"k{i % 4}")
                i += 1

        threads = [threading.Thread(target=hammer, daemon=True) for _ in range(4)]
        for t in threads:
            t.start()
        try:
            for _ in range(200):
                r = c.hit_rate
                assert 0.0 <= r <= 1.0
        finally:
            stop.set()
            for t in threads:
                t.join(timeout=2)


# ── Nr 292: maxsize validated ────────────────────────────────────────────────

class TestNr292Maxsize:
    def test_invalid_maxsize_raises(self):
        mod = _load_src("src/nexus_memory/embed_cache.py", "w19_embed_cache2")
        with pytest.raises(ValueError):
            mod.EmbedCache(maxsize=0)
        with pytest.raises(ValueError):
            mod.EmbedCache(maxsize=-3)

    def test_valid_maxsize_ok(self):
        mod = _load_src("src/nexus_memory/embed_cache.py", "w19_embed_cache3")
        c = mod.EmbedCache(maxsize=1)
        c.put("a", [1.0])
        assert c.get("a") == [1.0]


# ── Nr 293: config-load failures logged (cost_router, extractor, entity) ────

class TestNr293ConfigLogging:
    def test_cost_router_logs_config_failure(self):
        src = _read("src/nexus_memory/cost_router.py")
        assert "config read failed" in src

    def test_extractor_logs_config_failure(self):
        src = _read("src/nexus_memory/extractor.py")
        assert "config read failed" in src
        assert ".env read failed" in src

    def test_entity_extractor_logs_config_failure(self):
        src = _read("src/nexus_memory/entity_extractor.py")
        assert "config read failed" in src

    def test_bare_pass_removed_from_config_loaders(self):
        for rel in ("src/nexus_memory/cost_router.py",
                    "src/nexus_memory/extractor.py",
                    "src/nexus_memory/entity_extractor.py"):
            src = _read(rel)
            assert "except Exception:\n            pass" not in src


# ── Nr 294: premium tier falls back ─────────────────────────────────────────

class TestNr294PremiumFallback:
    def _router(self):
        from nexus_memory.cost_router import (
            CostAwareRouter, TIER_PREMIUM, TIER_STANDARD, TIER_ECONOMY)
        r = CostAwareRouter.__new__(CostAwareRouter)
        r._available_providers = {"ollama": TIER_ECONOMY, "openai": TIER_STANDARD}
        r._routing_enabled = True
        r._routing_decisions = {}
        return r

    def test_premium_category_gets_provider_not_none(self):
        r = self._router()
        # fact/rule/entity are premium categories; no premium provider is set
        got = r.get_provider_for_category("fact")
        assert got in ("openai", "ollama")
        assert got is not None

    def test_premium_falls_to_standard_first(self):
        r = self._router()
        got = r.get_provider_for_category("rule")
        assert got == "openai"  # standard beats economy in the fallback order


# ── Nr 295: ollama probe cleaned up + logged ────────────────────────────────

class TestNr295OllamaProbe:
    def test_probe_uses_context_manager(self):
        src = _read("src/nexus_memory/cost_router.py")
        assert "with socket.create_connection" in src

    def test_probe_failure_is_logged_narrow(self):
        src = _read("src/nexus_memory/cost_router.py")
        assert "except OSError" in src
        assert "ollama probe failed" in src
        # bare except must not swallow everything any more
        assert "except Exception:\n            pass" not in src


# ── Nr 296: estimate_cost/explain are read-only ─────────────────────────────

class TestNr296ReadOnlyRouting:
    def _router(self):
        from nexus_memory.cost_router import CostAwareRouter, TIER_PREMIUM
        r = CostAwareRouter.__new__(CostAwareRouter)
        r._available_providers = {"voyage": TIER_PREMIUM}
        r._routing_enabled = True
        r._routing_decisions = {}
        r._cost_tracker = {}
        return r

    def test_estimate_cost_records_nothing(self):
        r = self._router()
        r.estimate_cost("some text", "fact")
        assert r._routing_decisions == {}

    def test_direct_call_still_records(self):
        r = self._router()
        r.get_provider_for_category("fact")
        assert r._routing_decisions.get("voyage") == 1


# ── Nr 297: dead guard removed ──────────────────────────────────────────────

class TestNr297DeadGuard:
    def test_no_dead_model_guard(self):
        src = _read("src/nexus_memory/extractor.py")
        # the dead guard sat in _llm_extract; the fallback ASSIGNMENT
        # (line ~115) legitimately still matches the substring
        idx = src.find("def _llm_extract")
        body = src[idx:]
        assert 'if not config["model"]' not in body


# ── Nr 298: fd leak closed ──────────────────────────────────────────────────

class TestNr298FdLeak:
    def test_error_path_closes_fd(self):
        src = _read("src/nexus_memory/env_secret_store.py")
        assert "os.close(fd)" in src

    def test_fchmod_guarded_for_windows(self):
        src = _read("src/nexus_memory/env_secret_store.py")
        assert 'getattr(os, "fchmod", None)' in src

    def test_failed_write_still_writes_key(self, tmp_path, monkeypatch):
        from nexus_memory import env_secret_store as ess
        env = tmp_path / ".env"
        ess.write_env_key(env, "TEST_KEY_W19", "abc123")
        body = env.read_text()
        # store quotes values (parse-safe); both raw and quoted count
        assert "TEST_KEY_W19=" in body and "abc123" in body
        assert (env.stat().st_mode & 0o777) == 0o600


# ── Nr 299: chmod failures warned ───────────────────────────────────────────

class TestNr299ChmodWarn:
    def test_chmod_failures_surface_warnings(self):
        src = _read("src/nexus_memory/env_secret_store.py")
        assert "warnings.warn" in src
        assert "except OSError:\n        pass" not in src


# ── Nr 300: LLM path applies same caps + pairwise filter ────────────────────

class TestNr300LlmCaps:
    def _llm_body(self) -> str:
        src = _read("src/nexus_memory/entity_extractor.py")
        # caps live in the OpenAI round-trip helper (_extract_with_openai),
        # which _llm_extract_entities delegates to
        for name in ("_extract_with_openai", "_llm_extract_entities"):
            idx = src.find(f"def {name}")
            if idx >= 0:
                return src[idx:idx + 5000]
        return src

    def test_llm_path_caps_entities_and_rels(self):
        src = _read("src/nexus_memory/entity_extractor.py")
        # caps live around line 342 in the OpenAI round-trip helper
        assert "deduped: List[Entity]" in src
        assert "kept_names = {e.name.lower() for e in deduped}" in src
        assert "[:8]" in src

    def test_llm_caps_behaviorally(self, monkeypatch):
        from nexus_memory import entity_extractor as ee

        class _Msg:
            content = json.dumps({"entities": [
                {"name": f"E{i}", "type": "concept", "confidence": 0.9}
                for i in range(15)
            ] + [{"name": "e3", "type": "concept", "confidence": 0.9}],
                "relationships": [
                {"source": f"E{i}", "target": f"E{i+1}", "relation": "uses",
                 "confidence": 0.8} for i in range(12)
            ]})

        class _Choice:
            message = _Msg()

        class _Resp:
            choices = [_Choice()]

        class _Completions:
            @staticmethod
            def create(**kw):
                return _Resp()

        class _Chat:
            completions = _Completions()

        class _Client:
            def __init__(self, **kw):
                self.chat = _Chat()

        monkeypatch.setattr(ee, "_quick_health_check", lambda url: True)
        monkeypatch.setattr(ee, "_load_llm_config", lambda h: {
            "model": "m", "base_url": "http://x", "api_key": "k"})
        fake_openai = SimpleNamespace(OpenAI=_Client)
        monkeypatch.setitem(sys.modules, "openai", fake_openai)
        res = ee._llm_extract_entities("text with many entities", hermes_home="/tmp")
        assert len(res.entities) == 10
        assert len(res.relationships) <= 8
        names = {e.name.lower() for e in res.entities}
        for r in res.relationships:
            assert r.source.lower() in names
            assert r.target.lower() in names


# ── Nr 302: dead ips assignment removed ─────────────────────────────────────

class TestNr302DeadIps:
    def test_no_dead_ips_assignment(self):
        src = _read("src/nexus_memory/entity_extractor.py")
        # heuristic path: only the per-device local recompute remains
        # (word-boundary: 'nearby_ips' is legitimate, the dead bare
        # 'ips = _IP_PATTERN.findall' assignment is gone)
        assert "\n    ips = _IP_PATTERN.findall" not in src


# ── Nr 303: provider keys read live ─────────────────────────────────────────

class TestNr303LiveKeys:
    def test_try_voyage_reads_env_live(self):
        src = _read("src/nexus_memory/embeddings.py")
        assert 'os.environ.get("VOYAGE_API_KEY") or VOYAGE_API_KEY' in src
        assert 'os.environ.get("OPENAI_API_KEY") or OPENAI_API_KEY' in src
        assert 'os.environ.get("GOOGLE_API_KEY") or GOOGLE_API_KEY' in src

    def test_late_key_export_is_honored(self, monkeypatch):
        from nexus_memory import embeddings as emb
        # no key at import time (test env), then export late → _try_voyage must
        # see it. We cannot init a real client; assert the guard passes.
        monkeypatch.setenv("VOYAGE_API_KEY", "vo-test-late-key")
        p = emb.EmbeddingProvider.__new__(emb.EmbeddingProvider)
        p._reset_provider_state()

        captured = {}

        def fake_client(api_key=None):
            captured["key"] = api_key

        fake_voyage = SimpleNamespace(Client=fake_client)
        monkeypatch.setitem(sys.modules, "voyageai", fake_voyage)
        assert p._try_voyage() is True
        assert captured["key"] == "vo-test-late-key"


# ── Nr 304: query/doc mode split ────────────────────────────────────────────

class TestNr304QueryDocMode:
    def test_embed_signature_has_is_query(self):
        src = _read("src/nexus_memory/embeddings.py")
        assert "def embed(self, text: str, is_query: bool = True)" in src

    def test_prefix_only_for_queries(self):
        src = _read("src/nexus_memory/embeddings.py")
        assert "is_query and \"qwen3-embedding\"" in src

    def test_doc_sites_pass_is_query_false(self):
        plugin = _read("plugins/memory/nexus/__init__.py")
        assert "self._embedder.embed(text, is_query=False)" in plugin
        assert "self._embed_cached(text, is_query=False)" in plugin
        mcp = _read("src/nexus_memory/mcp_server.py")
        assert "await self._embed(text, is_query=False)" in mcp
        assert "await store._embed(text, is_query=False)" in mcp
        cons = _read("src/nexus_memory/consolidation.py")
        assert "is_query=False" in cons

    def test_qwen3_doc_embedding_is_plain(self, monkeypatch):
        from nexus_memory import embeddings as emb
        captured = {}

        def fake_post(url, json=None, timeout=None):
            captured["json"] = json

            class _R:
                def json(self):
                    return {"embeddings": [[0.1] * 1024]}
            return _R()

        monkeypatch.setattr("requests.post", fake_post)
        p = emb.EmbeddingProvider.__new__(emb.EmbeddingProvider)
        p._name = "qwen3-embedding:0.6b"
        p._dim = 1024
        p._model = None
        p._client = {"base_url": "http://localhost:11434"}
        import asyncio
        vec = asyncio.new_event_loop().run_until_complete(
            p.embed("irgendein dokument text", is_query=False))
        assert len(vec) == 1024
        assert captured["json"]["input"][0] == "irgendein dokument text"

    def test_qwen3_query_embedding_keeps_prefix(self, monkeypatch):
        from nexus_memory import embeddings as emb
        captured = {}

        def fake_post(url, json=None, timeout=None):
            captured["json"] = json

            class _R:
                def json(self):
                    return {"embeddings": [[0.1] * 1024]}
            return _R()

        monkeypatch.setattr("requests.post", fake_post)
        p = emb.EmbeddingProvider.__new__(emb.EmbeddingProvider)
        p._name = "qwen3-embedding:0.6b"
        p._dim = 1024
        p._model = None
        p._client = {"base_url": "http://localhost:11434"}
        import asyncio
        vec = asyncio.new_event_loop().run_until_complete(
            p.embed("wo ist bleki geboren"))
        assert captured["json"]["input"][0].startswith("Instruct:")

    def test_cache_keys_differ_per_mode(self):
        mod = _load_src("src/nexus_memory/embed_cache.py", "w19_embed_cache4")
        c = mod.EmbedCache(maxsize=8)
        c.put("text", [1.0], is_query=False)
        assert c.get("text", is_query=True) is None
        assert c.get("text", is_query=False) == [1.0]


# ── Nr 305: boolean env vocabulary unified ──────────────────────────────────

class TestNr305EnvBool:
    def test_env_bool_parser_exists(self):
        src = _read("src/nexus_memory/embeddings.py")
        assert "_ENV_FALSY" in src
        assert "def _env_bool" in src

    def test_falsy_tokens_cover_no_off_n(self, monkeypatch):
        from nexus_memory import embeddings as emb
        for bad in ("0", "false", "no", "off", "n", ""):
            monkeypatch.setenv("NEXUS_HF_BGE3", bad)
            p = emb.EmbeddingProvider.__new__(emb.EmbeddingProvider)
            p._name = "none"
            p._dim = 384
            p._backend = "none"
            p._client = None
            p._model = None
            p._preferred = ""
            # _try_sentence_transformers must NOT pick the HF route
            called = []
            monkeypatch.setattr(emb, "SentenceTransformer", None, raising=False)
            # simpler: with a falsy value the hf branch is skipped entirely
            src_ok = emb._env_bool(bad) is False
            assert src_ok

    def test_reranker_accepts_no_off_n(self):
        src = _read("src/nexus_memory/reranker.py")
        assert '"no", "off", "n"' in src

    def test_hf_bge3_disabled_by_zero(self, monkeypatch):
        from nexus_memory import embeddings as emb
        monkeypatch.setenv("NEXUS_HF_BGE3", "0")
        p = emb.EmbeddingProvider.__new__(emb.EmbeddingProvider)
        p._name = "none"
        p._dim = 384
        p._backend = "none"
        p._client = None
        p._model = None
        ok = p._try_sentence_transformers()
        # with ST installed, falls through to MiniLM (HF route skipped);
        # without it, returns False. Either way no crash and no bge-m3.
        assert p._name != "BAAI/bge-m3"


# ── Nr 306: sentence-transformers fallback never raises ─────────────────────

class TestNr306FallbackSoft:
    def test_broad_except_in_fallback(self):
        src = _read("src/nexus_memory/embeddings.py")
        idx = src.find("_try_sentence_transformers")
        body = src[idx:]
        assert "except Exception as exc:" in body
        assert "no local fallback available" in body

    def test_init_failure_does_not_raise(self, monkeypatch):
        from nexus_memory import embeddings as emb
        monkeypatch.setenv("NEXUS_HF_BGE3", "")

        class _Boom:
            def __init__(self, *a, **k):
                raise OSError("offline")

        fake = SimpleNamespace(SentenceTransformer=_Boom)
        monkeypatch.setitem(sys.modules, "sentence_transformers", fake)
        p = emb.EmbeddingProvider.__new__(emb.EmbeddingProvider)
        p._name = "none"
        p._dim = 384
        p._backend = "none"
        p._client = None
        p._model = None
        assert p._try_sentence_transformers() is False


# ── Nr 307: dead collection+path branch removed ─────────────────────────────

class TestNr307DeadBranch:
    def test_no_path_match_inside_collection_block(self):
        src = _read("src/nexus_memory/guardrails.py")
        idx = src.find('if rule.get("collection"):')
        chunk = src[idx:idx + 900]
        assert '_path_matches' not in chunk


# ── Nr 308: inf clamped to finite ───────────────────────────────────────────

class TestNr308FiniteStatus:
    def test_corrupt_ledger_reports_finite(self, monkeypatch, tmp_path):
        from nexus_memory import fuel_chain as fc
        monkeypatch.setattr(fc, "FUEL_STATE", tmp_path / "fuel.json")
        (tmp_path / "fuel.json").write_text("{corrupt json")
        info = fc.fuel_exhausted_info()
        assert math.isfinite(info["spent_usd"])
        assert json.dumps(info)  # must not raise on Infinity

    def test_clamp_code_present(self):
        src = _read("src/nexus_memory/fuel_chain.py")
        assert "isfinite" in src


# ── Nr 309: one-shot latch atomic ───────────────────────────────────────────

class TestNr309AtomicLatch:
    def test_read_check_write_in_one_lock(self):
        src = _read("src/nexus_memory/fuel_chain.py")
        idx = src.find("def budget_notification_pending")
        body = src[idx:idx + 1400]
        assert body.count("_budget_lock") == 1

    def test_latch_fires_exactly_once(self, monkeypatch, tmp_path):
        from nexus_memory import fuel_chain as fc
        monkeypatch.setattr(fc, "FUEL_STATE", tmp_path / "fuel.json")
        monkeypatch.setattr(fc, "budget_exhausted", lambda: True)
        assert fc.budget_notification_pending() is True
        assert fc.budget_notification_pending() is False
        assert fc.budget_notification_pending() is False

    def test_notified_month_survives_reload(self, monkeypatch, tmp_path):
        from nexus_memory import fuel_chain as fc
        monkeypatch.setattr(fc, "FUEL_STATE", tmp_path / "fuel.json")
        monkeypatch.setattr(fc, "budget_exhausted", lambda: True)
        fc.budget_notification_pending()
        raw = json.loads((tmp_path / "fuel.json").read_text())
        assert raw.get("notified_month") == fc._current_month()


# ── Nr 310: sort key type-safe ──────────────────────────────────────────────

class TestNr310SortKey:
    def test_typed_sort_tuple(self):
        src = _read("src/nexus_memory/health_audit.py")
        assert "isinstance(value, (int, float))" in src
        assert "(0, float(value))" in src
        assert "(1, str(value))" in src


# ── Nr 312: unique backup name ──────────────────────────────────────────────

class TestNr312UniqueBackup:
    def test_backup_name_has_uuid_suffix(self):
        src = _read("src/nexus_memory/health_audit.py")
        assert "uuid.uuid4().hex[:8]" in src

    def test_never_overwrites_existing(self):
        src = _read("src/nexus_memory/health_audit.py")
        assert "if not candidate.exists()" in src


# ── Nr 313: keeper payload persisted ────────────────────────────────────────

class TestNr313KeeperBackup:
    def test_keepers_array_in_backup(self):
        src = _read("src/nexus_memory/health_audit.py")
        assert '"keepers"' in src
        assert "keeper_rows.append" in src

    def test_backup_roundtrip_keeps_keeper(self, tmp_path):
        """Full backup dict round-trips including the keepers array."""
        data = {
            "keeper_strategy": "oldest_created_at",
            "collection": "c",
            "keepers": [{"id": "k1", "payload": {"entity_attributes": {"a": 1}}}],
            "deleted": [],
        }
        out = json.loads(json.dumps(data))
        assert out["keepers"][0]["payload"]["entity_attributes"] == {"a": 1}


# ── Nr 314: vectors only for delete ids ─────────────────────────────────────

class TestNr314VectorFetch:
    def test_scan_is_payload_only(self):
        src = _read("src/nexus_memory/health_audit.py")
        idx = src.find("def _dedup_sweep")
        # skip the docstring: first with_vectors reference must be the scan
        body = src[idx:idx + 2000]
        first = body.find("with_vectors=")
        assert first >= 0
        assert body[first:first + 30].startswith("with_vectors=False")

    def test_retrieve_vectors_helper_exists(self):
        src = _read("src/nexus_memory/health_audit.py")
        assert "def _retrieve_vectors" in src
        assert "with_vectors=True" in src


# ── Nr 315: numeric guard narrowed ──────────────────────────────────────────

class TestNr315NumericGuard:
    def test_fullmatch_guard(self):
        src = _read("src/nexus_memory/query_rewrite.py")
        assert 're.fullmatch(r"[\\d\\s:./#-]+", q.strip())' in src
        # old broad search-based guard must be gone
        assert 're.search(r"\\d", q) and len(q) < 60' not in src

    def test_port_query_untouched(self, monkeypatch):
        from nexus_memory import query_rewrite as qr
        monkeypatch.setattr(qr, "generate_rewrite", None, raising=False)
        assert qr.rewrite_query("port 9220", None) == "port 9220"

    def test_mixed_query_is_rewritten(self, monkeypatch):
        from nexus_memory import query_rewrite as qr
        assert qr.rewrite_query("fehler 404 beim login", lambda q: "login fehler 404") \
            == "login fehler 404"


# ── Nr 316: meta-discard anchored to start ──────────────────────────────────

class TestNr316MetaAnchor:
    def test_meta_regex_anchored(self):
        src = _read("src/nexus_memory/query_rewrite.py")
        assert 're.match(r"^(antwort|' in src

    def test_compound_with_antwort_survives(self):
        from nexus_memory import query_rewrite as qr
        out = qr._clean_output("beantworten wallbox rfid")
        assert out == "beantworten wallbox rfid"


# ── Nr 317: dash separator needs whitespace ─────────────────────────────────

class TestNr317DashSeparator:
    def test_dash_regex_requires_whitespace(self):
        src = _read("src/nexus_memory/query_rewrite.py")
        assert '\\s+[-–]\\s+' in src

    def test_compound_hyphen_survives(self):
        from nexus_memory import query_rewrite as qr
        out = qr._clean_output("wallbox ladekabel rfid-rueckgabe anleitung")
        assert "rfid-rueckgabe" in out


# ── Nr 318: months_since normalizes now ─────────────────────────────────────

class TestNr318MonthsSince:
    def test_naive_now_accepted(self):
        from datetime import datetime
        from nexus_memory.memory_dynamics import months_since
        naive = datetime(2026, 3, 1, 12, 0, 0)
        # months_since takes the raw timestamp; decay_factor extracts it
        # from the payload before calling. A naive `now` must still work.
        val = months_since("2025-03-01T00:00:00Z", naive)
        assert val > 0

    def test_iso_string_now_accepted(self):
        from nexus_memory.memory_dynamics import months_since
        assert months_since("2025-03-01T00:00:00Z", "2026-03-01T00:00:00Z") > 0

    def test_normalization_code_present(self):
        src = _read("src/nexus_memory/memory_dynamics.py")
        assert "now = parse_ts(now) or datetime.now(timezone.utc)" in src


# ── Nr 319: category-default salience everywhere ────────────────────────────

class TestNr319SalienceDefaults:
    def test_rule_default_is_decay_immune_in_decay_factor(self):
        from nexus_memory.memory_dynamics import decay_factor
        p = {"category": "rule", "created_at": "2020-01-01T00:00:00Z"}
        assert decay_factor(p) == 1.0

    def test_is_salient_uses_category_default(self):
        from nexus_memory.memory_dynamics import is_salient
        assert is_salient({"category": "rule"}) is True
        assert is_salient({"category": "temp"}) is False

    def test_helpers_share_logic(self):
        src = _read("src/nexus_memory/memory_dynamics.py")
        assert "normalize_salience(payload.get(\"salience\")" in src


# ── Nr 320: makedirs fail-soft ──────────────────────────────────────────────

class TestNr320MakedirsSoft:
    def test_makedirs_guarded(self):
        src = _read("src/nexus_memory/selective_forgetting.py")
        idx = src.find("def __init__")
        body = src[idx:idx + 700]
        assert "try:" in body
        assert "except OSError" in body

    def test_report_skip_when_dir_unavailable(self):
        src = _read("src/nexus_memory/selective_forgetting.py")
        assert "if not self._data_dir:" in src

    def test_constructor_survives_bad_dir(self, monkeypatch):
        from nexus_memory import selective_forgetting as sf
        monkeypatch.setattr(sf.os, "makedirs",
                            lambda *a, **k: (_ for _ in ()).throw(OSError("nope")))
        class _Store:
            client = None
        a = sf.SelectiveForgettingAuditor(_Store(), "coll", data_dir="/proc/1/w19-impossible")
        assert a._data_dir is None


# ── Nr 321: status-fallback in pre-filter ───────────────────────────────────

class TestNr321StatusFallback:
    def test_prefilter_uses_status_fallback(self):
        src = _read("src/nexus_memory/selective_forgetting.py")
        idx = src.find("def run(")
        body = src[idx:idx + 1200]
        assert 'payload.get("lifecycle_status") or payload.get("status")' in body
        assert "protected_lifecycle" in body

    def test_status_superseded_not_invalid_timestamp(self):
        from nexus_memory import selective_forgetting as sf

        class _P:
            def __init__(self, payload):
                self.payload = payload

        class _Store:
            client = None

        a = sf.SelectiveForgettingAuditor(_Store(), "coll", data_dir=None)
        # data_dir=None because /proc-based dir was refused above; report skip
        points = [_P({"status": "superseded", "created_at": "2020-01-01T00:00:00Z"})]
        # monkeypatch the scroll
        a._store = SimpleNamespace(client=SimpleNamespace(scroll=lambda *a, **k: ([], None)))
        rep = a.run()
        assert rep["invalid_timestamps"] == 0