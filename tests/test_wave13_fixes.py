"""Tests for OCR review Wave 13 — infra findings H141-H144.

Covers the CI/gitignore half of the wave (the TypeScript findings H122-H140
live in ``plugins/openclaw/test-*.mjs`` and are run with ``node``):

- H141: audit.yml uses ``grep -rnE`` (ERE) for BAD_PATTERNS and the
  status_code check - plain grep (BRE) made both checks silent no-ops.
- H142: audit.yml declares least-privilege ``permissions`` and a
  ``timeout-minutes`` bound.
- H143: .gitignore ignores every env-file variant (whitelisting the example).
- H144: snapshot globs are anchored to the repo root so arbitrary source
  paths containing "-bak-" / starting with "2026" stay trackable.
"""

from __future__ import annotations

import subprocess

import pytest

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
AUDIT_YML = REPO_ROOT / ".github" / "workflows" / "audit.yml"
GITIGNORE = REPO_ROOT / ".gitignore"


# ── H141: ERE grep in audit.yml ──────────────────────────────────────────────


def test_bad_patterns_uses_ere_grep():
    text = AUDIT_YML.read_text()
    assert "grep -rnE \"$BAD_PATTERNS\"" in text
    # the old BRE invocation must be gone
    assert 'grep -rn "$BAD_PATTERNS"' not in text


def test_status_code_check_is_ere_without_double_backslash():
    line = next(
        l for l in AUDIT_YML.read_text().splitlines()
        if "status_code == 200" in l and "grep" in l
    )
    assert "grep -rnE" in line
    # the escaped-dot must be exactly one backslash: a doubled one would make
    # the pattern match nothing (verified with a probe during the fix).
    assert "\\\\." not in line
    assert "\\." in line


def _grep_probe(pattern: str, sample: str, tmp: Path) -> bool:
    tmp.write_text(sample)
    proc = subprocess.run(
        ["grep", "-rnE", pattern, str(tmp)],
        capture_output=True,
    )
    return proc.returncode == 0


def test_bad_patterns_ere_actually_matches(tmp_path):
    """The fixed invocation must match a file containing a stale name."""
    sample = 'COLLECTION = "openclaw-memory"\n'
    pattern = '("openclaw-memory"|"nexus_events")'
    assert _grep_probe(pattern, sample, tmp_path / "probe.py")
    assert not _grep_probe(pattern, "CLEAN = no stale names\n", tmp_path / "clean.py")


def test_status_code_ere_matches_raw_checks_but_not_is_success(tmp_path):
    assert _grep_probe(r"\.status_code == 200|\.status_code != 200",
                       "if r.status_code == 200:\n", tmp_path / "a.py")
    assert not _grep_probe(r"\.status_code == 200|\.status_code != 200",
                           "ok = is_success()\n", tmp_path / "b.py")


# ── H142: permissions + timeout ──────────────────────────────────────────────


def test_audit_declares_least_privilege_and_timeout():
    text = AUDIT_YML.read_text()
    assert "permissions:" in text
    assert "contents: read" in text
    assert "timeout-minutes:" in text


# ── H143: env-file variants ignored ──────────────────────────────────────────


def _ignored(path: str) -> bool:
    proc = subprocess.run(
        ["git", "check-ignore", "-q", path],
        cwd=REPO_ROOT,
        capture_output=True,
    )
    return proc.returncode == 0


def _git_available() -> bool:
    try:
        return subprocess.run(
            ["git", "--version"], capture_output=True
        ).returncode == 0
    except OSError:
        return False


needs_git = pytest.mark.skipif(
    not _git_available(),
    reason="git not installed (bare CI container); gitignore semantics are "
    "proven on git-bearing runners (GitHub runner, dev machines)",
)


@needs_git
def test_env_variants_are_ignored():
    for p in (".env.local", ".env.production", ".env.staging.local", "prod.env"):
        assert _ignored(p), f"{p} must be git-ignored"


def test_env_example_whitelist_is_declared():
    text = GITIGNORE.read_text()
    assert "!.env.example" in text


# ── H144: snapshot globs anchored ────────────────────────────────────────────


@needs_git
def test_root_snapshot_still_ignored():
    assert _ignored("20260915_1200_abc.md")
    assert _ignored("20260915_bak.md")


@needs_git
def test_arbitrary_subpaths_no_longer_untracked():
    assert not _ignored("docs/2026_notes.md")
    assert not _ignored("src/old-bak-thing.py")
    assert not _ignored("tests/test_2026_fixtures.py")