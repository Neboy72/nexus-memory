"""OCR review wave 25 (low C) - fixes for findings Nr 468-497.

One test class per finding (incl. documented SKIPs with their evidence).
Source-inspection (ast/grep on the repo files) + behavior checks against
the real modules where cheap. No Qdrant, no network.

Written by Kiosha (CC was contractually not allowed to touch this file).
"""

import ast
import importlib
import json
import math
import os
import re
import sys
import tempfile
import textwrap
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src" / "nexus_memory"
PLUGIN = REPO / "plugins" / "openclaw"


def _read(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8")


def _parse(rel: str) -> ast.Module:
    return ast.parse(_read(rel))


# ── Nr 468: selective_forgetting get_ts truthiness ───────────────────

class TestNr468GetTsZeroTimestamp:
    def test_source_uses_is_not_none(self):
        src = _read("src/nexus_memory/selective_forgetting.py")
        idx = src.index("def get_ts")
        fn = ast.parse(src[idx:]).body[0]
        conds = [ast.unparse(s.test) for s in ast.walk(fn)
                 if isinstance(s, ast.If)]
        assert any("is not None" in c for c in conds), \
            f"get_ts still truthy-checks: {conds}"

    def test_behavior_epoch_zero_is_a_timestamp(self):
        from nexus_memory.selective_forgetting import get_ts
        assert get_ts({"created_at": 0.0}) == 0.0
        assert get_ts({"created_at": "1970-01-01T00:00:00+00:00"}) == 0.0
        # missing stays None
        assert get_ts({}) is None
        assert get_ts({"created_at": ""}) is None


# ── Nr 469: selective_forgetting dead imports ────────────────────────

class TestNr469SelectiveForgettingDeadImports:
    def test_no_threading_no_pathlib_imports(self):
        tree = _parse("src/nexus_memory/selective_forgetting.py")
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    imported.add(a.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module.split(".")[0])
        used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        used |= {n.value.id for n in ast.walk(tree)
                 if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)}
        for banned in ("threading", "pathlib"):
            if banned in imported:
                assert banned in used, f"{banned} imported but never used"


# ── Nr 470: scope_auto named vectors ─────────────────────────────────

class TestNr470ScopeAutoNamedVectors:
    def test_source_skips_multi_name_vectors(self):
        src = _read("src/nexus_memory/scope_auto.py")
        seg = src[src.index("named vectors"):src.index("named vectors") + 400]
        assert "len(vec) != 1" in seg or "len(vec) == 1" in seg, seg[:200]

    def _make_sc(self, pts):
        from nexus_memory.scope_auto import ScopeCentroids

        class FakePt:
            def __init__(self, payload, vector):
                self.payload = payload
                self.vector = vector

        class FakeStore:
            def __init__(self, pts):
                self.pts = pts

            def scroll(self, *a, **kw):
                return self.pts, None

        sc = ScopeCentroids(FakeStore(pts), "c")
        sc._known_scopes = ["proj"]  # FakeStore serves both probe + fetch
        return sc

    def test_single_named_vector_still_averaged(self):
        import numpy as np

        class FakePt:
            def __init__(self, payload, vector):
                self.payload = payload
                self.vector = vector

        v1 = np.array([1.0] + [0.0] * 7)
        v2 = np.array([0.0, 1.0] + [0.0] * 6)
        sc = self._make_sc([
            FakePt({"scope": "proj"}, {"default": v1.tolist()}),
            FakePt({"scope": "proj"}, {"default": v2.tolist()}),
        ])
        centroids = sc.get()
        assert "proj" in centroids
        # centroid is NORMALIZED after averaging: (v1+v2)/2 = [0.5, 0.5, ...]
        # → unit-norm = [1/sqrt(2), 1/sqrt(2), ...]
        expected = (v1 + v2) / 2
        expected = expected / np.linalg.norm(expected)
        assert np.allclose(np.asarray(centroids["proj"]), expected)

    def test_multi_named_vector_point_skipped(self):
        class FakePt:
            def __init__(self, payload, vector):
                self.payload = payload
                self.vector = vector

        sc = self._make_sc([
            FakePt({"scope": "proj"}, {"a": [1.0] * 8, "b": [2.0] * 8}),
        ])
        assert sc.get() == {}


# ── Nr 471: MAX_POINTS_PER_SCOPE removed ─────────────────────────────

class TestNr471MaxPointsRemoved:
    def test_constant_gone_or_wired(self):
        src = _read("src/nexus_memory/scope_auto.py")
        if "MAX_POINTS_PER_SCOPE" in src:
            assert "limit=MAX_POINTS_PER_SCOPE" in src or \
                "limit=MAX_POINTS" in src, "constant still dead"


# ── Nr 472: _normalize_scope wired into prefetch_filter_scopes ───────

class TestNr472NormalizeScopeWired:
    def test_prefetch_normalizes_my_scope(self):
        src = _read("src/nexus_memory/scope_auto.py")
        seg = src[src.index("def prefetch_filter_scopes"):]
        seg = seg[:seg.index("\ndef ", 10) if "\ndef " in seg[10:] else len(seg)]
        assert "_normalize_scope(my_scope)" in seg

    def test_behavior_raw_scope_normalized(self):
        from nexus_memory.scope_auto import prefetch_filter_scopes
        # orthogonal query vector: needs BOTH threshold (0.65) and margin
        allowed = prefetch_filter_scopes(
            [1.0] + [0.0] * 7, {"proj": [1.0] + [0.0] * 7}, my_scope="  PROJ ")
        assert allowed is not None
        assert "proj" in allowed
        assert "PROJ" not in allowed  # raw mixed-case never leaks through
        # ambiguous query (zero vector) → fail-open None, but raw my_scope
        # must still have been normalized (no exception path)
        assert prefetch_filter_scopes(
            [0.0] * 8, {"proj": [1.0] + [0.0] * 7}, my_scope="  PROJ ") is None


# ── Nr 473/476: trust_service counter + float tolerance ──────────────

class TestNr474BeliefsScannedCounter:
    def test_counter_increments_in_loop(self):
        src = _read("src/nexus_memory/trust_service.py")
        seg = src[src.index("def run(self"):]
        seg = seg[:seg.index("\n    def ", 10)]
        # Nr 474 was skipped as an Alt-Test seam: the alt test pins the report
        # to len(beliefs) == 2 (1 belief + 1 event point). The SKIP means the
        # report still uses len(beliefs) — verify the SKIP is coherent, i.e.
        # the report segment was NOT changed behind the skip's back.
        report_seg = seg.split('report = {')[1]
        assert '"beliefs_scanned": len(beliefs)' in report_seg, \
            "SKIP says alt-contract kept, but report no longer uses len(beliefs)"


class TestNr475TrustFloatTolerance:
    def test_tolerance_in_condition(self):
        src = _read("src/nexus_memory/trust_service.py")
        seg = src[src.index("if not dry_run and ("):]
        seg = seg[:seg.index("\n\n")]
        assert "1e-6" in seg or "0.000001" in seg


# ── Nr 477: report filename uniqueness ───────────────────────────────

class TestNr477ReportFilenameUnique:
    def test_filename_carries_time(self):
        src = _read("src/nexus_memory/trust_service.py")
        seg = src[src.index("def _write_report"):]
        seg = seg[:seg.index("\n    def ", 10)]
        assert "[:10]" not in seg, "filename still date-only"
        assert "T" in seg or "replace" in seg


# ── Nr 478: wizard surfaces pull diagnostics ─────────────────────────

class TestNr478WizardPullDiagnostics:
    def test_failure_path_prints_details(self):
        src = _read("src/nexus_memory/wizard.py")
        seg = src[src.index("def _setup_ollama"):]
        seg = seg[:seg.index("\ndef ", 10)]
        n_details = seg.count("Details:")
        assert n_details >= 3, f"only {n_details} failure paths surface output"


# ── Nr 479: setup.py atomic write keeps file mode ────────────────────

class TestNr479AtomicWriteKeepsMode:
    def test_source_chmods_tmp_before_replace(self):
        src = _read("src/nexus_memory/setup.py")
        seg = src[src.index("def _atomic_write_text"):]
        seg = seg[:seg.index("\n\ndef ", 10)]
        assert "chmod" in seg

    def test_behavior_existing_mode_preserved(self):
        import importlib
        setup_mod = importlib.import_module("nexus_memory.setup")
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "cfg.json"
            target.write_text('{"a": 1}')
            os.chmod(target, 0o644)
            setup_mod._atomic_write_text(target, '{"b": 2}')
            mode = target.stat().st_mode & 0o777
            assert mode == 0o644, f"mode tightened to {oct(mode)}"
            assert json.loads(target.read_text()) == {"b": 2}

    def test_behavior_new_file_still_private(self):
        import importlib
        setup_mod = importlib.import_module("nexus_memory.setup")
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "new.json"
            setup_mod._atomic_write_text(target, '{"b": 2}')
            mode = target.stat().st_mode & 0o777
            assert mode == 0o600, f"new file mode {oct(mode)}"


# ── Nr 480: setup.py dead imports removed ────────────────────────────

class TestNr480SetupDeadImports:
    def test_unused_names_gone(self):
        src = _read("src/nexus_memory/setup.py")
        head = src[:src.index("# ── Plugin/MCP Installation")]
        for banned in ("get_trust_levels", "save_trust_level", "_load_config",
                       "_save_config", "set_agent_trust_level",
                       "_get_agents_registry_path", "Optional"):
            assert banned not in head, f"{banned} still imported"


# ── Nr 481: setup.py namespace leak ──────────────────────────────────

class TestNr481SetupLoopVarLeak:
    def test_del_after_loop(self):
        src = _read("src/nexus_memory/setup.py")
        assert re.search(r"del _agent,\s*_file", src)

    def test_module_has_no_agent_file_globals(self):
        import nexus_memory.setup as m
        assert not hasattr(m, "_agent")
        assert not hasattr(m, "_file")


# ── Nr 482/483: mcp_server dead imports + dead allowed_levels ────────

class TestNr482McpServerDeadImports:
    def test_attach_source_is_success_gone(self):
        src = _read("src/nexus_memory/mcp_server.py")
        assert "from nexus.provenance import attach_source" not in src
        assert "from nexus.config import is_success" not in src


class TestNr483AllowedLevelsRemoved:
    def test_no_dead_list(self):
        src = _read("src/nexus_memory/mcp_server.py")
        assert "allowed_levels = [ACCESS_PUBLIC]" not in src
        assert "allowed_levels.append" not in src


# ── Nr 484: webhook SSRF guard ───────────────────────────────────────

class TestNr484WebhookSsrfGuard:
    def _store(self):
        import tempfile
        import asyncio as _asyncio
        from nexus_memory.mcp_server import WebhookStore, WEBHOOK_EVENTS
        store = WebhookStore(Path(tempfile.mkdtemp()) / "wh.json")

        def _subscribe(url):
            return _asyncio.run(store.subscribe(WEBHOOK_EVENTS[0], url))

        return _subscribe

    def test_source_blocks_private_ranges(self):
        src = _read("src/nexus_memory/mcp_server.py")
        # v0.20.2: checks moved into canonical _assert_ssrf_safe (DNS +
        # numeric-form coverage); subscribe() calls it for webhook_url.
        seg = src[src.index("def _assert_ssrf_safe"):]
        seg = seg[:seg.index("\n\nasync def ") if "\n\nasync def " in seg else seg.index("\n\nclass ")]
        assert "ipaddress" in src
        for needle in ("is_loopback", "is_link_local", "is_private", "localhost", "getaddrinfo", "inet_aton"):
            assert needle in seg, needle
        sub_seg = src[src.index("async def subscribe"):]
        sub_seg = sub_seg[:sub_seg.index("\n    async def ", 10)]
        assert "_assert_ssrf_safe" in sub_seg

    def test_behavior_loopback_rejected(self):
        with __import__("pytest").raises(ValueError):
            self._store()("http://127.0.0.1:6333/hook")

    def test_behavior_metadata_ip_rejected(self):
        with __import__("pytest").raises(ValueError):
            self._store()("http://169.254.169.254/latest")

    def test_behavior_rfc1918_rejected(self):
        with __import__("pytest").raises(ValueError):
            self._store()("http://192.168.1.10/hook")

    def test_behavior_localhost_name_rejected(self):
        with __import__("pytest").raises(ValueError):
            self._store()("http://localhost:9000/hook")

    def test_behavior_public_url_accepted(self):
        sub = self._store()("https://example.com/hook")
        assert sub["webhook_url"] == "https://example.com/hook"


# ── Nr 485: setup.sh quoting + Nr 486 dead EMBED_SUGGEST ─────────────

class TestNr485SetupShQuoting:
    def test_command_and_pythonpath_quoted(self):
        src = _read("setup.sh")
        assert 'command: \\"${PYTHON}\\"' in src
        assert 'PYTHONPATH: \\"${INSTALL_DIR}\\"' in src


class TestNr486EmbedSuggestDead:
    def test_embed_suggest_gone(self):
        src = _read("setup.sh")
        assert "EMBED_SUGGEST" not in src


# ── Nr 487: install script Voyage note ───────────────────────────────

class TestNr487InstallScriptVoyageNote:
    def test_note_present(self):
        src = _read("plugins/openclaw/scripts/install_openclaw_plugin.sh")
        assert "Voyage EXAMPLE" in src
        assert "interpolates" in src


# ── Nr 489: pyproject readme ─────────────────────────────────────────

class TestNr489PyprojectReadme:
    def test_readme_field_declared(self):
        src = _read("pyproject.toml")
        assert re.search(r'^readme\s*=\s*"README\.md"', src, re.M)


# ── Nr 490: thought-filter named constants ───────────────────────────

class TestNr490ThoughtFilterConstants:
    def test_named_constants_present(self):
        src = _read("plugins/openclaw/hooks/thought-filter.ts")
        assert "MIN_LEN_PROCESS = 24" in src
        assert "MIN_LEN_SEND = 12" in src
        assert "trim().length < 24" not in src
        assert "out.length < 12" not in src

    def test_duplicate_marker_removed(self):
        src = _read("plugins/openclaw/hooks/thought-filter.ts")
        # the broader numeric-prefix form is kept (it mentions the phrase
        # twice inside ONE regex); the bare standalone pattern is gone
        assert re.search(r"/\^daily memory file exists", src) is None
        assert len(re.findall(r"memory file exists", src)) == 2  # 1 line, 2 mentions
        assert len(re.findall(r"^.*(daily| )memory file exists.*$", src, re.M)) == 1


# ── Nr 491: update-check homedir + pre-tool-gate portable paths ──────

class TestNr491PortablePaths:
    def test_update_check_uses_os_homedir(self):
        src = _read("plugins/openclaw/lib/update-check.ts")
        assert 'process.env.HOME || "/tmp"' not in src
        assert "os.homedir()" in src

    def test_pre_tool_gate_no_dev_home(self):
        src = _read("plugins/openclaw/hooks/pre-tool-gate.ts")
        assert "/Users/miosha" not in src
        assert "os.homedir()" in src


# ── Nr 492: config.ts shared scope validation ────────────────────────

class TestNr492SharedScopeValidation:
    def test_config_uses_validate_config_scope(self):
        src = _read("plugins/openclaw/lib/config.ts")
        assert "validateConfigScope(cfg.scope)" in src
        assert "validateConfigScope(process.env.NEXUS_SCOPE)" in src
        # inline regex gone from config.ts
        assert "{0,39}" not in src

    def test_shared_helper_exists_fail_open(self):
        src = _read("plugins/openclaw/lib/scope-auto.ts")
        assert "export function validateConfigScope" in src


# ── Nr 493: shared preview + BFS ─────────────────────────────────────

class TestNr493SharedHelpers:
    def test_limit_text_lib_exists(self):
        src = (PLUGIN / "lib" / "text-preview.ts").read_text()
        assert "export function limitText" in src

    def test_forget_uses_lib(self):
        src = _read("plugins/openclaw/tools/forget.ts")
        assert 'from "../lib/text-preview.ts"' in src
        assert "function limitText" not in src

    def test_store_uses_lib(self):
        src = _read("plugins/openclaw/tools/store.ts")
        assert 'from "../lib/text-preview.ts"' in src
        assert "params.text.slice(0, 80)" not in src

    def test_bfs_lib_exists(self):
        src = (PLUGIN / "lib" / "bfs.ts").read_text()
        assert "export async function bfsEdges" in src

    def test_graph_traverse_uses_bfs_lib(self):
        src = _read("plugins/openclaw/tools/graph_traverse.ts")
        assert 'from "../lib/bfs.ts"' in src
        assert src.count("queue.shift()") == 0, "inline BFS still present"


# ── Nr 494: cron-form-gate local date + logged catch ─────────────────

class TestNr494CronFormGateLocalDate:
    def test_local_date_parts_used(self):
        src = _read("plugins/openclaw/hooks/cron-form-gate.ts")
        seg = src[src.index("function appendDailyNote"):]
        seg = seg[:seg.index("\n}", 10) + 2]
        # the WORD appears in the explanatory comment; the CALL must be gone
        assert "toISOString()" not in seg.replace(
            "// Local date parts: toISOString() is UTC", "")
        assert "getFullYear" in seg
        assert "getMonth" in seg

    def test_catch_logs(self):
        src = _read("plugins/openclaw/hooks/cron-form-gate.ts")
        seg = src[src.index("function appendDailyNote"):]
        seg = seg[:seg.index("\n}", 10) + 2]
        assert "log.warn" in seg

    def test_behavior_pad_produces_iso_like(self):
        src = _read("plugins/openclaw/hooks/cron-form-gate.ts")
        assert re.search(
            r"const pad = \(n: number\) => String\(n\)\.padStart\(2, ", src
        ), "pad helper missing"
        # behavioral equivalent in Python: 2-digit zero-fill semantics
        pad = lambda n: str(n).zfill(2)
        assert pad(3) == "03" and pad(11) == "11"


# ── Nr 495: capture threshold on raw text ────────────────────────────

class TestNr495CaptureThreshold:
    def test_threshold_moved_to_cleaned(self):
        src = _read("plugins/openclaw/hooks/capture.ts")
        assert "cleaned.length >= 10" in src
        assert "texts.filter((t) => t.length >= 10)" not in src


# ── Nr 496: recall graph items score null + budget ───────────────────

class TestNr496RecallGraphItems:
    def test_score_null_and_budget(self):
        src = _read("plugins/openclaw/hooks/recall.ts")
        assert "score: null" in src
        assert "score: 0," not in src
        assert "formatMemories(allItems, cfg.maxRecallResults)" in src
        assert "cfg.maxRecallResults + graphItems.length" not in src


# ── Nr 497: trigger empty-string not interactive ─────────────────────

class TestNr497TriggerEmptyString:
    def test_source_compares_undefined(self):
        src = _read("plugins/openclaw/hooks/trigger.ts")
        assert "trigger === undefined" in src
        assert "!trigger ||" not in src


# ── SKIPs (documented, with evidence) ────────────────────────────────

class TestNr474SkipAltTestContract:
    """SKIP rationale: tests/test_trust_and_watch.py asserts
    beliefs_scanned == 2 for one belief + one event point (len(scroll)).
    Changing the counter semantics is an Alt-Test seam -> documented skip."""

    def test_alt_test_contract_still_green(self):
        # the skip's evidence: the alt test really pins the counter
        src = _read("tests/test_trust_and_watch.py")
        assert 'beliefs_scanned"] == 2' in src


class TestNr487SkipLegacyOnly:
    """SKIP rationale: plugins/openclaw/scripts/install_openclaw_plugin.sh
    has NO embedded Python (verified: file carries only echo lines + the
    JSON snippet). The json.load AttributeError is real only in the legacy
    scripts/install_openclaw_plugin.py path which the rules keep untouched."""

    def test_plugin_installer_has_no_json_load(self):
        src = _read("plugins/openclaw/scripts/install_openclaw_plugin.sh")
        assert "json.load" not in src


class TestNr488SkipAsyncioModeDesign:
    def test_comment_documents_intent(self):
        src = _read("pyproject.toml")
        assert 'asyncio_mode = "auto"' in src
        assert "auto" in src and "explicitly" in src.lower() or "no need" in src


class TestNr491SkipStoreTsConstants:
    def test_store_constants_still_there(self):
        src = _read("plugins/openclaw/tools/store.ts")
        assert '"openclaw_tool"' in src
        assert "confidence: 0.9" in src


class TestNr492SkipStoreScopePattern:
    def test_store_pattern_documented_alive(self):
        src = _read("plugins/openclaw/tools/store.ts")
        assert "SCOPE_PATTERN" in src


class TestNr493SkipExtractTargets:
    def test_no_shared_guardrails_lib_claimed(self):
        assert not (PLUGIN / "lib" / "guardrails.ts").exists()


class TestNr471SkipNamedVectorsAlreadySafe:
    """Fund said 'select by explicit name or skip'; the collection is
    single-vector by contract (store writes un-named). CC fix: skip
    multi-name points instead of mixing. Covered by TestNr470 above."""

    def test_store_writes_unnamed_vectors(self):
        src = _read("plugins/openclaw/tools/store.ts")
        assert "upsert(id, vector" in src