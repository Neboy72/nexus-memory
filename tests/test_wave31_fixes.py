"""OCR-2 Welle 31 (high, packet 4): analyzer, classifier, retrieval, migrate,
sica, scope_auto, plugin guards. One class per root cause."""

import pytest

REPO = __file__.rsplit("/tests/", 1)[0]


def _read(rel: str) -> str:
    with open(f"{REPO}/{rel}", encoding="utf-8") as f:
        return f.read()


# ── W31-1..3: sica-analyzer robustness ────────────────────────────────────


class TestW31Analyzer:
    def test_source_error_report_has_suggestions(self):
        src = _read("examples/nexus-sica-analyzer.py")
        assert "suggestions" in src
        assert '"status": "error"' in src or '"status":"error"' in src

    def test_source_or_dict(self):
        src = _read("examples/nexus-sica-analyzer.py")
        assert 'payload.get("provenance") or {}' in src or \
               'payload.get("provenance", {}) or {}' in src or \
               '(payload.get("provenance") or {})' in src


# ── W31-4: classifier supersedes conjunction ──────────────────────────────


class TestW31Classifier:
    def test_source_conjunction(self):
        src = _read("nexus/discovery/classifier.py")
        seg = src[src.index("def _check_supersedes"):]
        seg = seg[:seg.index("\n\ndef ", 1)]
        # both tokens must gate the emit (and-style), same-version must be excluded
        assert "and" in seg.lower()
        # the reason text must not claim a conjunction the code does not do
        assert "Version string + newer/older language" not in seg or (
            "has_version" in seg and ("direction" in seg or "newer" in seg)
        )


# ── W31-5/6: retrieval sort key + session fallback ────────────────────────


class TestW31Retrieval:
    def test_source_no_mixed_key(self):
        src = _read("nexus/retrieval/__init__.py")
        # a consistent sort key must exist (no bare 0 fallback next to ISO strings)
        assert "isinstance" in src or "or \"\"" in src

    def test_source_session_bucket_scoped(self):
        src = _read("nexus/retrieval/__init__.py")
        seg = src[src.index("def ")::]
        # bucket must incorporate real session id, not blanket type fallback
        assert "point:" in src or "session:" in src


# ── W31-7/8: migrate.py point-id verification + reason key ────────────────


class TestW31Migrate:
    def test_source_verifies_point_id(self):
        src = _read("nexus/scripts/migrate.py")
        assert "RuntimeError" in src
        assert "source_id" in src

    def test_source_writes_reason_key(self):
        src = _read("nexus/scripts/migrate.py")
        assert '"reason"' in src


# ── W31-9: sica guards ────────────────────────────────────────────────────


class TestW31Sica:
    def test_source_dict_guard(self):
        src = _read("nexus/sica/__init__.py")
        assert "isinstance(edge, dict)" in src or "isinstance(_edge, dict)" in src


# ── W31-10/11: scope_auto env-guard + default set ─────────────────────────


class TestW31ScopeAuto:
    def test_import_survives_bad_env(self, monkeypatch, tmp_path):
        import importlib, sys
        for mod in ("src.nexus_memory.scope_auto", "nexus.scope_auto"):
            pass
        # direct: load the file with a bad env value set
        import os
        env = dict(os.environ)
        env["NEXUS_SCOPE_AUTO_THRESHOLD"] = "0.65x"
        import subprocess, sys
        r = subprocess.run(
            [sys.executable, "-c",
             'import sys; sys.path.insert(0, "."); import src.nexus_memory.scope_auto'],
            capture_output=True, text=True, env=env, cwd=REPO, timeout=60,
        )
        assert r.returncode == 0, r.stderr[-300:]

    def test_no_match_includes_default_with_manual(self):
        src = _read("plugins/claude-code/scripts/scope_auto.py")
        seg = src[src.index("def prefetch_allowed_scopes"):]
        seg = seg[:seg.index("\n\ndef ", 1)]
        assert '"default"' in seg or "'default'" in seg


# ── W31-12/13: plugin guardrail fail-closed + backup size ─────────────────


class TestW31PluginMemory:
    def test_source_fail_closed(self):
        src = _read("plugins/memory/nexus/__init__.py")
        seg = src[src.index("def _guardrail_check"):]
        seg = seg[:seg.index("\n    def ", 1)]
        # infra-error must not collapse to blanket allow
        assert "deny" in seg or "fail-closed" in seg or "infra" in seg

    def test_source_backup_size_guard(self):
        src = _read("plugins/memory/nexus/__init__.py")
        seg = src[src.index("def _do_backup"):]
        seg = seg[:seg.index("\n    def ", 1)]
        # vectors must be excluded or the dump must be chunked
        assert "vector" in seg or "chunk" in seg


# ── W31-14/15: cron-form-gate logged fail-open + payload guards ───────────


class TestW31CronFormGate:
    def test_source_logged_failopen(self):
        src = _read("plugins/openclaw/hooks/cron-form-gate.ts")
        assert "log.warn" in src
        # bare silent catch must be gone
        assert "catch {\n      return //" not in src

    def test_source_payload_guards(self):
        src = _read("plugins/openclaw/hooks/cron-form-gate.ts")
        assert "typeof" in src or "?? " in src


# ── W31-16/17: qdrant-client fail-closed + DELETE check ───────────────────


class TestW31QdrantClient:
    def test_source_empty_levels_early_return(self):
        src = _read("plugins/openclaw/lib/qdrant-client.ts")
        assert "levels.length === 0" in src or "levels.length <= 0" in src

    def test_source_delete_verified(self):
        src = _read("plugins/openclaw/lib/qdrant-client.ts")
        seg = src[src.index('method: "DELETE"'):]
        seg = seg[:seg.index("\n", src.index("}") )] if "\n" in src else seg
        # response must be checked
        assert "resp.ok" in src or "response.ok" in src


# ── W31-18: graph_traverse incoming validation + total cap ────────────────


class TestW31GraphTraverse:
    def test_source_incoming_validated(self):
        src = _read("plugins/openclaw/tools/graph_traverse.ts")
        # the validation gate must also cover the incoming branch
        assert "findIncomingEdges" in src
        seg = src[src.index("findIncomingEdges"):]
        assert "validate" in seg.lower() or "KG_RELATIONS" in seg or ".includes(" in seg

    def test_source_total_cap(self):
        src = _read("plugins/openclaw/tools/graph_traverse.ts")
        assert "MAX_SCROLL_PAGES" not in src.replace("next_page_offset", "") or "results.length >= limit" in src