"""
Tiered Enrichment — automatic memory enrichment at storage time.

Separated from ``nexus_remember()`` so tier logic can grow independently.
See :class:`Enricher` and :class:`EnrichmentTier`.

Tiers:
    **T1 (Raw)** — Store as-is, no enrichment.  Default for bulk / low-value data.
    **T2 (Tagged)** — Validate category, extract keywords, add enrichment metadata.
    **T3 (Linked)** — T2 + semantically link to existing memories, flag contradictions.
"""

from __future__ import annotations
import re
from enum import IntEnum


# ── Known category taxonomy ──────────────────────────────────────────────────
# Grows over time.  Unknown categories are flagged but not rejected.
# H211: the canonical MemoryCategory enum is the source of truth — it is
# unioned in below so newly added canonical categories (belief, session,
# procedure, temp …) can never drift out of this set again and be flagged
# with a bogus `unknown_category:<cat>` warning.
try:
    from nexus import MemoryCategory as _MemoryCategory
    _CANONICAL_CATEGORIES = frozenset(c.value for c in _MemoryCategory)
except Exception:  # pragma: no cover — import-cycle safety, enrichment stays usable
    _CANONICAL_CATEGORIES = frozenset()

KNOWN_CATEGORIES = frozenset({
    "fact", "rule", "config", "decision", "preference",
    "architecture", "pattern", "lesson", "goal", "project",
    "person", "tool", "workflow", "log", "query",
}) | _CANONICAL_CATEGORIES

# ── Keyword extraction patterns ──────────────────────────────────────────────
KEYWORD_PATTERNS = [
    re.compile(r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*\b"),  # Proper nouns
    re.compile(r"\b(?:API|CLI|IDE|URL|JSON|YAML|TOML|XML|HTTP|SSH|DNS|IP)\b"),  # Acronyms
    re.compile(r"\bv?\d+\.\d+\.\d+\b"),  # Version numbers
    re.compile(r"\b(?:http|https|ftp)://\S+\b"),  # URLs
]

HEURISTIC_HIGH_SIGNAL = re.compile(
    # H212: \b anchors — without them the alternatives matched inside
    # unrelated words ("key" in monkey/keyboard, "nie" in denied, "secret"
    # in secretary, "immer" in glimmer) and spuriously promoted to LINKED.
    r"\b(?:muss|darf|nie|immer|verboten|erlaubt|required|mandatory|"
    r"critical|blocker|produktion|production|passwort|password|secret|key)\b",
    re.IGNORECASE,
)


# ── EnrichmentTier ────────────────────────────────────────────────────────────
class EnrichmentTier(IntEnum):
    """Enrichment depth for a memory entry."""

    RAW = 1      # T1: store as-is
    TAGGED = 2   # T2: validate + extract keywords
    LINKED = 3   # T3: T2 + semantic linking

    @classmethod
    def from_str(cls, value: str | int | None) -> EnrichmentTier:
        """Parse a tier from string/int/None.  Returns ``RAW`` on failure."""
        if value is None:
            return cls.RAW
        if isinstance(value, cls):
            return value
        if isinstance(value, int):
            try:
                return cls(value)
            except ValueError:
                pass
        try:
            return cls[value.upper()]
        except (KeyError, AttributeError):
            pass
        return cls.RAW


# ── Heuristics ────────────────────────────────────────────────────────────────
IMPORTANCE_TIER_MAP: dict[str, EnrichmentTier] = {
    "high": EnrichmentTier.LINKED,
    "medium": EnrichmentTier.TAGGED,
    "low": EnrichmentTier.RAW,
}

CATEGORY_TIER_MAP: dict[str, EnrichmentTier] = {
    "rule": EnrichmentTier.LINKED,
    "config": EnrichmentTier.LINKED,
    "architecture": EnrichmentTier.LINKED,
    "pattern": EnrichmentTier.LINKED,
    "decision": EnrichmentTier.TAGGED,
    "preference": EnrichmentTier.TAGGED,
    "goal": EnrichmentTier.TAGGED,
    "lesson": EnrichmentTier.TAGGED,
    "project": EnrichmentTier.TAGGED,
}


def decide_tier(
    content: str,
    category: str,
    importance: str | None = None,
) -> EnrichmentTier:
    """Decide enrichment tier based on content heuristics.

    * Explicit ``importance`` field takes highest precedence.
    * Known high-value categories imply T2/T3.
    * Content length + signal words bump T1 → T2, T2 → T3.
    * Default: T1 (conservative — never enrich where uncertain).

    Args:
        content: The memory content text.
        category: Category tag (e.g. ``\"fact\"``, ``\"rule\"``).
        importance: Explicit importance hint (``\"low\"``, ``\"medium\"``,
            ``\"high\"``, or ``None``).

    Returns:
        :class:`EnrichmentTier` — one of ``RAW``, ``TAGGED``, ``LINKED``.
    """
    # 1. Explicit importance (highest precedence)
    if importance:
        tier = IMPORTANCE_TIER_MAP.get(importance.lower())
        if tier is not None:
            return tier

    # 2. Category-based
    tier = CATEGORY_TIER_MAP.get(category)
    if tier is not None:
        return tier

    # 3. Content heuristics
    content_len = len(content.strip())

    # High-signal keywords → LINKED
    if HEURISTIC_HIGH_SIGNAL.search(content):
        return EnrichmentTier.LINKED

    # Long content → TAGGED. Length-only heuristic: this branch checks nothing
    # but ``content_len`` (no KEYWORD_PATTERNS / proper-noun detection).
    if content_len > 300:
        return EnrichmentTier.TAGGED

    # 4. Default
    return EnrichmentTier.RAW


def enrich(tier: EnrichmentTier, payload: dict) -> dict:
    """Enrich *payload* in-place according to *tier*.

    Args:
        tier: The target enrichment tier.
        payload: The memory payload dict (mutated in-place).

    Returns:
        The enriched payload dict (same reference, mutated).
    """
    if tier == EnrichmentTier.RAW:
        payload["_enrichment_tier"] = 1
        return payload

    # ── T2+: category validation ──────────────────────────────────────────
    cat = payload.get("category", "fact")
    if cat not in KNOWN_CATEGORIES:
        payload["_enrichment_warnings"] = payload.get("_enrichment_warnings", [])
        payload["_enrichment_warnings"].append(f"unknown_category:{cat}")

    # ── T2+: keyword extraction ───────────────────────────────────────────
    # H213: `or ""` — a payload whose "content" key exists but is None (callers
    # merging metadata/**kwargs over the payload can null it out) made
    # pattern.finditer(None) raise TypeError.
    content = payload.get("content") or ""
    keywords = set()
    for pattern in KEYWORD_PATTERNS:
        for match in pattern.finditer(content):
            kw = match.group().strip()
            if 2 <= len(kw) <= 60:
                keywords.add(kw)
    if keywords:
        payload["_keywords"] = sorted(keywords)

    payload["_enrichment_tier"] = 2

    if tier == EnrichmentTier.TAGGED:
        return payload

    # ── T3: semantic linking (placeholder for future) ─────────────────────
    # For now, T3 = T2 + a note that linking was attempted.
    # Full semantic linking requires querying existing memories via
    # nexus_search, which depends on Qdrant being reachable — that
    # belongs in the caller (nexus_remember), not here.
    payload["_linking_attempted"] = True
    payload["_enrichment_tier"] = 3
    return payload
