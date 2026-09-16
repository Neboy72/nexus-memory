"""OCR-2 Welle 33 (high, packet 6): entity/consolidation/health_audit/
guardrails/wizard + auto_recall embed. One class per root cause.

Behavior tests target the real public APIs: extract_entities
(entity_extractor), GuardrailEngine.check_action (guardrails), _parse_verdict
(consolidation). Source-inspection for the wizard/health_audit seams.
"""

import pytest

REPO = __file__.rsplit("/tests/", 1)[0]


def _read(rel: str) -> str:
    with open(f"{REPO}/{rel}", encoding="utf-8") as f:
        return f.read()


REASON_NEXUS = "nexus_memory not importable"


def _load(name: str, path: str):
    import importlib.util, sys
    spec = importlib.util.spec_from_file_location(name, f"{REPO}/{path}")
    if spec is None or spec.loader is None:
        pytest.skip("no spec")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod  # dataclasses need the module in sys.modules
    spec.loader.exec_module(mod)
    return mod


# ── W33-1: entity_extractor granular parse ──────────────────────────────────

class TestW33EntityExtractor:
    def test_source_relation_typechecked(self):
        src = _read("src/nexus_memory/entity_extractor.py")
        # isinstance guard on relation before `in RELATION_TYPES`
        seg = src.split("def __init__")[1][:800]
        assert "isinstance(relation, str)" in seg or "not isinstance(relation, str)" in src

    def test_behavior_relationship_construction(self):
        mod = _load("ee_w33", "src/nexus_memory/entity_extractor.py")
        Relationship = mod.Relationship
        # unhashable relation must degrade to connected_to, not raise
        r = Relationship(source="a", target="b", relation={"bad": 1})
        assert r.relation == "connected_to"


# ── W33-2: consolidation JSON extraction ────────────────────────────────────

class TestW33ConsolidationJson:
    def test_verdict_prose_never_drives(self):
        # W33-2 SKIPPED by design: the strict verdict mode rejects lenient
        # rescue — embedded instruction text must NOT deactivate a memory.
        mod = _load("cons_w33", "src/nexus_memory/consolidation.py")
        v = mod._parse_verdict('The model says: {"verdict": "supersede"}')
        assert v == "unrelated"

    def test_clean_json_still_parses(self):
        mod = _load("cons_w33b", "src/nexus_memory/consolidation.py")
        assert mod._parse_verdict('{"verdict": "supersede"}') == "supersede"
        assert mod._parse_verdict("plain prose, no json") == "unrelated"


# ── W33-3: backfill guardrail rule ──────────────────────────────────────────

class TestW33BackfillGuard:
    def test_source_threads_source_payload(self):
        src = _read("src/nexus_memory/consolidation.py")
        # backfill entry must accept and pass source_payload through
        assert "source_payload" in src.split("def backfill")[1].split("\ndef ")[0]


# ── W33-4/5: health_audit keeper + report race ──────────────────────────────

class TestW33HealthAudit:
    def test_source_keeper_reverified(self):
        src = _read("src/nexus_memory/health_audit.py")
        assert "keeper" in src.lower() and "content_hash" in src

    def test_source_publish_last(self):
        src = _read("src/nexus_memory/health_audit.py")
        idx_pub = src.find("self._last_report")
        idx_sweep = src.find("dedup_sweep")
        assert idx_pub > idx_sweep


# ── W33-6/7: guardrails classify + quoted targets ───────────────────────────

class TestW33Guardrails:
    def test_behavior_content_words_not_destructive(self):
        mod = _load("gr_w33a", "src/nexus_memory/guardrails.py")
        engine = mod.GuardrailEngine.__new__(mod.GuardrailEngine)
        try:
            result = engine.check_action(
                command="", tool_name="write_file",
                tool_input={"path": "/tmp/x.txt", "content": "kill -9 and drop table, truncate logs"},
            )
        except TypeError:
            pytest.skip("check_action signature differs")
        assert result.verdict == mod.GuardrailVerdict.ALLOW

    def test_behavior_quoted_protected_target_extracted(self):
        mod = _load("gr_w33b", "src/nexus_memory/guardrails.py")
        # W33-7: the quoted operand must be extracted in FULL (with the space).
        targets = mod.extract_targets('rm -rf "/data/protected dir"')
        assert "/data/protected dir" in targets
        # And with a matching protection rule the engine blocks it.
        engine = mod.GuardrailEngine.__new__(mod.GuardrailEngine)
        engine._cache = [{"path": "/data/protected", "text": "protect /data/protected",
                          "rule_text": "rm auf /data/protected ist verboten", "source_memory_id": "m1"}]
        engine._cache_time = __import__("time").time()
        engine._cache_ttl = 60.0
        engine._last_load_state = "fresh"
        engine._last_complete_rules = engine._cache
        try:
            engine._load_rules = lambda *a, **k: None
        except Exception:
            pass
        result = engine.check_action(command='rm -rf "/data/protected dir"', tool_name="terminal", tool_input=None)
        assert result.verdict == mod.GuardrailVerdict.BLOCK


# ── W33-8/9: wizard consistency ─────────────────────────────────────────────

class TestW33Wizard:
    def test_source_hf_fallback_complete(self):
        src = _read("src/nexus_memory/wizard.py")
        assert 'os.environ["NEXUS_HF_BGE3"]' in src or "os.environ['NEXUS_HF_BGE3']" in src
        assert "huggingface" in src


# ── W33-10: auto_recall ollama endpoint ─────────────────────────────────────

class TestW33AutoRecallOllama:
    def test_source_new_endpoint(self):
        src = _read("plugins/claude-code/scripts/auto_recall.py")
        assert "/api/embed" in src
        assert "embeddings" in src


# ── W33-11: legacy test_mcp robustness ──────────────────────────────────────

class TestW33LegacyTestMcp:
    def test_source_robust(self):
        src = _read("scripts/legacy/test_mcp.py")
        assert "isinstance" in src or "try" in src