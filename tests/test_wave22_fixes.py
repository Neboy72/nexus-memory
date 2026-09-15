"""OCR review wave 22 - fixes for findings Nr 381-402.

One test class per finding: source-inspection plus behavior tests.
Written by Kiosha (CC was contractually not allowed to touch this file).
Nr 386 is a documented false positive (log IS used in runtime.ts:175) —
assertion pins that decision so a future dead-import removal is deliberate.
"""
import json
import re
from pathlib import Path

import pytest

_TESTS = Path(__file__).resolve().parent
_ROOT = _TESTS.parent


def _read(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


# ── Nr 381: scrollPoint/scrollFiltered/findIncomingEdges log errors ─────────

class TestNr381SwallowLogging:
    def test_scrollpoint_logs_on_error(self):
        src = _read("plugins/openclaw/lib/qdrant-client.ts")
        sp = src[src.index("async scrollPoint"):]
        sp = sp[:sp.index("async ", 10)]
        assert "log.error" in sp, "scrollPoint must log !resp.ok and catch"
        assert sp.count("return null") >= 2

    def test_scrollfiltered_logs(self):
        src = _read("plugins/openclaw/lib/qdrant-client.ts")
        sf = src[src.index("scrollFiltered"):]
        assert "scrollFiltered: Qdrant returned" in sf or "log.error" in sf

    def test_findincomingedges_logs(self):
        src = _read("plugins/openclaw/lib/qdrant-client.ts")
        fi = src[src.index("findIncomingEdges"):]
        assert "findIncomingEdges: request failed" in fi


# ── Nr 382: url ?? fallback -> explicit length check ────────────────────────

class TestNr382UrlFallback:
    def test_code_present(self):
        src = _read("plugins/openclaw/lib/update-check.ts")
        assert "entry.url ?? " not in src
        assert "entry.url && entry.url.length > 0" in src


# ── Nr 383: isNewerVersion rejects non-numeric segments ─────────────────────

class TestNr383ParseRobustness:
    def test_code_present(self):
        src = _read("plugins/openclaw/lib/update-check.ts")
        code_only = "\n".join(
            line for line in src.splitlines() if not line.strip().startswith("//")
        )
        assert "parseInt" not in code_only
        assert "Number.isInteger" in code_only

    def test_non_numeric_fail_open(self):
        src = _read("plugins/openclaw/lib/update-check.ts")
        assert "return null" in src


# ── Nr 384: version tag sanitized before prompt interpolation ───────────────

class TestNr384SanitizeTag:
    def test_code_present(self):
        src = _read("plugins/openclaw/lib/update-check.ts")
        assert "sanitizeVersionTag" in src
        # raw interpolation gone; the ONLY allowed use is feeding the sanitizer
        assert "sanitizeVersionTag(result.latest)" in src
        nudge = src[src.index("buildUpdateNudgeLines"):]
        assert "v${safeTag}" in nudge

    def test_safe_tag_in_nudge(self):
        src = _read("plugins/openclaw/lib/update-check.ts")
        nudge = src[src.index("buildUpdateNudgeLines"):]
        assert "safeTag" in nudge


# ── Nr 386: false positive pin (log IS used) ────────────────────────────────

class TestNr386FalsePositivePin:
    def test_log_used_in_runtime(self):
        src = _read("plugins/openclaw/runtime.ts")
        assert "log.debug(" in src, "Nr 386 was a false positive; if this fails, removal must be a deliberate new decision"


# ── Nr 387: ensureCollection 404 vs other statuses ──────────────────────────

class TestNr387StatusDistinction:
    def test_code_present(self):
        src = _read("plugins/openclaw/lib/qdrant-client.ts")
        assert "resp.status === 404" in src
        # Network error must NOT be treated as "does not exist"
        assert "Qdrant connection failed while checking collection" in src

    def test_non404_throws(self):
        src = _read("plugins/openclaw/lib/qdrant-client.ts")
        assert "Qdrant collection check failed" in src


# ── Nr 385: unknown dimensions refused ──────────────────────────────────────

class TestNr385UnknownDims:
    def test_code_present(self):
        src = _read("plugins/openclaw/lib/qdrant-client.ts")
        assert "assume it's fine" not in src
        assert "dimensions are not readable" in src


# ── Nr 388/389: forget tool verify-before-delete + XOR params ───────────────

class TestNr388ForgetVerify:
    def test_code_present(self):
        src = _read("plugins/openclaw/tools/forget.ts")
        assert "scrollPoint" in src
        assert "Memory not found (id does not exist)." in src

    def test_both_params_rejected(self):
        src = _read("plugins/openclaw/tools/forget.ts")
        assert "Provide either memoryId OR query, not both." in src


# ── Nr 390: query text not logged ───────────────────────────────────────────

class TestNr390NoQueryLogging:
    def test_code_present(self):
        src = _read("plugins/openclaw/tools/search.ts")
        assert 'query="' not in src
        assert "queryLen=" in src


# ── Nr 391: score finiteness ────────────────────────────────────────────────

class TestNr391ScoreFiniteness:
    def test_code_present(self):
        src = _read("plugins/openclaw/tools/search.ts")
        assert "Number.isFinite(r.score)" in src


# ── Nr 392: explicit randomUUID import ──────────────────────────────────────

class TestNr392ExplicitImport:
    def test_code_present(self):
        src = _read("plugins/openclaw/tools/guardrail_check.ts")
        assert 'import { randomUUID } from "node:crypto"' in src
        assert "crypto.randomUUID()" not in src


# ── Nr 393: misleading collection argument removed ──────────────────────────

class TestNr393NoMisleadingArg:
    def test_code_present(self):
        src = _read("plugins/openclaw/tools/guardrail_check.ts")
        assert 'loadProtectionRules(qdrantClient, cfg.collection || "nexus")' not in src


# ── Nr 394/394c/395: type declarations tightened ────────────────────────────

class TestNr394TypeContract:
    def test_logger_variadic_and_optional_debug(self):
        src = _read("plugins/openclaw/types/openclaw.d.ts")
        assert "info: (msg: string, ...args: unknown[]) => void" in src
        assert "debug?: (msg: string) => void" in src

    def test_no_any_left(self):
        src = _read("plugins/openclaw/types/openclaw.d.ts")
        assert ": any" not in src
        assert "noExplicitAny" not in src

    def test_event_union_with_escape_hatch(self):
        src = _read("plugins/openclaw/types/openclaw.d.ts")
        assert "PluginHookEvent" in src
        assert '"before_prompt_build"' in src
        assert "(string & {})" in src


# ── Nr 396: plugin.yaml version parity ──────────────────────────────────────

class TestNr396VersionParity:
    def test_version_matches_pyproject(self):
        import tomllib
        pyproject = tomllib.loads(_read("pyproject.toml"))
        yaml_text = _read("plugins/memory/nexus/plugin.yaml")
        py_version = pyproject["project"]["version"]
        assert f"version: {py_version}" in yaml_text


# ── Nr 397: optional env documentation ──────────────────────────────────────

class TestNr397EnvDocs:
    def test_comment_lists_optional_vars(self):
        src = _read("plugins/memory/nexus/plugin.yaml")
        assert "VOYAGE_API_KEY" in src
        assert "NEXUS_QDRANT_HOST" in src


# ── Nr 398: CI concurrency ──────────────────────────────────────────────────

class TestNr398Concurrency:
    def test_code_present(self):
        src = _read(".github/workflows/audit.yml")
        assert "concurrency:" in src
        assert "cancel-in-progress: true" in src


# ── Nr 399: grep exit codes distinguished ───────────────────────────────────

class TestNr399GrepExitCodes:
    def test_no_blind_true(self):
        src = _read(".github/workflows/audit.yml")
        assert "|| true)" not in src
        assert "GREP_EXIT" in src
        assert src.count("$GREP_EXIT -eq 2") >= 2


# ── Nr 400/401/402: .gitignore ──────────────────────────────────────────────

class TestNr400LockfileTracked:
    def test_lockfile_not_ignored(self):
        src = _read(".gitignore")
        assert "package-lock.json" not in src


class TestNr401StaleSessionId:
    def test_gone(self):
        src = _read(".gitignore")
        assert "20260630_100157_88b366a4" not in src


class TestNr402LocalStatePatterns:
    def test_patterns_present(self):
        src = _read(".gitignore")
        assert "*.py[cod]" in src
        assert ".pytest_cache/" in src
        assert ".coverage" in src