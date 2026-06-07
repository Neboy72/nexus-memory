"""Classifier — Heuristic relation classification for Auto-Discovery.

v2.1.0: Determines the semantic relation between two facts using
only regex heuristics and content analysis. No LLM calls = zero token cost.

Strategies (in priority order):
  1. **Category match** → same category tag = ``references``
  2. **Explicit reference** → [[Wikilink]], "siehe X", "vgl. Y", "see also" = ``depends_on``
  3. **Keyword overlap** → high overlap (≥80%) but not category match = ``references``
  4. **Time-aware** → older fact referenced by newer = directed ``references``
"""

from __future__ import annotations

import logging
import re
from typing import Optional

_logger = logging.getLogger(__name__)

# ── Patterns for explicit reference detection ──────────────────────────────

WIKILINK_PATTERN = re.compile(r"\[\[([^\]]+)\]\]")
SEE_ALSO_PATTERN = re.compile(
    r"\b(siehe|vgl\.?|vergleiche|see also|see:|refer to|cf\.?)\b",
    re.IGNORECASE,
)
# Note: German keyword patterns are intentional — they enable German
# relation detection (requires, contradicts, etc.) for German-language
# corpora. Multi-language support can be added by extending these regexes.
DEPENDENCY_PATTERN = re.compile(
    r"\b(benötigt|benötigt |requires?|depends?\s+on|abhängig\s+von|based\s+on|"
    r"uses:|using|implemented\s+(with|using|via))\b",
    re.IGNORECASE,
)


def classify_relation(
    source_content: str,
    target_content: str,
    source_category: str,
    target_category: str,
    source_id: str,
    target_id: str,
    similarity_score: float,
) -> dict:
    """Classify the semantic relation between two facts.

    Args:
        source_content: Content of the source fact.
        target_content: Content of the target fact.
        source_category: Category tag of source.
        target_category: Category tag of target.
        source_id: ID of source fact.
        target_id: ID of target fact.
        similarity_score: Cosine similarity score (0.0–1.0).

    Returns:
        ``{"relation": str, "confidence": float, "reason": str}``
        where ``relation`` is one of: ``references``, ``depends_on``, ``supersedes``,
        ``contradicts``, ``supports``, ``alternative_to``.
    """
    # 1. Check for explicit references / dependencies (highest priority)
    explicit = _check_explicit_reference(source_content, target_content, source_id, target_id)
    if explicit:
        return explicit

    # 2. Check for contradiction signals
    contra = _check_contradiction(source_content, target_content)
    if contra:
        return contra

    # 3. Check for supersedes (same topic, similar but one is "newer" approach)
    supersedes = _check_supersedes(source_content, target_content, source_category, target_category)
    if supersedes:
        return supersedes

    # 4. Same category → references (most common auto-discovery result)
    if source_category and target_category and source_category == target_category:
        return {
            "relation": "references",
            "confidence": round(similarity_score * 0.9, 4),  # slight discount for category-only
            "reason": f"Same category '{source_category}' → references",
        }

    # 5. Keyword overlap ≥ 80% → references (secondary signal)
    overlap = _keyword_overlap(source_content, target_content)
    if overlap >= 0.80:
        return {
            "relation": "references",
            "confidence": round(similarity_score * overlap, 4),
            "reason": f"High keyword overlap ({overlap:.0%}) → references",
        }

    # 6. Fallback: below similarity threshold → no relation
    if similarity_score < 0.90:
        return None

    return {
        "relation": "references",
        "confidence": round(similarity_score * 0.85, 4),
        "reason": f"Semantic similarity ({similarity_score:.2f}) → references",
    }


# ── Internal heuristics ────────────────────────────────────────────────────


def _check_explicit_reference(
    source_content: str,
    target_content: str,
    source_id: str,
    target_id: str,
) -> Optional[dict]:
    """Check if source explicitly references or depends on target.

    Priority: Wikilinks > "siehe/vgl." > "depends_on/based_on" patterns.
    """
    source_lower = source_content.lower()
    target_lower = target_content.lower()

    # Extract key terms from target content for wikilink matching
    target_title = target_content.split("\n")[0][:100].strip() if target_content else ""
    target_keywords = set(re.findall(r"\b[a-zA-ZäöüßÄÖÜ]{4,}\b", target_lower))

    # 1. Wikilink pattern: [[Target Fact Name]]
    wikilinks = WIKILINK_PATTERN.findall(source_content)
    for link in wikilinks:
        link_lower = link.lower()
        # Match if wikilink contains a key term from target
        if any(kw in link_lower for kw in target_keywords if len(kw) > 3):
            return {
                "relation": "depends_on",
                "confidence": 0.95,
                "reason": f"Explicit wikilink [[{link}]] → depends_on",
            }
        # Wortgrenzen-Match (Miosha Review: verhindert "Open" in "OpenAir")
        link_escaped = re.escape(link_lower)
        if re.search(rf"(?<!\w){link_escaped}(?!\w)", target_lower):
            return {
                "relation": "depends_on",
                "confidence": 0.90,
                "reason": f"Wikilink [[{link}]] found in target → depends_on",
            }

    # 2. "siehe" / "vgl." / "see also" patterns
    if SEE_ALSO_PATTERN.search(source_content):
        # Check if a key target term appears nearby
        for kw in list(target_keywords)[:5]:
            # Look for pattern like "siehe [keyword]" in source
            nearby_pattern = re.compile(
                rf"\b(siehe|vgl\.?|see also)\b[^.]{{0,80}}{re.escape(kw)}",
                re.IGNORECASE,
            )
            if nearby_pattern.search(source_content):
                return {
                    "relation": "depends_on",
                    "confidence": 0.85,
                    "reason": f"See-also reference to '{kw}' → depends_on",
                }

    # 3. Dependency pattern (requires, depends on, based on)
    if DEPENDENCY_PATTERN.search(source_content):
        for kw in list(target_keywords)[:5]:
            if re.search(r'\b' + re.escape(kw) + r'\b', source_lower) and len(kw) > 4:
                return {
                    "relation": "depends_on",
                    "confidence": 0.80,
                    "reason": f"Dependency keyword + target term '{kw}' → depends_on",
                }

    return None


def _check_contradiction(source_content: str, target_content: str) -> Optional[dict]:
    """Check for contradiction signals between two facts.

    Looks for negation + same topic patterns or explicit "contradicts" keywords.
    """
    source_lower = source_content.lower()
    target_lower = target_content.lower()

    # Explicit contradiction keywords
    # Note: German contradiction keywords are intentional — they enable
    # German-language contradiction detection alongside English patterns.
    # Extend with additional languages as needed.
    contradicts_keywords = [
        r"\b(but|however|contrary|instead|actually|contradicts)\b",
        r"\b(widerspricht|aber|jedoch|stattdessen|tatsächlich)\b",
    ]

    has_contra_source = any(
        re.search(p, source_lower) for p in contradicts_keywords
    )
    has_contra_target = any(
        re.search(p, target_lower) for p in contradicts_keywords
    )

    if has_contra_source or has_contra_target:
        # Must share some topic to be a meaningful contradiction
        shared = set(re.findall(r"\b[a-zA-ZäöüßÄÖÜ]{5,}\b", source_lower)) & \
                 set(re.findall(r"\b[a-zA-ZäöüßÄÖÜ]{5,}\b", target_lower))
        if len(shared) >= 2:
            return {
                "relation": "contradicts",
                "confidence": 0.75,
                "reason": f"Contradiction keywords + shared topics ({', '.join(list(shared)[:3])})",
            }

    return None


def _check_supersedes(
    source_content: str,
    target_content: str,
    source_category: str,
    target_category: str,
) -> Optional[dict]:
    """Check if one fact supersedes another (same topic, newer approach).

    Only returns a result if categories match and version/newer language is detected.
    """
    source_lower = source_content.lower()
    target_lower = target_content.lower()

    if source_category != target_category:
        return None

    # Version markers
    version_markers = [
        r"\b(v?\d+\.\d+\.?\d*)\b",
        r"\b(newer|older|deprecated|legacy|current|latest)\b",
        r"\b(neu|alt|veraltet|aktuell|neueste)\b",
    ]

    has_version_source = any(re.search(p, source_lower) for p in version_markers)
    has_version_target = any(re.search(p, target_lower) for p in version_markers)

    if has_version_source or has_version_target:
        return {
            "relation": "supersedes",
            "confidence": 0.70,  # Lower confidence — manual verification advised
            "reason": "Version/language detected in same-category facts → supersedes",
        }

    return None


def _keyword_overlap(text_a: str, text_b: str) -> float:
    """Compute the Jaccard-like keyword overlap ratio.

    Only considers words ≥ 4 chars (filters out stop words implicitly).
    """
    words_a = set(re.findall(r"\b[a-zA-ZäöüßÄÖÜ]{4,}\b", text_a.lower()))
    words_b = set(re.findall(r"\b[a-zA-ZäöüßÄÖÜ]{4,}\b", text_b.lower()))

    if not words_a or not words_b:
        return 0.0

    intersection = words_a & words_b
    # Weighted: ratio of intersection to the SMALLER set (avoids false low for same-size)
    # If sets are very different sizes, use intersection/min(len) to find coverage
    smaller = min(len(words_a), len(words_b))
    return len(intersection) / smaller if smaller > 0 else 0.0
