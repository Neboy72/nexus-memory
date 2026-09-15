"""Tests for OCR review Wave 8 — HIGH findings H1–H10.

One test class per finding. Hooks are loaded via importlib; network paths are
monkeypatched so the tests are hermetic and fast.
"""

from __future__ import annotations

import importlib.util
import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "plugins" / "claude-code" / "scripts"


def _load(name: str, path: Path | None = None):
    path = path or SCRIPTS / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"wave8_{name}", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


class _FakeResp:
    """Minimal urlopen() response context manager."""

    def __init__(self, payload):
        self._payload = json.dumps(payload).encode()

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


# ── H1: graph_traverse main() exits non-zero on error ────────────────────────


class TestH1GraphTraverseExitCode:
    @pytest.mark.parametrize("action", ["traverse", "subgraph", "related"])
    def test_missing_fact_id_exits_1(self, action):
        r = subprocess.run(
            [sys.executable, str(SCRIPTS / "graph_traverse.py"), "--action", action],
            capture_output=True, text=True, cwd=REPO_ROOT, timeout=60,
        )
        assert r.returncode == 1, r.stdout + r.stderr
        assert "error" in r.stdout

    def test_except_branch_and_validation_exit(self):
        src = (SCRIPTS / "graph_traverse.py").read_text()
        # 3 validation branches + unknown-action + except handler
        assert src.count("sys.exit(1)") >= 5


# ── H2: graph_boost fails closed on missing access_level ─────────────────────


class TestH2GraphBoostTrustDefault:
    def _mod(self, monkeypatch):
        monkeypatch.syspath_prepend(str(SCRIPTS))
        return _load("auto_recall")

    def _fake_urlopen(self, request, timeout=None):
        body = json.loads(request.data.decode())
        has_id = body["filter"]["must"][0]["has_id"][0]
        if has_id == "P1":
            return _FakeResp({"result": {"points": [{"payload": {"edges": [
                {"target_fact_id": "T1", "relation": "runs_on", "status": "active"},
            ]}}]}})
        return _FakeResp({"result": {"points": [{"payload": {"content": "SECRET-NO-LEVEL"}}]}})

    def test_missing_level_is_private_for_public_agent(self, monkeypatch):
        mod = self._mod(monkeypatch)
        monkeypatch.setattr("urllib.request.urlopen", self._fake_urlopen)
        boosted = mod.graph_boost([{"id": "P1"}], max_boost=3, access_level="public")
        assert boosted == []

    def test_missing_level_visible_for_private_agent(self, monkeypatch):
        mod = self._mod(monkeypatch)
        monkeypatch.setattr("urllib.request.urlopen", self._fake_urlopen)
        boosted = mod.graph_boost([{"id": "P1"}], max_boost=3, access_level="private")
        assert boosted == ["[graph:runs_on] SECRET-NO-LEVEL"]


# ── H3: guardrail fail-closed flag ───────────────────────────────────────────


class TestH3GuardrailFailClosed:
    def test_flag_parsing(self, monkeypatch):
        mod = _load("guardrail_check")
        monkeypatch.setenv("NEXUS_GUARDRAIL_FAIL_CLOSED", "1")
        assert mod.fail_closed_enabled() is True
        monkeypatch.setenv("NEXUS_GUARDRAIL_FAIL_CLOSED", "0")
        assert mod.fail_closed_enabled() is False

    def test_load_rules_outage_fail_closed_is_none(self, monkeypatch):
        mod = _load("guardrail_check")
        monkeypatch.setenv("NEXUS_GUARDRAIL_FAIL_CLOSED", "1")

        def boom(*a, **k):
            raise OSError("qdrant down")

        monkeypatch.setattr("urllib.request.urlopen", boom)
        assert mod.load_protection_rules() is None

    def test_load_rules_outage_fail_open_is_empty(self, monkeypatch):
        mod = _load("guardrail_check")
        monkeypatch.delenv("NEXUS_GUARDRAIL_FAIL_CLOSED", raising=False)

        def boom(*a, **k):
            raise OSError("qdrant down")

        monkeypatch.setattr("urllib.request.urlopen", boom)
        assert mod.load_protection_rules() == []

    def test_check_action_blocks_on_unavailable_rules(self, monkeypatch):
        mod = _load("guardrail_check")
        monkeypatch.setenv("NEXUS_GUARDRAIL_FAIL_CLOSED", "1")
        monkeypatch.setattr(mod, "load_protection_rules", lambda: None)
        res = mod.check_action("rm -rf /tmp/protected", "Bash", {})
        assert res["verdict"] == "block"

    def test_check_action_allows_when_no_rules_configured(self, monkeypatch):
        # fail-closed must NOT slow down the genuine "no rules" path
        mod = _load("guardrail_check")
        monkeypatch.setenv("NEXUS_GUARDRAIL_FAIL_CLOSED", "1")
        monkeypatch.setattr(mod, "load_protection_rules", lambda: [])
        res = mod.check_action("rm -rf /tmp/unprotected", "Bash", {})
        assert res["verdict"] == "allow"

    def test_inner_error_blocks_and_logs_when_fail_closed(self, monkeypatch, capsys):
        mod = _load("guardrail_check")
        monkeypatch.setenv("NEXUS_GUARDRAIL_FAIL_CLOSED", "1")
        monkeypatch.setattr(mod, "check_action",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(
            {"tool_name": "Bash", "tool_input": {"command": "rm -rf /x"}})))

        mod.main()

        captured = capsys.readouterr()
        assert json.loads(captured.out)["allow"] is False
        assert "inner error" in captured.err

    def test_inner_error_allows_when_fail_open(self, monkeypatch, capsys):
        mod = _load("guardrail_check")
        monkeypatch.delenv("NEXUS_GUARDRAIL_FAIL_CLOSED", raising=False)
        monkeypatch.setattr(mod, "check_action",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(
            {"tool_name": "Bash", "tool_input": {"command": "rm -rf /x"}})))

        mod.main()

        assert json.loads(capsys.readouterr().out)["allow"] is True


# ── H4: mcp_bootstrap resolves PATH before execv ─────────────────────────────


class TestH4BootstrapResolvedExecv:
    def test_execv_uses_resolved_path(self, monkeypatch):
        mod = _load("mcp_bootstrap")
        monkeypatch.setenv("NEXUS_PYTHON", "python3")  # bare name — not a file
        monkeypatch.setattr(mod, "interpreter_has_nexus", lambda py: True)
        monkeypatch.setattr(mod.shutil, "which", lambda py: "/resolved/bin/python3")

        calls = []

        def fake_execv(path, argv):
            calls.append((path, argv))
            raise RuntimeError("stop")

        monkeypatch.setattr(mod.os, "execv", fake_execv)

        with pytest.raises(RuntimeError, match="stop"):
            mod.main()

        assert calls[0][0] == "/resolved/bin/python3"
        assert calls[0][1] == ["/resolved/bin/python3", "-m", "nexus_memory.mcp_server"]


# ── H5: backfill terminates on real progress, not on scanned ─────────────────


class TestH5BackfillTermination:
    def _mod(self):
        return _load("backfill_consolidation",
                     REPO_ROOT / "scripts" / "backfill_consolidation.py")

    def test_failed_only_batch_terminates(self, monkeypatch):
        mod = self._mod()

        calls = {"n": 0}

        class FakeConsolidator:
            def __init__(self, *a, **k):
                pass

            def run(self, batch_size=0):
                calls["n"] += 1
                # scanned>0 but no durable progress (all points failed)
                return {"scanned": 25, "facts_created": 0, "duplicates": 0,
                        "skipped": 0, "failed": 25}

        monkeypatch.setattr(mod, "Consolidator", FakeConsolidator)
        monkeypatch.setattr("nexus_memory.mcp_server.MemoryStore", lambda *a, **k: object())
        monkeypatch.setattr(mod.time, "sleep", lambda s: None)
        monkeypatch.setattr(sys, "argv", ["backfill_consolidation.py"])

        rc = mod.main()

        assert rc == 0
        assert calls["n"] == 1  # would loop forever if it keyed off `scanned`

    def test_skipped_counts_as_progress(self, monkeypatch):
        mod = self._mod()

        reports = iter([
            {"scanned": 25, "facts_created": 0, "duplicates": 0, "skipped": 25, "failed": 0},
            {"scanned": 0, "facts_created": 0, "duplicates": 0, "skipped": 0, "failed": 0},
        ])
        calls = {"n": 0}

        class FakeConsolidator:
            def __init__(self, *a, **k):
                pass

            def run(self, batch_size=0):
                calls["n"] += 1
                return next(reports)

        monkeypatch.setattr(mod, "Consolidator", FakeConsolidator)
        monkeypatch.setattr("nexus_memory.mcp_server.MemoryStore", lambda *a, **k: object())
        monkeypatch.setattr(mod.time, "sleep", lambda s: None)
        monkeypatch.setattr(sys, "argv", ["backfill_consolidation.py"])

        assert mod.main() == 0
        assert calls["n"] == 2  # first batch progressed, second was empty


# ── H6/H7: scope_auto.fetch_centroids ────────────────────────────────────────


class TestH6MalformedPoints:
    def test_malformed_points_yield_empty_centroids(self, monkeypatch):
        sa = _load("scope_auto")
        points = [
            "not-a-dict",
            {"payload": "not-a-dict", "vector": [1.0, 0.0]},
            {"payload": {"scope": 123}, "vector": [1.0, 0.0]},
            {"payload": {"scope": "voice"}, "vector": "not-a-list"},
            {"payload": {"scope": "voice"}, "vector": [1.0, "x"]},
            {"payload": None, "vector": None},
        ]

        def handler(request, timeout=None):
            return _FakeResp({"result": {"points": points, "next_page_offset": None}})

        monkeypatch.setattr("urllib.request.urlopen", handler)
        assert sa.fetch_centroids("http://x", "nexus") == {}

    def test_valid_point_alongside_malformed_still_works(self, monkeypatch):
        sa = _load("scope_auto")
        points = [
            {"payload": {"scope": "voice"}, "vector": [1.0, 0.0]},
            {"payload": {"scope": 7}, "vector": [1.0, 0.0]},
            {"payload": {"scope": "voice"}, "vector": [0.0, "nan"]},
        ]

        def handler(request, timeout=None):
            return _FakeResp({"result": {"points": points, "next_page_offset": None}})

        monkeypatch.setattr("urllib.request.urlopen", handler)
        cents = sa.fetch_centroids("http://x", "nexus")
        assert set(cents) == {"voice"}


class TestH7ScrollFilterAndPagination:
    def test_scope_clause_present_and_paginates(self, monkeypatch):
        sa = _load("scope_auto")
        bodies: list = []
        pages = iter([
            {"result": {"points": [
                {"payload": {"scope": "voice"}, "vector": [1.0, 0.0]},
            ], "next_page_offset": "off2"}},
            {"result": {"points": [
                {"payload": {"scope": "voice"}, "vector": [0.0, 1.0]},
            ], "next_page_offset": None}},
        ])

        def handler(request, timeout=None):
            bodies.append(json.loads(request.data.decode()))
            return _FakeResp(next(pages))

        monkeypatch.setattr("urllib.request.urlopen", handler)
        cents = sa.fetch_centroids("http://x", "nexus")

        # H7a: scope clause is in the query filter (not just lifecycle_status)
        for b in bodies:
            must = b["filter"]["must"]
            assert any(c.get("key") == "scope" for c in must)
            assert any(c.get("key") == "lifecycle_status" for c in must)

        # H7b: follow next_page_offset for a second page, then stop
        assert len(bodies) == 2
        assert bodies[1].get("offset") == "off2"
        assert set(cents) == {"voice"}


# ── H8: auto_capture reads message.content blocks ────────────────────────────


class TestH8TranscriptExtraction:
    def _mod(self, monkeypatch):
        monkeypatch.syspath_prepend(str(SCRIPTS))
        return _load("auto_capture")

    def test_real_cc_nested_content(self, tmp_path, monkeypatch):
        mod = self._mod(monkeypatch)
        lines = [
            json.dumps({"type": "user", "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": [
                    {"type": "text", "text": "File created: /tmp/wave8_new.py"},
                ]},
            ]}}),
            json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "text", "text": "I decided to use the new parser for this."},
            ]}}),
        ]
        p = tmp_path / "transcript.jsonl"
        p.write_text("\n".join(lines))

        facts = mod.extract_facts_from_transcript(str(p), "sess-1")
        texts = " ".join(f["text"] for f in facts)
        assert "/tmp/wave8_new.py" in texts
        assert "decided to use the new parser" in texts

    def test_legacy_top_level_still_works(self, tmp_path, monkeypatch):
        mod = self._mod(monkeypatch)
        lines = [
            json.dumps({"type": "assistant", "content": [
                {"type": "text", "text": "We fixed the bug in the loader today."},
            ]}),
        ]
        p = tmp_path / "legacy.jsonl"
        p.write_text("\n".join(lines))

        facts = mod.extract_facts_from_transcript(str(p), "sess-2")
        assert any("fixed the bug" in f["text"] for f in facts)


# ── H9/H10: legacy trust-proof scripts ───────────────────────────────────────


class TestH9LegacyTestMcpAsserts:
    def test_trust_boundary_is_asserted_not_printed(self):
        src = (REPO_ROOT / "scripts" / "legacy" / "test_mcp.py").read_text()
        assert 'assert r["count"] == 0' in src
        assert 'assert r["count"] >= 1' in src


class TestH10LegacyTestMinimalRememberId:
    def test_forget_uses_remember_id(self):
        src = (REPO_ROOT / "scripts" / "legacy" / "test_minimal.py").read_text()
        assert 'remember_id = data.get("id")' in src
        assert '"memory_id": remember_id' in src
        # must no longer delete via the recall payload's id
        assert 'forget", {"memory_id": data.get("id", "")}' not in src
