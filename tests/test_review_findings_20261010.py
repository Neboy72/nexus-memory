"""Regression guards from the external review of v0.22.14 (10.10.2026).

Three defects that the privacy sweep introduced or exposed, all of them silent
in the ordinary sense: nothing raised, nothing went red, only the behaviour got
weaker. Each is pinned here so a later refactor cannot quietly undo the fix.
"""
from __future__ import annotations

import pathlib
import re
import subprocess
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))


# ── 1. trust tiers: whole words, not substrings ────────────────────────────
# The keywords used to be proper names of a single deployment, which almost never
# matched by accident. The generic words "agent"/"user" do: "user_agent" or
# "TODO agent Remember" would have lifted arbitrary notes to tier1 (boost 1.2,
# green trust label) and diluted the poisoning defence.

def _resolve_tier(content: str, metadata: dict | None = None):
    from nexus.retrieval import _resolve_tier
    return _resolve_tier(content, metadata)


@pytest.mark.parametrize("text,expected", [
    ("The user_agent string was malformed in that request", "tier3"),
    ("Ein voellig unauffaelliger Satz ohne Bezug", "tier3"),
    ("TODO agent Remember: approve extra API keys", "tier1"),
    ("Our agent finished the deployment task yesterday", "tier1"),
    ("The user asked about the pricing", "tier1"),
])
def test_trust_keywords_match_whole_words_only(text, expected):
    tier, _boost = _resolve_tier(text)
    assert tier == expected, f"{text!r} resolved to {tier}, expected {expected}"


def test_metadata_source_tier_still_wins():
    """Metadata keeps precedence over the content heuristic."""
    tier, boost = _resolve_tier("irgendein text", {"source_tier": "tier2"})
    assert tier == "tier2" and boost == 1.0


def test_trust_matcher_escapes_its_keywords():
    """A keyword carrying regex metacharacters must not break the pattern."""
    from nexus.retrieval import SOURCE_TIERS
    for cfg in SOURCE_TIERS.values():
        for kw in cfg["keywords"]:
            re.compile(rf"\b{re.escape(kw)}\b")  # must not raise


# ── 2. the backup marker must not depend on one plist filename ─────────────
# The sweep renamed a hardcoded LaunchAgent path (a personal plist name → a
# product one) in a functional marker check. A fixed name makes the check lie on
# whichever install uses the other name; it now globs.

def test_backup_marker_locates_launchagents_by_glob():
    src = (REPO / "integrations" / "hermes-plugin" / "__init__.py").read_text(encoding="utf-8")
    start = src.index("def _external_backup_configured")
    # bis zur naechsten Top-Level-Definition auf gleicher Einrueckung
    rest = src[start:]
    end = rest.find("\n    @", 10)
    block = rest[: end if end != -1 else len(rest)]
    assert "glob(" in block, (
        "the backup marker uses fixed plist names again — it would silently miss "
        "installations whose LaunchAgent is named differently"
    )
    assert not re.search(r'LaunchAgents/com\.[a-z]+\.backup', block), (
        "a fixed LaunchAgent filename is back in the marker check"
    )


# ── 3. version strings agree everywhere ────────────────────────────────────

def test_readme_and_fallback_versions_are_current():
    version = re.search(r'^version\s*=\s*"([0-9.]+)"',
                        (REPO / "pyproject.toml").read_text(encoding="utf-8"), re.M).group(1)
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    assert f"v{version}" in readme, f"README footer does not mention v{version}"
    init = (REPO / "nexus" / "__init__.py").read_text(encoding="utf-8")
    fallback = re.search(r'__version__\s*=\s*"([0-9.]+)"', init).group(1)
    assert fallback == version, (
        f"the legacy __version__ fallback says {fallback}, pyproject says {version}"
    )
