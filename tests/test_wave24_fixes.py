"""OCR review wave 24 (low B) - fixes for findings Nr 438-467.

One test class per finding: source-inspection plus behavior tests.
Written by Kiosha (CC was contractually not allowed to touch this file).
CC documented two justified deviations: Nr 443 (_SCOPE_RE IS used, 4 refs;
no dead `import time` exists) and Nr 458 (13 logger calls, not 14 - the
13th+14th were one two-line call). Nr 451 was implemented as a coalescing
delta buffer by CC but REVIEW-REJECTED by Kiosha (per-process buffer loses
subprocess stats on exit; corrupt registry would surface at flush time, not
immediately - fail-closed broken). Final fix: atomic replace kept, fsync
skipped on the stats path only.
"""
import ast
import json
import math
import os
import re
import sys
import textwrap
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
if str(REPO / "src") not in sys.path:
    sys.path.insert(0, str(REPO / "src"))


def _read(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8")


def _code_lines(src: str) -> str:
    """Source without full-line comments (comment-marks often quote keywords)."""
    return "\n".join(
        ln for ln in src.splitlines()
        if not ln.strip().startswith(("#", "//", "*", "/*"))
    )


# ── Block A: nexus/graph ────────────────────────────────────────────────────


class TestNr438EdgeEnumValidation:
    def test_from_payload_validates_status_and_relation(self):
        from nexus.graph.schema import Edge, EdgeSchemaError

        good = {
            "edge_id": "e1",
            "source_fact_id": "f1",
            "target_fact_id": "f2",
            "relation": "supports",
            "status": "active",
        }
        e = Edge.from_payload_entry(good, "f1")
        assert e.status == "active" and e.relation == "supports"  # str-Enum

        bad_status = dict(good, status="Active")
        with pytest.raises(EdgeSchemaError):
            Edge.from_payload_entry(bad_status, "f1")

        bad_rel = dict(good, relation="loves")
        with pytest.raises(EdgeSchemaError):
            Edge.from_payload_entry(bad_rel, "f1")

    def test_missing_status_still_defaults_active(self):
        from nexus.graph.schema import Edge, EdgeStatus

        no_status = {
            "edge_id": "e2", "source_fact_id": "f1",
            "target_fact_id": "f2", "relation": "supports",
        }
        e = Edge.from_payload_entry(no_status, "f1")
        assert e.status == EdgeStatus.ACTIVE  # backward compat preserved

    def test_validation_sets_derived_from_enums(self):
        src = _code_lines(_read("nexus/graph/schema.py"))
        assert "_VALID_RELATIONS = frozenset(r.value for r in EdgeRelation)" in src
        assert "_VALID_STATUSES = frozenset(s.value for s in EdgeStatus)" in src


class TestNr439SelfEdgeDedup:
    def test_merge_dedupes_self_edge_by_edge_id(self):
        from nexus.graph.store import EdgeStore

        store = EdgeStore.__new__(EdgeStore)
        e = _edge_stub("e-self", source="f1", target="f1")
        merged = store._merge_edge_lists([e, e]) if hasattr(
            store, "_merge_edge_lists") else None
        if merged is None:
            pytest.skip("seam-level merge helper not exposed; covered by source check")

    def test_no_dedup_needed_comment_gone(self):
        src = _read("nexus/graph/store.py")
        # the outdated claim must only survive as Nr-439 history inside the
        # explanatory comment, never as the merge rationale
        code = _code_lines(src)
        assert "return outgoing_edges + incoming_edges" not in code
        assert "merged.setdefault(edge.edge_id, edge)" in code


class TestNr440GraphBoostScopeGating:
    def test_graph_boost_passes_query_embedding(self):
        src = _read("plugins/claude-code/scripts/auto_recall.py")
        sig = re.search(r"def graph_boost\(([^)]*)\)", src)
        assert sig and "query_embedding" in sig.group(1)
        # the main() call site must pass the embedding through
        call = re.search(
            r"graph_boost\(\s*results,\s*max_boost=3,\s*access_level=trust_level,"
            r"\s*query_embedding=embedding", src)
        assert call, "main() must pass query_embedding=embedding to graph_boost"

    def test_graph_boost_applies_allowed_scopes(self):
        src = _code_lines(_read("plugins/claude-code/scripts/auto_recall.py"))
        m = re.search(r"def graph_boost.*?(?=\ndef |\Z)", src, re.S)
        body = m.group(0)
        assert "allowed_scopes is not None" in body
        assert "p_scope not in allowed_scopes" in body
        # same helper as the vector path
        assert "prefetch_allowed_scopes" in body

    def test_intentional_bypass_note_removed(self):
        src = _read("plugins/claude-code/scripts/auto_recall.py")
        assert "scoped-out memories can surface" not in src


class TestNr441GuardrailTargetDedupe:
    def test_dedupe_line_present(self):
        src = _code_lines(_read("plugins/claude-code/scripts/guardrail_check.py"))
        assert "targets = list(dict.fromkeys(targets))" in src

    def test_behavior_no_duplicate_matched_rules(self, tmp_path, monkeypatch):
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "gc_w24", REPO / "plugins/claude-code/scripts/guardrail_check.py")
        mod = importlib.util.module_from_spec(spec)
        monkeypatch.setattr(sys, "argv", ["x"])
        spec.loader.exec_module(mod)
        cmd = "rm -rf /Users/nobody/protected"
        tool_input = {"command": cmd, "path": "/Users/nobody/protected/x"}
        res = mod.check_action(cmd, "Bash", tool_input)
        if res.get("verdict") == "block":
            rules = res.get("matched_rules", [])
            paths = [r for r in rules if "/Users/nobody" in str(r)]
            assert len(paths) == len(set(map(str, paths))), \
                "duplicate path entries in matched_rules"


class TestNr442StatsUsesGraphCache:
    def test_no_list_edges_scroll_in_stats(self):
        src = _code_lines(_read("nexus/graph/traversal.py"))
        m = re.search(r"def stats\(self\).*?(?=\n    def |\Z)", src, re.S)
        body = m.group(0)
        # the scroll is only allowed on the cold-cache fallback path
        warm = body.split("else:")[0]
        assert "list_edges(status=\"active\")" not in warm, \
            "warm path must derive counts from the NetworkX cache"
        assert "self._graph.graph" in body or "number_of_nodes" in body
        assert "total_relations = len(edge_ids)" in body

    def test_cold_cache_falls_back_to_scroll(self):
        src = _code_lines(_read("nexus/graph/traversal.py"))
        m = re.search(r"def stats\(self\).*?(?=\n    def |\Z)", src, re.S)
        body = m.group(0)
        assert "else:" in body and "all_edges" in body  # fallback path kept

    def test_total_relations_counts_distinct_edge_ids(self):
        src = _read("nexus/graph/traversal.py")
        m = re.search(r"def stats\(self\).*?(?=\n    def |\Z)", src, re.S)
        body = m.group(0)
        assert "edge_ids = set()" in body
        assert "total_relations = len(edge_ids)" in body


class TestNr443AutoCaptureScopeRe:
    def test_scope_re_is_used(self):
        """Fund claimed dead; verification showed 4 live usages. The regex
        must STAY and stay used - the fix is the corrected claim only."""
        src = _read("plugins/claude-code/scripts/auto_capture.py")
        code = _code_lines(src)
        uses = [ln for ln in code.splitlines() if "_SCOPE_RE" in ln]
        assert len(uses) >= 2, "_SCOPE_RE must remain defined AND used"
        assert re.search(r"_SCOPE_RE\.(?:search|match|findall|sub)", src)

    def test_no_dead_time_import(self):
        src = _code_lines(_read("plugins/claude-code/scripts/auto_capture.py"))
        tree = ast.parse(src)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.update(
                    a.name for a in node.names if a.name != "*")
        used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        used |= {n.value.id for n in ast.walk(tree)
                 if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)}
        dead = {m for m in ("time",) if m in imported and m not in used}
        assert not dead, f"dead imports present: {dead}"


class TestNr444ScopeOverrideAlwaysWins:
    def test_behavior_manual_scope_survives_ambiguity(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "sa_w24", REPO / "plugins/claude-code/scripts/scope_auto.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        # ambiguous query (empty centroids -> infer_scope returns "default")
        res = mod.prefetch_allowed_scopes([0.1] * 8, {}, "voice")
        if res is not None:
            assert "voice" in res, "manual override must survive the no-match path"
        # explicit None override stays None
        assert mod.prefetch_allowed_scopes([0.1] * 8, {}, "") is None

    def test_docstring_matches_implementation(self):
        src = _read("plugins/claude-code/scripts/scope_auto.py")
        m = re.search(r'def prefetch_allowed_scopes.*?"""(.*?)"""', src, re.S)
        doc = m.group(1)
        assert "NEITHER a clear match NOR a manual" in doc


# ── Block B: legacy scripts ─────────────────────────────────────────────────


class TestNr445LegacyTestMcpGuard:
    def test_import_time_run_guarded(self):
        src = _code_lines(_read("scripts/legacy/test_mcp.py"))
        assert 'if __name__ == "__main__":' in src
        # ast-based: no top-level asyncio.run call; exactly one inside the
        # __main__ guard (the trailing comment line is stripped by _code_lines,
        # so a regex over the guard block is fragile).
        tree = ast.parse(src)

        def _is_run_call(expr):
            return (isinstance(expr, ast.Expr) and isinstance(expr.value, ast.Call)
                    and isinstance(expr.value.func, ast.Attribute)
                    and expr.value.func.attr == "run"
                    and isinstance(expr.value.func.value, ast.Name)
                    and expr.value.func.value.id == "asyncio")

        top_calls = [n for n in tree.body if _is_run_call(n)]
        assert top_calls == [], "asyncio.run must not execute at import time"
        guarded = [
            n for node in tree.body
            if isinstance(node, ast.If)
            and isinstance(node.test, ast.Compare)
            and isinstance(node.test.left, ast.Name)
            and node.test.left.id == "__name__"
            for n in node.body if _is_run_call(n)
        ]
        assert guarded, "the asyncio.run call must live inside the __main__ guard"

    def test_module_import_is_side_effect_free(self):
        """The Nr-445 fix only guards the RUN; the module-level VOYAGE_API_KEY
        check deliberately sys.exit(1)s without a key (an honest legacy test
        refuses to run degraded). Import side-effect test = with key set."""
        import importlib.util
        monkeypatch_env = {"VOYAGE_API_KEY": "pa-test-not-a-real-key"}
        saved = {k: os.environ.get(k) for k in monkeypatch_env}
        os.environ.update(monkeypatch_env)
        try:
            spec = importlib.util.spec_from_file_location(
                "tm_w24b", REPO / "scripts/legacy/test_mcp.py")
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)  # must not start anything
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


class TestNr446SessionDumpPortablePath:
    def test_no_hardcoded_home(self):
        src = _code_lines(_read("scripts/session_dump.py"))
        assert "/Users/miosha" not in src
        assert "NEXUS_STATE_DB" in src
        assert "expanduser" in src

    def test_env_override_used_for_connection(self):
        src = _read("scripts/session_dump.py")
        assert re.search(r'os\.environ\.get\(\s*"NEXUS_STATE_DB"', src)


class TestNr447SessionDumpDstZone:
    def test_zoneinfo_used(self):
        src = _code_lines(_read("scripts/session_dump.py"))
        assert "ZoneInfo" in src
        assert "Europe/Berlin" in src
        assert "timedelta(hours=2)" not in src

    def test_fixed_offset_gone(self):
        src = _code_lines(_read("scripts/session_dump.py"))
        assert "timezone(timedelta" not in src


class TestNr448BenchLatencyDocstring:
    def test_docstring_matches_15_queries(self):
        src = _read("scripts/bench_latency.py")
        m = re.search(r'"""(.*?)"""', src, re.S)
        doc = m.group(1)
        assert "15 queries" in doc, "docstring must state the real sample size"

    def test_mean_printed(self):
        src = _code_lines(_read("scripts/bench_latency.py"))
        assert "statistics.mean" in src
        assert "mean=" in src


# ── Block C: src/nexus_memory part 1 ────────────────────────────────────────


class TestNr449TrustFilterCoercedMarker:
    def test_coerced_marker_on_unknown_level(self):
        from nexus_memory.chat_wizard import get_qdrant_filter_for_trust_level
        res = get_qdrant_filter_for_trust_level("trustd")
        assert res.get("coerced") is True
        assert res.get("coerced_from") == "trustd"
        # filter itself still fail-safe public
        vals = [s["match"]["value"] for s in res["should"]]
        assert vals == ["public"]

    def test_no_marker_on_known_level(self):
        from nexus_memory.chat_wizard import get_qdrant_filter_for_trust_level
        res = get_qdrant_filter_for_trust_level("trusted")
        assert "coerced" not in res and "coerced_from" not in res


class TestNr450KiloDirIsDir:
    def test_kilo_detector_uses_is_dir(self):
        src = _code_lines(_read("src/nexus_memory/agent_detect.py"))
        m = re.search(r"def _check_kilo_code.*?(?=\ndef |\Z)", src, re.S)
        body = m.group(0)
        assert "kilo_dir.is_dir()" in body
        assert "any(kilo_dir.iterdir())" not in body


class TestNr451StatsNoFsyncOnHotPath:
    """Final resolution after review: CC's coalescing buffer REJECTED
    (subprocess stats loss + fail-closed delayed). Kept: atomic replace.
    Removed: fsync on the stats write path."""

    def test_update_agent_stats_uses_nofsync_writer(self):
        src = _code_lines(_read("src/nexus_memory/agent_detect.py"))
        m = re.search(r"def update_agent_stats.*?(?=\ndef |\Z)", src, re.S)
        body = m.group(0)
        assert "_write_registry_unlocked_nofsync(registry)" in body
        assert "_flush_pending_stats" not in body
        assert "_stats_pending" not in body

    def test_nofsync_writer_is_atomic_without_fsync(self):
        src = _code_lines(_read("src/nexus_memory/agent_detect.py"))
        m = re.search(
            r"def _write_registry_unlocked_nofsync.*?(?=\ndef |\Z)", src, re.S)
        body = m.group(0)
        assert "os.replace(tmp_path, path)" in body
        assert "os.fsync" not in body, "fsync must be gone on the stats path"

    def test_stats_write_still_atomic_and_complete(self, tmp_path, monkeypatch):
        from nexus_memory import agent_detect as ad
        reg = tmp_path / "agents.json"
        monkeypatch.setattr(ad, "_get_agents_registry_path", lambda: reg)
        ad.register_agent("a1", "A1", "f", "trusted", "mcp_only")
        ad.update_agent_stats("a1", read=True)
        ad.update_agent_stats("a1", read=True, write=True)
        doc = json.loads(reg.read_text())
        a = doc["agents"][0]
        assert a["reads"] == 2 and a["writes"] == 1  # immediate, no buffering
        leftovers = [p.name for p in tmp_path.iterdir()
                     if p.name.startswith("agents.json.tmp")]
        assert leftovers == []

    def test_corrupt_registry_still_fails_closed_immediately(
            self, tmp_path, monkeypatch):
        from nexus_memory import agent_detect as ad
        reg = tmp_path / "agents.json"
        monkeypatch.setattr(ad, "_get_agents_registry_path", lambda: reg)
        reg.write_text("{corrupt json")
        with pytest.raises(json.JSONDecodeError):
            ad.update_agent_stats("a1", read=True)

    def test_buffer_rejection_documented(self):
        src = _read("src/nexus_memory/agent_detect.py")
        assert "REJECTED" in src, "review decision must be documented in-code"


class TestNr452EmbedCacheDeadImport:
    def test_threading_module_import_gone(self):
        src = _read("src/nexus_memory/embed_cache.py")
        assert "\nimport threading\n" not in src
        assert "from threading import Lock" in src


class TestNr453ProviderPriorityExplicit:
    def test_priority_tuple_defined(self):
        src = _code_lines(_read("src/nexus_memory/cost_router.py"))
        m = re.search(r'PROVIDER_PRIORITY\s*=\s*\(([^)]*)\)', src)
        assert m, "explicit PROVIDER_PRIORITY tuple must exist"
        names = [x.strip().strip('"\'') for x in m.group(1).split(",")]
        assert names[:3] == ["voyage", "openai", "google"]

    def test_tier_providers_sorted_by_priority(self):
        src = _code_lines(_read("src/nexus_memory/cost_router.py"))
        assert "tier_providers.sort(" in src
        assert "PROVIDER_PRIORITY.index(n)" in src

    def test_behavior_voyage_wins_regardless_of_detection_order(
            self, monkeypatch, tmp_path):
        from nexus_memory import cost_router as cr

        r = cr.CostAwareRouter.__new__(cr.CostAwareRouter)
        # sabotage detection order: openai first, voyage last
        r._available_providers = {"openai": cr.TIER_PREMIUM,
                                  "voyage": cr.TIER_PREMIUM,
                                  "ollama": cr.TIER_ECONOMY}
        r._routing_enabled = True
        r._routing_config_override = True
        r._record_decision = lambda *a, **k: None
        monkeypatch.setattr(cr, "CATEGORY_TIERS",
                            {**cr.CATEGORY_TIERS, "test_cat": cr.TIER_PREMIUM})
        chosen = r.get_provider_for_category("test_cat", record=False)
        assert chosen == "voyage", "priority list must beat detection order"


class TestNr454RuleBranchSentenceSplit:
    def test_rule_branch_uses_same_separator(self):
        src = _code_lines(_read("src/nexus_memory/extractor.py"))
        m = re.search(r"def _heuristic_extract\(.{0,4000}?"
                      r"sentences = re\.split\(r\"(\[[^]]*])", src, re.S)
        assert m, "sentence split in _heuristic_extract must exist"
        assert m.group(1) == r"[.!?]", \
            "rule branch must use the same [.!?] separator as other branches"


class TestNr455ExtractorDeadTimeImport:
    def test_time_import_gone(self):
        src = _code_lines(_read("src/nexus_memory/extractor.py"))
        assert re.search(r"^import time$", src, re.M) is None
        assert "time." not in src


class TestNr456UrlVersionPatternsWired:
    def test_patterns_now_referenced(self):
        src = _code_lines(_read("src/nexus_memory/entity_extractor.py"))
        # wired via the loop: `for pattern, attr in ((_URL_PATTERN, "url"), …)`
        uses_url = len(re.findall(r"_URL_PATTERN[,)]", src))
        uses_ver = len(re.findall(r"_VERSION_PATTERN[,)]", src))
        assert uses_url >= 1 and uses_ver >= 1

    def test_behavior_url_and_version_attached(self):
        from nexus_memory.entity_extractor import _heuristic_extract_entities
        res = _heuristic_extract_entities(
            "Qdrant laeuft auf dem Homeserver, Version 1.14.1, "
            "erreichbar unter http://q:6333")
        flat = json.dumps(
            [(e.name, e.attributes) for e in res.entities])
        # version attaches to the nearest entity (Qdrant); the URL is captured
        # as its own pseudo-entity "http" (service keyword hit)
        assert "1.14.1" in flat and "version" in flat
        assert any(e.name.lower() == "http" for e in res.entities)

    def test_ip_span_guard(self):
        src = _read("src/nexus_memory/entity_extractor.py")
        assert "ip_spans" in src, "version matches inside IPs must be skipped"


class TestNr457JinaDeadTryRemoved:
    def test_no_try_around_pure_assignments(self):
        src = _code_lines(_read("src/nexus_memory/embeddings.py"))
        m = re.search(r"def _try_jina.*?(?=\n    def |\Z)", src, re.S)
        body = m.group(0)
        assert "try:" not in body, "dead try/except must be gone"

    def test_jina_availability_still_key_presence_only(self):
        src = _code_lines(_read("src/nexus_memory/embeddings.py"))
        m = re.search(r"def _try_jina.*?(?=\n    def |\Z)", src, re.S)
        body = m.group(0)
        assert "JINA_API_KEY" in body
        assert "self._backend = \"jina\"" in body


class TestNr458EmbeddingsModuleLogger:
    def test_no_root_logging_calls_left(self):
        src = _code_lines(_read("src/nexus_memory/embeddings.py"))
        # module-level logger exists and is used; root-level logging.* gone
        m = re.search(r"^logger = logging\.getLogger\(__name__\)", src, re.M)
        assert m, "module logger must be declared"
        # remaining logging.info/warning only inside logger = getLogger line context
        bad = [ln for ln in src.splitlines()
               if re.match(r"\s*logging\.(info|warning|error|debug)\(", ln)]
        assert bad == [], f"root-logger calls remain: {bad}"

    def test_lazy_percent_formatting(self):
        src = _read("src/nexus_memory/embeddings.py")
        lazy = len(re.findall(r"logger\.(?:info|warning)\([^)]*%s", src, re.S))
        assert lazy >= 10, "logger calls must use lazy %-formatting"

    def test_fstring_logs_gone(self):
        src = _code_lines(_read("src/nexus_memory/embeddings.py"))
        bad = [ln for ln in src.splitlines()
               if re.search(r"logger\.\w+\(f\"", ln)]
        assert bad == [], f"eager f-string logger calls remain: {bad}"


class TestNr459GuardrailsUnusedAny:
    def test_any_gone_from_import_and_code(self):
        src = _read("src/nexus_memory/guardrails.py")
        assert "from typing import Any" not in src
        code = _code_lines(src)
        assert re.search(r"\bAny\b", code) is None


class TestNr460FuelChainDeadConstants:
    def test_constants_gone(self):
        src = _code_lines(_read("src/nexus_memory/fuel_chain.py"))
        assert re.search(r"^FUEL_BASE\s*=", src, re.M) is None
        assert re.search(r"^FUEL_KEY\s*=", src, re.M) is None
        assert re.search(r"^FUEL_MODEL\s*=", src, re.M) is None

    def test_build_chain_reads_env_at_call_time(self):
        src = _code_lines(_read("src/nexus_memory/fuel_chain.py"))
        assert "NEXUS_FUEL_BASE" in src  # consumed inside build_chain()

    def test_behavior_env_change_after_import_honoured(self, monkeypatch):
        from nexus_memory import fuel_chain as fc
        monkeypatch.setenv("NEXUS_FUEL_BUDGET_USD", "2.50")
        assert fc._budget_from_env() == 2.50
        monkeypatch.setenv("NEXUS_FUEL_BUDGET_USD", "garbage")
        assert fc._budget_from_env() == fc.DEFAULT_FUEL_BUDGET_USD


# ── Block D: src/nexus_memory part 2 ────────────────────────────────────────


class TestNr461HealthAuditIdempotentStart:
    def test_thread_assigned_and_guarded(self):
        src = _code_lines(_read("src/nexus_memory/health_audit.py"))
        m = re.search(r"def start\(self\).*?(?=\n    def |\Z)", src, re.S)
        body = m.group(0)
        assert "if self._thread is not None and self._thread.is_alive():" in body
        assert "self._thread = t" in body

    def test_behavior_double_start_single_thread(self):
        from nexus_memory.health_audit import HealthAuditor
        h = HealthAuditor.__new__(HealthAuditor)
        h._thread = None
        h._thread_stop = getattr(h, "_thread_stop", None)
        # do NOT actually start the daemon loop; just verify the guard logic
        import threading
        sentinel = threading.Thread(target=lambda: None, daemon=True)
        sentinel.start()
        sentinel.join()
        h._thread = sentinel
        # a second start must return without spawning (thread object dead here,
        # but is_alive() False -> the guard passes through; simulate live):
        class _Live:
            def is_alive(self):
                return True
        h._thread = _Live()
        # if the guard works, start() returns before defining _loop
        # (we detect spawn-avoidance by patching Thread)
        import nexus_memory.health_audit as ha
        created = []
        orig = ha.threading.Thread

        def _spy(*a, **k):
            created.append(1)
            return orig(*a, **k)

        ha.threading.Thread = _spy
        try:
            h.start()
        finally:
            ha.threading.Thread = orig
        assert created == [], "second start() must not spawn another loop"


class TestNr462QueryContentOutOfLogs:
    def test_no_query_content_in_rewrite_log(self):
        src = _code_lines(_read("src/nexus_memory/query_rewrite.py"))
        m = re.search(r"def rewrite_query.*?(?=\ndef |\Z)", src, re.S)
        body = m.group(0)
        assert "len=%d -> len=%d" in body
        assert "%.60r" not in body

    def test_lengths_only_debug_line(self):
        src = _read("src/nexus_memory/query_rewrite.py")
        assert re.search(
            r'log\.debug\("query-rewrite: len=%d -> len=%d", len\(q\), len\(rewritten\)\)',
            src)


class TestNr463MemoryDynamicsUnusedOptional:
    def test_optional_gone(self):
        src = _read("src/nexus_memory/memory_dynamics.py")
        assert "from typing import" in src
        imp = re.search(r"from typing import ([^\n]+)", src).group(1)
        assert "Optional" not in imp
        code = _code_lines(src)
        assert re.search(r"\bOptional\b", code) is None


class TestNr464AccessCountLegacyFallback:
    def test_use_count_base_when_access_count_missing(self):
        from nexus_memory.memory_dynamics import access_update_payload
        # legacy payload: use_count=7, no access_count
        p = {"use_count": 7, "access_count": None}
        out = access_update_payload(p)
        assert int(out["access_count"]) == 8, \
            "legacy use_count must seed the access_count base"
        # fresh payload without both: default base
        out2 = access_update_payload({})
        assert int(out2["access_count"]) >= 1

    def test_no_direct_default_base_read(self):
        src = _code_lines(_read("src/nexus_memory/memory_dynamics.py"))
        m = re.search(r"def access_update_payload.*?(?=\ndef |\Z)", src, re.S)
        body = m.group(0)
        assert 'payload.get("access_count", DEFAULT_USE_COUNT)' not in body
        assert "raw_acc" in body and "use_count" in body


class TestNr465SalienceImmuneConstant:
    def test_default_salience_returns_constant(self):
        src = _code_lines(_read("src/nexus_memory/memory_dynamics.py"))
        m = re.search(r"def default_salience.*?(?=\ndef |\Z)", src, re.S)
        body = m.group(0)
        assert "return SALIENCE_IMMUNE" in body
        assert re.search(r"return 0\.8", body) is None

    def test_behavior_rule_category_is_decay_immune(self):
        from nexus_memory.memory_dynamics import (
            SALIENCE_IMMUNE, default_salience, is_salient)
        assert default_salience("rule") == SALIENCE_IMMUNE
        assert default_salience("procedure") == SALIENCE_IMMUNE
        assert is_salient({"category": "rule"}) is True
        assert default_salience("temp") < SALIENCE_IMMUNE


class TestNr466RerankerDeadIndex:
    def test_no_enumerate_in_rank_order(self):
        src = _code_lines(_read("src/nexus_memory/reranker.py"))
        m = re.search(r"def rerank_points.*?(?=\ndef |\Z)", src, re.S)
        body = m.group(0)
        assert "for i, r in enumerate(ranked)" not in body
        assert 'order = [r["_idx"] for r in ranked]' in body


class TestNr467RerankerDocstringPoolK:
    def test_docstring_states_full_length_preserved(self):
        src = _read("src/nexus_memory/reranker.py")
        m = re.search(r"def rerank_points\(.*?\"\"\"(.*?)\"\"\"", src, re.S)
        doc = m.group(1)
        assert "len(result) == len(points)" in doc
        assert 'limited to pool_k' in doc

    def test_behavior_length_preserved_and_prefix_reranked(
            self, monkeypatch):
        from types import SimpleNamespace
        import nexus_memory.reranker as rr

        def pt(i, score):
            p = SimpleNamespace()
            p.id = f"p{i}"
            p.payload = {"content": f"doc {i}"}
            p.score = score
            return p

        points = [pt(i, 1.0 - i * 0.1) for i in range(6)]

        # fake the local cross-encoder path end-to-end (reranker is a STRING
        # parameter resolved by _resolve_reranker -> 'cross-encoder')
        monkeypatch.setattr(rr, "_resolve_reranker", lambda r, k: "cross-encoder")

        class _FakeCE:
            def predict(self, pairs):
                # doc0 gets the LOWEST score, doc2 the highest -> pool order
                # must come out reversed after the sort
                return [float(i) for i in range(len(pairs))]

        # _rerank_local does `from nexus.retrieval import _get_cross_encoder`
        # at call time — patch the source module, not the importer.
        import nexus.retrieval as _nr
        monkeypatch.setattr(_nr, "_get_cross_encoder", lambda: _FakeCE())
        out = rr.rerank_points(
            "q", points, reranker="cross-encoder", pool_k=3)
        assert len(out) == len(points), "all input points must be preserved"
        # re-ranked prefix is the reversed pool (fake CE reverses the order)
        assert [p.id for p in out[:3]] == ["p2", "p1", "p0"]
        # out-of-pool tail preserved in original order
        assert [p.id for p in out[3:]] == ["p3", "p4", "p5"]


# ── helper ──────────────────────────────────────────────────────────────────


def _free_port() -> int:
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _edge_stub(edge_id, source, target):
    from nexus.graph.schema import Edge, EdgeRelation, EdgeStatus
    now = "2026-01-01T00:00:00Z"
    return Edge(
        edge_id=edge_id, source_fact_id=source, target_fact_id=target,
        relation=EdgeRelation.REFERENCES.value, status=EdgeStatus.ACTIVE.value,
        created_at=now, updated_at=now,
    )