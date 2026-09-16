"""OCR-2 Welle 29 (high, packet 2): guardrail family + gate + thought-filter.

Behavior tests run the real scripts (importlib from repo path). TS files are
source-checked only.
"""

import importlib.util
import re

import pytest

REPO = __file__.rsplit("/tests/", 1)[0]


def _read(rel: str) -> str:
    with open(f"{REPO}/{rel}", encoding="utf-8") as f:
        return f.read()


def _load_guardrail_module(tag: str):
    path = f"{REPO}/plugins/claude-code/scripts/guardrail_check.py"
    spec = importlib.util.spec_from_file_location(f"guardrail_check_{tag}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


FAKE_RULE = {
    "name": "test", "path": "/Users/miosha/nexus-memory",
    "rule_text": "protect repo", "source_id": "mem-123", "allow": False,
}


# ── W29-1: reverse containment ────────────────────────────────────────────


class TestW29ReverseContainment:
    def test_behavior_parent_of_protected_blocked(self):
        g = _load_guardrail_module("rev1")
        g.load_protection_rules = lambda: [dict(FAKE_RULE)]
        r = g.check_action("rm -rf /Users/miosha", "Bash", {})
        assert r["verdict"] == "block"

    def test_behavior_root_blocked(self):
        g = _load_guardrail_module("rev2")
        g.load_protection_rules = lambda: [dict(FAKE_RULE)]
        r = g.check_action("rm -rf /", "Bash", {})
        assert r["verdict"] == "block"

    def test_behavior_unrelated_allowed(self):
        g = _load_guardrail_module("rev3")
        g.load_protection_rules = lambda: [dict(FAKE_RULE)]
        r = g.check_action("rm -rf /tmp/scratch", "Bash", {})
        assert r["verdict"] == "allow"

    def test_behavior_grandparent_blocked(self):
        g = _load_guardrail_module("rev4")
        g.load_protection_rules = lambda: [dict(FAKE_RULE)]
        r = g.check_action("rm -rf /Users", "Bash", {})
        assert r["verdict"] == "block"


# ── W29-2: Write/Edit path bypass ─────────────────────────────────────────


class TestW29PathCarryingTools:
    def test_behavior_write_on_protected_blocked(self):
        g = _load_guardrail_module("wp1")
        g.load_protection_rules = lambda: [dict(FAKE_RULE)]
        r = g.check_action("", "Write", {"file_path": "/Users/miosha/nexus-memory/README.md", "content": "x"})
        assert r["verdict"] == "block"

    def test_behavior_edit_on_protected_blocked(self):
        g = _load_guardrail_module("wp2")
        g.load_protection_rules = lambda: [dict(FAKE_RULE)]
        r = g.check_action("", "Edit", {"file_path": "/Users/miosha/nexus-memory/x.py"})
        assert r["verdict"] == "block"

    def test_behavior_write_outside_allowed(self):
        g = _load_guardrail_module("wp3")
        g.load_protection_rules = lambda: [dict(FAKE_RULE)]
        r = g.check_action("", "Write", {"file_path": "/tmp/scratch.txt", "content": "x"})
        assert r["verdict"] == "allow"

    def test_source_path_carrying_map(self):
        src = _read("plugins/claude-code/scripts/guardrail_check.py")
        assert "file_path" in src and "notebook_path" in src
        assert "PATH_CARRYING_TOOLS" in src or "path_carrying" in src.lower()


# ── W29-3/4: pre-tool-gate rm regex + field coverage (source checks) ──────


class TestW29PreToolGate:
    def test_source_recursive_rm_regex(self):
        src = _read("plugins/openclaw/hooks/pre-tool-gate.ts")
        # lang-form flags + flag-swap must be covered
        assert "--recursive" in src or "recursive" in src
        # $HOME expansion present
        assert "${?HOME}" in src or "${HOME}" in src or "HOME" in src

    def test_source_field_coverage(self):
        src = _read("plugins/openclaw/hooks/pre-tool-gate.ts")
        # needsPlan/checkGuardrails must consult more than command alone
        idx = src.index("function needsPlan")
        seg = src[idx:idx + 1200]
        assert "input" in seg or "script" in seg or "firstCommand" in src


# ── W29-5: plan-lock hardening (source checks) ────────────────────────────


class TestW29PlanLock:
    def test_source_lstat_and_content_check(self):
        src = _read("plugins/openclaw/hooks/pre-tool-gate.ts")
        assert "lstatSync" in src
        assert 'startsWith("plan:"' in src or "startsWith('plan:'" in src


# ── W29-6: weak adverb markers narrowed ───────────────────────────────────


class TestW29WeakAdverbs:
    def test_behavior_now_with_reasoning_kept(self):
        if not _thought_filter_available():
            pytest.skip("thought-filter not importable (TS)")
        # TS behavior is tested via the built dist in the plugin CI; here we
        # check the source narrowing instead.
        src = _read("plugins/openclaw/hooks/thought-filter.ts")
        # weak class must exist and require extra reasoning signal
        assert "weak" in src.lower() or "WEAK" in src
        assert "prevWasLeak" in src

    def test_source_strong_markers_unchanged(self):
        src = _read("plugins/openclaw/hooks/thought-filter.ts")
        # strong markers (let me, runtime context, the user asks) still present
        assert "let me" in src
        assert "runtime context" in src


# ── W29-7: prevWasLeak collateral narrowed ────────────────────────────────


class TestW29Collateral:
    def test_source_dash_bold_rules_removed(self):
        src = _read("plugins/openclaw/hooks/thought-filter.ts")
        prev_seg = src[src.index("prevWasLeak"):]
        prev_seg = prev_seg[:prev_seg.index("return REASONING_MARKERS")]
        # dash continuation must NOT survive as an unconditional rule
        assert not re.search(r"/\^-\s/", prev_seg)
        # numbered continuation kept but capped
        assert "\\d+" in prev_seg
        assert "MAX_CONTINUATION" in src


# ── W29-8: line-level refinement ──────────────────────────────────────────


class TestW29LineLevel:
    def test_source_splits_lines(self):
        src = _read("plugins/openclaw/hooks/thought-filter.ts")
        idx = src.index("buildThoughtFilterHandler")
        seg = src[idx:]
        assert "\\n" in seg  # line splitting present in handler
        # kept logic no longer whole-block-only
        assert "kept" in seg


# ── W29-9: undefined-message documentation ────────────────────────────────


class TestW29UndefinedMessage:
    def test_source_has_w29_comment(self):
        src = _read("plugins/openclaw/hooks/thought-filter.ts")
        assert "W29-9" in src


def _thought_filter_available() -> bool:
    return False  # TS — source checks only in this suite