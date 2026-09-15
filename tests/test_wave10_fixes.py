"""Tests for OCR review Wave 10 — HIGH findings H1–H10.

One test class per finding. Pure-logic paths are exercised behaviourally;
external I/O (Qdrant, OpenAI, Voyage HTTP, requests) is replaced by mocks so
the tests stay hermetic and offline.
"""

from __future__ import annotations

import json
import sys
import time
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest


# ── H1: chat_wizard surfaces a corrupt-config error as JSON ──────────────────


class TestH1ChatWizardCorruptConfig:
    """_save_config() raises ValueError on a corrupt config; the two wizard
    entry points must convert that into an {"error": ...} dict, not a
    traceback."""

    def _wizard(self):
        import nexus_memory.chat_wizard as cw

        return cw

    def test_apply_choice_returns_error_dict(self, monkeypatch, tmp_path):
        cw = self._wizard()
        monkeypatch.setattr(cw, "_load_config", lambda: {"_config_corrupt": True})
        monkeypatch.setattr(cw, "_get_env_path", lambda: tmp_path / ".env")
        monkeypatch.delenv("JINA_API_KEY", raising=False)

        result = cw.apply_choice("jina")  # jina needs no pip install

        assert "error" in result
        assert "corrupt" in result["error"].lower()
        assert "step" not in result

    def test_save_trust_level_returns_error_dict(self, monkeypatch):
        cw = self._wizard()
        monkeypatch.setattr(cw, "_load_config", lambda: {"_config_corrupt": True})

        result = cw.save_trust_level("trusted")

        assert "error" in result
        assert "corrupt" in result["error"].lower()
        assert "step" not in result


# ── H2: Roo Code vs. Cline extension detection ───────────────────────────────


class TestH2RooVsClineDetection:
    """Real extension ids: Roo Code is ``rooveterinaryinc.roo-cline-*`` (no
    "code" substring), Cline is ``saoudrizwan.claude-dev-*``. The old bare
    substring predicates swapped/missed these."""

    def _ext_dir(self, tmp_path, monkeypatch, folder):
        import nexus_memory.agent_detect as ad

        ext = tmp_path / ".vscode" / "extensions"
        ext.mkdir(parents=True)
        (ext / folder).mkdir()
        # Redirect Path.home() for the detectors only.
        monkeypatch.setattr(ad.Path, "home", staticmethod(lambda: tmp_path))
        return ad

    def test_roo_cline_detected_as_roo_not_cline(self, tmp_path, monkeypatch):
        ad = self._ext_dir(tmp_path, monkeypatch,
                           "rooveterinaryinc.roo-cline-3.3.0")

        assert ad._check_roo_code()["detected"] is True
        assert ad._check_cline()["detected"] is False

    def test_claude_dev_detected_as_cline_not_roo(self, tmp_path, monkeypatch):
        ad = self._ext_dir(tmp_path, monkeypatch,
                           "saoudrizwan.claude-dev-3.17.0")

        assert ad._check_cline()["detected"] is True
        assert ad._check_roo_code()["detected"] is False

    def test_plain_cline_folder_still_detected(self, tmp_path, monkeypatch):
        ad = self._ext_dir(tmp_path, monkeypatch, "cline-1.0.0")

        assert ad._check_cline()["detected"] is True


# ── H3: explicit cost_aware_routing config wins over provider count ──────────


class TestH3RoutingConfigOverride:
    def _router(self, tmp_path, routing_value, providers):
        from nexus_memory.cost_router import CostAwareRouter

        nexus_dir = tmp_path / "nexus"
        nexus_dir.mkdir(exist_ok=True)
        (nexus_dir / "config.json").write_text(
            json.dumps({"cost_aware_routing": routing_value})
        )
        router = CostAwareRouter(hermes_home=str(tmp_path))
        # Deterministic provider set (no env/socket probing).
        router._detect_available_providers = lambda: router._available_providers.update(providers)
        return router

    def test_override_false_beats_two_providers(self, tmp_path):
        from nexus_memory.cost_router import TIER_ECONOMY, TIER_PREMIUM

        router = self._router(
            tmp_path, False, {"voyage": TIER_PREMIUM, "ollama": TIER_ECONOMY}
        )
        router.initialize()
        assert router._routing_enabled is False

    def test_override_true_beats_single_provider(self, tmp_path):
        from nexus_memory.cost_router import TIER_ECONOMY

        router = self._router(tmp_path, True, {"ollama": TIER_ECONOMY})
        router.initialize()
        assert router._routing_enabled is True

    def test_no_override_falls_back_to_provider_count(self, tmp_path):
        from nexus_memory.cost_router import TIER_ECONOMY, TIER_PREMIUM

        # No config file → heuristic applies (2 providers → enabled).
        from nexus_memory.cost_router import CostAwareRouter

        router = CostAwareRouter(hermes_home=str(tmp_path / "nope"))
        router._detect_available_providers = lambda: router._available_providers.update(
            {"voyage": TIER_PREMIUM, "ollama": TIER_ECONOMY}
        )
        router.initialize()
        assert router._routing_enabled is True


# ── H4: LLM entity JSON is validated before use ──────────────────────────────


class TestH4EntityExtractorValidation:
    def _extract(self, monkeypatch, payload_text):
        import nexus_memory.entity_extractor as ee

        monkeypatch.setattr(
            ee, "_load_llm_config",
            lambda home: {"model": "fake", "base_url": "http://x", "api_key": "k"},
        )
        monkeypatch.setattr(ee, "_quick_health_check", lambda base_url, timeout=1.0: True)

        fake_resp = types.SimpleNamespace(
            choices=[types.SimpleNamespace(message=types.SimpleNamespace(content=payload_text))]
        )
        fake_openai = types.ModuleType("openai")
        fake_openai.OpenAI = lambda **kw: types.SimpleNamespace(
            chat=types.SimpleNamespace(
                completions=types.SimpleNamespace(create=lambda **kw: fake_resp)
            )
        )
        monkeypatch.setitem(sys.modules, "openai", fake_openai)
        return ee._llm_extract_entities("some text", "/tmp/hermes")

    def test_non_dict_json_returns_empty(self, monkeypatch):
        for payload in ("[1, 2, 3]", "null", '"just a string"'):
            result = self._extract(monkeypatch, payload)
            assert result.entities == []
            assert result.relationships == []
            assert result.is_empty()

    def test_bad_field_types_fall_back_to_defaults(self, monkeypatch):
        payload = json.dumps({
            "entities": [
                {"name": "Router", "type": 123, "attributes": "not-a-dict",
                 "confidence": "high"},
                {"name": "Modem", "type": "device", "confidence": True},
                {"name": 42},          # non-string name → skipped
                {"name": ""},          # empty name → skipped
                "not-a-dict",          # skipped
            ],
            "relationships": [
                {"source": "Router", "target": "Modem",
                 "relation": "connected_to", "confidence": "bad"},
                {"source": 5, "target": "Modem", "relation": "connected_to"},
                {"source": "Router", "target": "", "relation": "connected_to"},
                "not-a-dict",
            ],
        })
        result = self._extract(monkeypatch, payload)

        assert [e.name for e in result.entities] == ["Router", "Modem"]
        router = result.entities[0]
        assert router.entity_type == "concept"   # non-str type → default
        assert router.attributes == {}           # non-dict attrs → default
        assert router.confidence == 0.8          # non-numeric → default
        # bool is not a real confidence → default.
        assert result.entities[1].confidence == 0.8
        assert result.entities[1].entity_type == "device"

        assert len(result.relationships) == 1
        rel = result.relationships[0]
        assert (rel.source, rel.target) == ("Router", "Modem")
        assert rel.confidence == 0.7             # non-numeric → default

    def test_out_of_range_confidence_is_clamped(self, monkeypatch):
        payload = json.dumps({
            "entities": [
                {"name": "A", "confidence": 5},
                {"name": "B", "confidence": -3},
            ],
            "relationships": [],
        })
        result = self._extract(monkeypatch, payload)
        assert result.entities[0].confidence == 1.0
        assert result.entities[1].confidence == 0.0


# ── H5: dotenv serialize → parse → read round-trip ───────────────────────────


class TestH5EnvValueRoundTrip:
    def test_write_read_roundtrip_inverts_escapes(self, tmp_path):
        from nexus_memory.env_secret_store import (
            _parse_env_text, read_env_key, write_env_key,
        )

        env = tmp_path / ".env"
        for value in ("vo-abc123", "it's", "back\\slash", "a'b\\c", 'q"uote'):
            write_env_key(env, "KEY", value)
            assert read_env_key(env, "KEY") == value
            # read-modify-write parser agrees with the single-key reader
            assert _parse_env_text(env.read_text())["KEY"] == value

    def test_double_quoted_value_is_unquoted(self, tmp_path):
        from nexus_memory.env_secret_store import (
            _parse_env_text, read_env_key,
        )

        env = tmp_path / ".env"
        env.write_text('K="double-quoted-value"\n')
        assert read_env_key(env, "K") == "double-quoted-value"
        assert _parse_env_text(env.read_text())["K"] == "double-quoted-value"


# ── H6: unresolved variable targets must not bypass the guardrail ────────────


class TestH6VariableTargetBlocks:
    def test_variable_targets_block(self):
        from nexus_memory.guardrails import GuardrailEngine, GuardrailVerdict

        client = MagicMock()
        client.scroll.return_value = ([], None)
        engine = GuardrailEngine(client)

        for cmd in ("rm -rf $BACKUP_DIR", "rm -rf ${BACKUP_DIR}",
                    "del /f %USERPROFILE%"):
            result = engine.check_action(cmd)
            assert result.verdict == GuardrailVerdict.BLOCK, cmd
            assert "variable" in result.reason.lower()

    def test_concrete_path_unaffected(self):
        from nexus_memory.guardrails import GuardrailEngine, GuardrailVerdict

        client = MagicMock()
        client.scroll.return_value = ([], None)
        engine = GuardrailEngine(client)
        result = engine.check_action("rm -rf /tmp/plain_dir")
        assert result.verdict == GuardrailVerdict.ALLOW


# ── H7: a served cache counts as a complete rule load in the no-target branch ─


class TestH7CachedNoTarget:
    def test_cached_state_allows_no_target_action(self):
        from nexus_memory.guardrails import GuardrailEngine, GuardrailVerdict

        engine = GuardrailEngine(MagicMock())
        engine._cache = [{"path": "/x", "rule_text": "r", "source_memory_id": "1"}]
        engine._cache_time = time.time()  # fresh TTL → served as "cached"

        result = engine.check_action("pkill")  # destructive, no extractable target

        assert engine._last_load_state == "cached"
        assert result.verdict == GuardrailVerdict.ALLOW
        assert "no protected target" in result.reason.lower()

    def test_degraded_state_still_blocks(self):
        from nexus_memory.guardrails import GuardrailEngine, GuardrailVerdict

        client = MagicMock()
        client.scroll.side_effect = Exception("Qdrant down")
        engine = GuardrailEngine(client)

        result = engine.check_action("pkill")

        assert engine._last_load_state == "degraded"
        assert result.verdict == GuardrailVerdict.BLOCK


# ── H8: page-bound hit degrades state and never caches a partial rule set ────


class TestH8PageBoundDegrades:
    def test_bound_hit_degrades_and_keeps_cache(self):
        from nexus_memory.guardrails import GuardrailEngine, GuardrailVerdict

        client = MagicMock()
        client.scroll.return_value = ([], "next-page")  # never reaches the end

        engine = GuardrailEngine(client)
        sentinel = [{"path": "/keep/me", "rule_text": "x", "source_memory_id": "r1"}]
        engine._cache = list(sentinel)
        engine._cache_time = 0.0  # expired → real reload

        engine._load_protected_rules()

        assert engine._last_load_state == "degraded"
        assert engine._cache == sentinel          # partial set NOT cached
        assert client.scroll.call_count > 100

        # A degraded load must fail closed on an unmatched target.
        result = engine.check_action("rm -rf /tmp/x")
        assert result.verdict == GuardrailVerdict.BLOCK


# ── H9: a dedup-sweep exception is not reported as read-only ─────────────────


class _FakeResp:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TestH9DedupSweepErrorReporting:
    def _auditor(self, tmp_path, monkeypatch):
        from nexus_memory import health_audit as ha

        monkeypatch.setattr(
            "nexus_memory.agent_detect.cleanup_removed_agents",
            lambda: {"removed": 0},
        )
        return ha.HealthAuditor(MagicMock(), "coll", data_dir=str(tmp_path / "reports"))

    def test_run_audit_records_error_entry(self, tmp_path, monkeypatch):
        monkeypatch.setenv("NEXUS_DEDUP_SWEEP", "1")
        aud = self._auditor(tmp_path, monkeypatch)
        monkeypatch.setattr(aud, "_audit", lambda: {
            "timestamp": "2026-09-15 00:00:00",
            "collection": "coll",
            "total_points": 3,
            "duplicate_summary": {"groups": 1, "excess_copies": 1},
            "duplicate_groups": [{"count": 2, "ids": ["a", "b"]}],
        })

        def _boom():
            raise RuntimeError("qdrant died mid-delete")

        monkeypatch.setattr(aud, "_dedup_sweep", _boom)

        report = aud.run_audit()

        assert report["dedup_sweep"]["error"] == "qdrant died mid-delete"
        assert report["dedup_sweep"]["mode"] == "error"
        # get_flags must not claim a read-only run.
        message = aud.get_flags()["dedup"]["message"]
        assert "errored" in message.lower()
        assert "READ-ONLY" not in message

    def test_webhook_does_not_report_readonly_on_error(self, tmp_path, monkeypatch):
        import urllib.request

        from nexus_memory import health_audit as ha

        monkeypatch.setenv("NEXUS_WEBHOOK_URL", "http://webhook.example/notify")
        captured = []

        def _fake_urlopen(req, timeout=15):
            captured.append(json.loads(req.data.decode("utf-8")))
            return _FakeResp()

        monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)

        aud = ha.HealthAuditor(MagicMock(), "coll", data_dir=str(tmp_path))
        aud._maybe_webhook({
            "timestamp": "2026-09-15 00:00:00",
            "collection": "coll",
            "total_points": 3,
            "duplicate_summary": {"groups": 1, "excess_copies": 1},
            "report_file": "/tmp/report.json",
            "dedup_sweep": {"error": "boom", "mode": "error"},
        })

        assert len(captured) == 1
        body = captured[0]["content"]
        assert "errored" in body.lower()
        assert "read-only" not in body.lower()


# ── H10: reranker tolerates malformed scores / short score vectors ───────────


class TestH10RerankerRobustness:
    def _voyage(self, monkeypatch, data):
        import nexus_memory.reranker as rr

        class FakeResp:
            status_code = 200

            def json(self):
                return {"data": data}

        fake_requests = MagicMock()
        fake_requests.post.return_value = FakeResp()
        monkeypatch.setattr(rr, "_requests", fake_requests)
        return rr

    def test_missing_index_and_null_score_do_not_crash(self, monkeypatch):
        rr = self._voyage(monkeypatch, [
            {"relevance_score": 0.9},                 # missing index → dropped
            {"index": 1, "relevance_score": None},    # null score → 0.0
            {"index": 99, "relevance_score": 0.5},    # out of range → dropped
            {"index": "0", "relevance_score": 1.0},   # non-int index → dropped
            {"index": 0, "relevance_score": "0.8"},   # string score → 0.0
        ])
        results = [{"_idx": 0, "text": "a"}, {"_idx": 1, "text": "b"}]

        out = rr._rerank_voyage("q", results, "k")

        assert sorted(r["_idx"] for r in out) == [0, 1]
        assert all(r["_rerank_score"] == 0.0 for r in out)

    def test_all_entries_malformed_returns_empty(self, monkeypatch):
        rr = self._voyage(monkeypatch, [{"relevance_score": 1.0}, {"index": 42}])
        out = rr._rerank_voyage("q", [{"_idx": 0, "text": "a"}], "k")
        assert out == []

    def test_local_short_predict_fails_open_to_empty(self, monkeypatch):
        import nexus_memory.reranker as rr
        import nexus.retrieval as nr

        class _ShortCE:
            def predict(self, pairs):
                return [0.1] * (len(pairs) - 1)

        monkeypatch.setattr(nr, "_get_cross_encoder", lambda: _ShortCE())
        results = [{"_idx": 0, "text": "a"}, {"_idx": 1, "text": "b"}]

        assert rr._rerank_local("q", results) == []

    def test_local_bad_score_values_do_not_crash(self, monkeypatch):
        import nexus_memory.reranker as rr
        import nexus.retrieval as nr

        class _BadCE:
            def predict(self, pairs):
                return ["nope"] * len(pairs)

        monkeypatch.setattr(nr, "_get_cross_encoder", lambda: _BadCE())
        results = [{"_idx": 0, "text": "a"}, {"_idx": 1, "text": "b"}]

        out = rr._rerank_local("q", results)

        assert [r["_idx"] for r in out] == [0, 1]  # fallback keeps order
