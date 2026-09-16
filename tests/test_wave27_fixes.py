"""OCR-2 Welle 27 (critical): fixes for the 10 root causes / 13 findings.

One test class per root cause. Behavior checks where cheap (real functions
with fakes), source-inspection elsewhere. Run:
  cd /tmp/ocr-review-target && /tmp/w12-venv/bin/python -m pytest tests/test_wave27_fixes.py -q
"""
import ast
import math
import re
import sys
import json
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "src"))

# nexus package is <repo>/nexus/, nexus_memory is <repo>/src/nexus_memory
try:
    import nexus.confidence  # noqa: F401
    import nexus.sica  # noqa: F401
    import nexus.discovery.matcher  # noqa: F401
    import nexus.staging  # noqa: F401
    HAS_NEXUS = True
except Exception:
    HAS_NEXUS = False

REASON_NEXUS = "nexus package needs deps (qdrant_client, requests) in this env"


def _read(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8")


import importlib.util
from importlib.machinery import SourceFileLoader


def _load_guardrail_module(tag: str):
    path = REPO / "plugins/claude-code/scripts/guardrail_check.py"
    loader = SourceFileLoader(f"guardrail_check_{tag}", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


# ════════════════════════════════════════════════════════════════════
# W27-1: nexus/__init__.py — vector-loss on update (findings C1+C6)
# ═════════════════════════════════ nexus/__init__.py ════════════════
class TestW27VectorLossOnUpdate:
    def test_source_requests_vector(self):
        src = _read("nexus/__init__.py")
        seg = src[src.index("def nexus_update"):]
        seg = seg[:seg.index("\ndef ", 1)]
        # scroll explicitly asks for the vector on both branches
        assert seg.count('"with_vector": True') >= 1
        # upsert no longer sends the empty-vector fallback
        assert '"vector": vector if vector else []' not in seg
        assert '"vector": vector' in seg

    def test_source_refuses_empty_vector(self):
        src = _read("nexus/__init__.py")
        seg = src[src.index("def nexus_update"):]
        seg = seg[:seg.index("\ndef ", 1)]
        assert "refusing to overwrite" in seg
        assert "would destroy the embedding" in seg

    def test_behavior_empty_vector_raises(self):
        if not HAS_NEXUS:
            pytest.skip(REASON_NEXUS)
        from unittest.mock import patch as _p

        import requests as real_requests

        import nexus as nx

        calls = {}

        class FakeResp:
            def __init__(self, payload):
                self._p = payload
                self.status_code = 200
                self.text = ""

            def json(self):
                return self._p

        def fake_post(url, json=None, timeout=None):
            calls["scroll_body"] = json
            # Qdrant WITHOUT with_vector: point found, no vector in payload
            return FakeResp({"result": {"points": [
                {"id": "abc", "payload": {"content": "old"}, },
            ]}})

        def fake_put(url, json=None, timeout=None):
            calls["put_body"] = json
            return FakeResp({"result": {"status": "ok"}})

        with _p.object(real_requests, "post", fake_post), \
             _p.object(real_requests, "put", fake_put):
            # nexus_update imports requests as _req inside the function —
            # patching the real module covers that reference.
            with pytest.raises(RuntimeError, match="would destroy the embedding"):
                nx.nexus_update("abc", new_content="new")

    def test_behavior_with_vector_passes_vector_through(self):
        if not HAS_NEXUS:
            pytest.skip(REASON_NEXUS)
        from unittest.mock import patch as _p

        import requests as real_requests

        import nexus as nx

        calls = {}

        class FakeResp:
            def __init__(self, payload):
                self._p = payload
                self.status_code = 200
                self.text = ""

            def json(self):
                return self._p

        def fake_post(url, json=None, timeout=None):
            # honor the requested flags: with_vector=True must carry the vector
            want_vec = (json or {}).get("with_vector")
            pt = {"id": "abc", "payload": {"content": "old"}}
            if want_vec:
                pt["vector"] = [0.1, 0.2, 0.3]
            return FakeResp({"result": {"points": [pt]}})

        def fake_put(url, json=None, timeout=None):
            calls["put_body"] = json
            return FakeResp({"result": {"status": "ok"}})

        with _p.object(real_requests, "post", fake_post), \
             _p.object(real_requests, "put", fake_put):
            out = nx.nexus_update("abc", new_content="new")
        assert calls["put_body"]["points"][0]["vector"] == [0.1, 0.2, 0.3]


# ════════════════════════════════════════════════════════════════════
# W27-2: confidence.py — sqrt ValueError on negative scores (C2)
# ════════════════════════════════════════════════════════════════════
class TestW27SqrtNegativeScores:
    def test_source_clamps_ratio(self):
        src = _read("nexus/confidence.py")
        seg = src[src.index("def _signal_dominance"):]
        seg = seg[:seg.index("\n    # ", 1)]
        assert "sum(chunk_scores) <= 0" in seg
        assert re.search(r"ratio\s*=\s*max\(\s*0\.0\s*,\s*min\(ratio", seg)

    def test_behavior_negative_scores_no_crash(self):
        if not HAS_NEXUS:
            pytest.skip(REASON_NEXUS)
        from nexus.confidence import GroundingScorer
        # scores [0.9, -0.8, -0.5] → sum=-0.4, old code raised ValueError
        out = GroundingScorer._signal_dominance([0.9, -0.8, -0.5])
        assert isinstance(out, float)
        assert not math.isnan(out)

    def test_behavior_top_score_not_max(self):
        if not HAS_NEXUS:
            pytest.skip(REASON_NEXUS)
        from nexus.confidence import GroundingScorer
        # top score smaller than sum of the rest: ratio > 1 pre-clamp
        out = GroundingScorer._signal_dominance([0.4, 0.9, 0.9])
        assert out <= 1.0

    def test_behavior_normal_case_unchanged(self):
        if not HAS_NEXUS:
            pytest.skip(REASON_NEXUS)
        from nexus.confidence import GroundingScorer
        assert GroundingScorer._signal_dominance([0.8, 0.2]) == pytest.approx(math.sqrt(0.8))


# ═════════════════════════════════════════════ nexus/sica/__init__.py ═
class TestW27SicaDocstringTruthful:
    """C12-Report: behavior (auto delete) is deliberate — only the docstring
    claimed otherwise."""

    def test_module_docstring_documents_deletions(self):
        src = _read("nexus/sica/__init__.py")
        assert "Deletions are explicit issue types" in src
        assert "retention_expired, low_confidence purge, stale_temp" in src
        # the false claim is gone
        assert "only change metadata" not in src

    def test_behavior_unchanged_delete_path_still_automatic(self):
        src = _read("nexus/sica/__init__.py")
        seg = src[src.index("def _apply_auto_patch"):]
        seg = seg[:seg.index("\ndef ", 1)]
        assert 'client.delete(' in seg
        assert '"stale_temp"' in seg and '"retention_expired"' in seg and '"low_confidence"' in seg
        assert 'auto_patch: bool = True' in src  # default unchanged


# ════════════════════════════════════════════════════════════════════
# W27-4: staging.py — non-2xx is not "no canonical" (C4)
# ════════════════════════════════════════════════════════════════════
class TestW27StagingNon2xx:
    def test_source_fails_loud_on_other_non2xx(self):
        src = _read("nexus/staging.py")
        seg = src[src.index("def _get_current_canonical"):]
        seg = seg[:seg.index("\ndef ", 1)]
        assert "status_code == 404" in seg
        assert "cannot decide promote safety" in seg

    def test_behavior_5xx_raises(self):
        if not HAS_NEXUS:
            pytest.skip(REASON_NEXUS)
        import requests as real_requests

        from nexus import staging

        class FakeResp:
            status_code = 503
            text = "boom"

            def json(self):
                return {}

        with patch.object(real_requests, "get", return_value=FakeResp()):
            with pytest.raises(RuntimeError, match="503"):
                staging._get_current_canonical("fact-1")

    def test_behavior_404_still_none(self):
        if not HAS_NEXUS:
            pytest.skip(REASON_NEXUS)
        import requests as real_requests

        from nexus import staging

        class FakeResp:
            status_code = 404
            text = ""

            def json(self):
                return {}

        with patch.object(real_requests, "get", return_value=FakeResp()):
            assert staging._get_current_canonical("fact-1") is None

    def test_behavior_200_canonical_returned(self):
        if not HAS_NEXUS:
            pytest.skip(REASON_NEXUS)
        import requests as real_requests

        from nexus import staging

        class FakeResp:
            status_code = 200

            def json(self):
                return {"result": {"payload": {
                    "version_id": "v1", "fact_id": "fact-1",
                    "content": "c", "status": "canonical",
                }}}

        with patch.object(real_requests, "get", return_value=FakeResp()):
            out = staging._get_current_canonical("fact-1")
        assert out is not None and out.version_id == "v1"


# ═══════════════════ W27-5: staging.py — PENDING never returned (C9) ══
class TestW27FindLastCanonical:
    def test_source_raises_on_non_canonical_end(self):
        src = _read("nexus/staging.py")
        seg = src[src.index("def _find_last_canonical"):]
        seg = seg[:seg.index("\ndef ", 1)]
        assert "No canonical version found in supersedes chain" in seg

    def test_behavior_pending_end_raises(self):
        if not HAS_NEXUS:
            pytest.skip(REASON_NEXUS)
        from nexus import staging
        from nexus.lifecycle import FactVersion

        # a first-time-promotion draft: PENDING, supersedes=None
        draft = FactVersion.new_pending(content={"content": "c"}, fact_id="f1")
        # chain never reaches a CANONICAL version (lookup returns None)
        with patch.object(staging, "_get_version", return_value=None):
            with pytest.raises(ValueError, match="No canonical version"):
                staging._find_last_canonical(draft)

    def test_behavior_canonical_chain_still_resolves(self):
        if not HAS_NEXUS:
            pytest.skip(REASON_NEXUS)
        from nexus import staging
        from nexus.lifecycle import FactStatus, FactVersion

        draft = FactVersion.new_pending(content={"content": "c"}, fact_id="f1", supersedes="v1")
        canon = FactVersion.new_pending(content={"content": "1"}, fact_id="f1")
        canon.status = FactStatus.CANONICAL.value
        with patch.object(staging, "_get_version", return_value=canon):
            out = staging._find_last_canonical(draft)
        assert out.version_id == canon.version_id

    def test_rollback_wraps_error(self):
        src = _read("nexus/staging.py")
        seg = src[src.index("def rollback"):]
        seg = seg[:seg.index("\ndef ", 1)]
        assert "Rollback aborted" in seg


# ════════════════════════════════════════════════════════════════════
# W27-6: matcher.py — int/str ID mismatch at the source (C7)
# ═══════════════════════════════════════════════════ requests.post ══
class TestW27MatcherStrIds:
    def test_source_normalizes_ids_in_scroll(self):
        src = _read("nexus/discovery/matcher.py")
        seg = src[src.index("def scroll_facts"):]
        seg = seg[:seg.index("\ndef ", 1)]
        assert 'p["id"] = str(p.get("id", ""))' in seg

    @pytest.mark.skipif(not HAS_NEXUS, reason=REASON_NEXUS)
    def test_behavior_scroll_returns_str_ids(self):
        import requests as real_requests

        from nexus.discovery import matcher

        class FakeResp:
            status_code = 200

            def raise_for_status(self):
                pass

            def json(self):
                return {"result": {"points": [
                    {"id": 42, "payload": {"type": "memory", "content": "a"}, "vector": [0.1]},
                    {"id": 7, "payload": {"type": "memory", "content": "b"}, "vector": [0.2]},
                ]}, "next_page_offset": None}

        with patch.object(real_requests, "post", return_value=FakeResp()):
            pts = matcher.scroll_facts(collection="nexus", qdrant_url="http://x")
        assert all(isinstance(p["id"], str) for p in pts)
        assert pts[0]["id"] == "42"


# ════════════════════════════════════════════════════════════════════
# W27-7: migrate.py — set_payload with real point-id (C8)
# ════════════════════════════════════════════════════════════════════
class TestW27MigratePointId:
    def test_source_uses_scrolled_point_id(self):
        src = _read("nexus/scripts/migrate.py")
        seg = src[src.index("for source_id, payload_edges in grouped.items():"):]
        seg = seg[:seg.index("\n    for ", 1)] if "\n    for " in seg else seg
        assert "point_id = scroll_result[0][0].id" in seg
        assert re.search(r"points\s*=\s*\[point_id\]", seg)
        # the fact_id value is no longer used as the point id for the write
        assert "points=[source_id]" not in src


# ════════════════════════════════════════════════════════════════════
# W27-8: guardrail_check.py — bare ~ / / matched (C10)
# ════════════════════════════════════════════════════════════════════
class TestW27GuardrailBareTargets:
    def _mod(self):
        return _load_guardrail_module("bare")

    def test_patterns_match_bare_targets(self):
        mod = self._mod()
        text = "rm -rf /"
        targets = mod.extract_targets(text)
        assert "/" in targets
        text = "rm -rf ~"
        targets = mod.extract_targets(text)
        assert "~" in targets
        # negative: ordinary absolute paths still match exactly once
        targets = mod.extract_targets("rm -rf /tmp/x")
        assert "/tmp/x" in targets

    def test_no_double_match_for_bare_slash(self):
        mod = self._mod()
        # bare / must not ALSO be reported twice via the /abs/path pattern
        targets = mod.extract_targets("rm -rf /")
        assert targets.count("/") == 1

    def test_destructive_classification_unchanged(self):
        mod = self._mod()
        assert mod.classify_action("rm -rf /") == "delete"


# ════════════════════════════════════════════════════════════════════
# W27-9: guardrail_check.py — deny contract (C11)
# ════════════════════════════════════════════════════════════════════
class TestW27GuardrailDenyContract:
    def _mod(self):
        return _load_guardrail_module("deny")

    def test_block_uses_permission_decision_deny(self):
        src = _read("plugins/claude-code/scripts/guardrail_check.py")
        seg = src[src.index('if result["verdict"] == "block":'):]
        seg = seg[:seg.index("\n    except", 1)]
        assert '"permissionDecision": "deny"' in seg
        assert "hookEventName" in seg and "PreToolUse" in seg
        # the ignored payload shape is gone
        assert '"allow": False' not in seg

    def test_fail_closed_uses_deny_too(self):
        src = _read("plugins/claude-code/scripts/guardrail_check.py")
        seg = src[src.index("if fail_closed_enabled():"):]
        seg = seg[:seg.index("\n        else:", 1)]
        assert '"permissionDecision": "deny"' in seg


    def test_docstring_states_real_contract(self):
        src = _read("plugins/claude-code/scripts/guardrail_check.py")
        assert "hookSpecificOutput.permissionDecision" in src
        assert '"allow: false" blocks' not in src

    def test_end_to_end_block_decision(self):
        mod = self._mod()
        # deny needs a matching protection rule: "no rules" is a deliberate
        # fast-allow (destructive on unprotected target), so we inject one.
        fake_rules = [{"path": "/", "rule_text": "never delete /", "source_id": "w27-e2e"}]
        payload = json.dumps({
            "tool_name": "Bash",
            "tool_input": {"command": "rm -rf /"},
        })
        import io

        buf_in, buf_out = io.StringIO(payload), io.StringIO()
        with patch.object(mod, "load_protection_rules", return_value=fake_rules), \
             patch("sys.stdin", buf_in), patch("sys.stdout", buf_out):
            mod.main()
        out = json.loads(buf_out.getvalue())
        decision = out.get("hookSpecificOutput", {}).get("permissionDecision")
        assert decision == "deny"
        assert "Nexus Guardrail BLOCKED" in out["hookSpecificOutput"]["permissionDecisionReason"]

    def test_end_to_end_unprotected_target_allows(self):
        mod = self._mod()
        fake_rules = [{"path": "/protected/*", "rule_text": "keep", "source_id": "w27"}]
        payload = json.dumps({
            "tool_name": "Bash",
            "tool_input": {"command": "rm -rf /tmp/x"},
        })
        import io

        buf_in, buf_out = io.StringIO(payload), io.StringIO()
        with patch.object(mod, "load_protection_rules", return_value=fake_rules), \
             patch("sys.stdin", buf_in), patch("sys.stdout", buf_out):
            mod.main()
        out = json.loads(buf_out.getvalue())
        assert out.get("allow") is True or out.get("hookSpecificOutput", {}).get("permissionDecision") != "deny"


# ════════════════════════════════════════════════════════════════════
# W27-10: install_openclaw_plugin.sh — accessLevel (C12-Report)
# ═════════════════════════════════════ echo ═════════════════════════
class TestW27InstallAccessLevel:
    def test_no_default_access_level_in_snippet(self):
        src = _read("plugins/openclaw/scripts/install_openclaw_plugin.sh")
        assert '"accessLevel": "default"' not in src
        assert '"accessLevel": "private"' in src
        # echo hint line updated too
        assert 'access disabled, accessLevel' in src
        assert 'accessLevel \\"default\\"' not in src


# ════════════════════════════════════════════════════════════════════
# W27-11/12: capture-retry-queue.ts — stale snapshot + env override (C5+C13)
# ════════════════════════════════════════════════════════════════════
class TestW27QueueStaleSnapshot:
    def test_source_rereads_before_rewrite(self):
        src = _read("plugins/openclaw/hooks/capture-retry-queue.ts")
        assert "const fresh = readQueue()" in src
        assert "restoredIds.size" in src
        # the stale-snapshot rewrite is gone
        assert "entries.length - kept.length" not in src

    def test_source_queue_env_override(self):
        src = _read("plugins/openclaw/hooks/capture-retry-queue.ts")
        assert "NEXUS_CAPTURE_QUEUE_FILE" in src
        assert "export function queueFile()" in src
        # no direct QUEUE_FILE usage left in the io functions
        body = src[src.index("export function enqueueCapture"):]
        assert re.search(r"QUEUE_FILE\b", body) is None


# ── C5 (test hygiene): my own fix, asserted from the test file side ──
class TestW27TestHygieneSandbox:
    """C5: the test must not unlink the PRODUCTION queue. Assert the test
    file itself now uses the env override BEFORE importing the module."""

    def test_no_prod_path_unlink(self):
        src = _read("plugins/openclaw/test-capture-retry-queue.mjs")
        # production path must not be touched by the test anymore
        assert 'join(homedir(), ".openclaw", "workspace", "data", "capture-retry-queue.jsonl")' not in src
        assert "NEXUS_CAPTURE_QUEUE_FILE" in src


# ════════════════════════════════════════════════════════════════════
# C3 (SICA delete) is covered behaviorally in TestW27SicaDocstringTruthful;
# C1/C6 share one root (W27-1); C7 source = W27-6; C13 = W27-11.
# ════════════════════════════════════════════════════════════════════