"""Classifier — Heuristic relation classification for Auto-Discovery.

v2.1.0: Determines the semantic relation between two facts using
only regex heuristics and content analysis. No LLM calls = zero token cost.

Strategies (in priority order) — mirrors the branch order in
``classify_relation``:
  1. **Explicit reference** → [[Wikilink]], "siehe X", "vgl. Y", "see also",
     dependency patterns = ``depends_on``
  2. **Contradiction** → explicit marker (+ shared topics) or weak discourse
     cue (+ strong overlap) = ``contradicts``
  3. **Supersedes** → same category + version/newer language = ``supersedes``
  4. **Category match** → same category tag = ``references``
  5. **Keyword overlap** → high overlap (≥80%) = ``references``

There is deliberately no "time-aware" strategy — it was documented in an
earlier revision but never implemented.
"""

from __future__ import annotations

import re
from typing import Optional

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
) -> Optional[dict]:
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
        where ``relation`` is one of: ``references``, ``depends_on``,
        ``supersedes``, ``contradicts``.
        Returns ``None`` if no relation is found and the similarity score is
        below 0.90 (fallback threshold).
    """
    # 1. Check for explicit references / dependencies (highest priority)
    explicit = _check_explicit_reference(source_content, target_content)
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
        # Match if wikilink contains a key term from target.
        # H202: boundary-aware match — the previous bare substring test
        # (`kw in link_lower`) made e.g. "open" match inside "[[OpenAir]]".
        if any(
            re.search(rf"(?<!\w){re.escape(kw)}(?!\w)", link_lower)
            for kw in target_keywords if len(kw) > 3
        ):
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
        # H203: sorted() — a set slice depends on PYTHONHASHSEED and made
        # classification nondeterministic between runs.
        for kw in sorted(target_keywords)[:5]:
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
        # H203: sorted() — a set slice depends on PYTHONHASHSEED and made
        # classification nondeterministic between runs.
        for kw in sorted(target_keywords)[:5]:
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

    # H204: two signal classes. An *explicit* contradiction marker is strong
    # evidence on its own; the soft, extremely common discourse words
    # ("but"/"however"/"aber"/"jedoch") are not — they appear in ordinary
    # prose of the same domain, so they only count behind a much higher
    # shared-topic gate. That gate runs before the same-category/overlap
    # branches below, so an over-permissive test mislabels plain prose.
    explicit_marker = re.compile(
        r"\b(contradicts?|widerspruch|widerspricht|kontra)\b",
        re.IGNORECASE,
    )
    soft_marker = re.compile(
        r"\b(but|however|contrary|instead|actually|aber|jedoch|stattdessen|tatsächlich)\b",
        re.IGNORECASE,
    )

    has_explicit = bool(
        explicit_marker.search(source_lower) or explicit_marker.search(target_lower)
    )
    has_soft = bool(
        soft_marker.search(source_lower) or soft_marker.search(target_lower)
    )

    if not (has_explicit or has_soft):
        return None

    shared = set(re.findall(r"\b[a-zA-ZäöüßÄÖÜ]{5,}\b", source_lower)) & \
             set(re.findall(r"\b[a-zA-ZäöüßÄÖÜ]{5,}\b", target_lower))

    # Explicit marker: 0.75 with at least two shared topic tokens.
    if has_explicit and len(shared) >= 2:
        return {
            "relation": "contradicts",
            "confidence": 0.75,
            "reason": f"Explicit contradiction marker + shared topics "
                      f"({', '.join(sorted(shared)[:3])})",
        }
    # Soft marker alone: only with substantially stronger shared-topic
    # evidence (>= 5 shared tokens) and a lower confidence — 0.75 requires
    # the explicit marker.
    if has_soft and len(shared) >= 5:
        return {
            "relation": "contradicts",
            "confidence": 0.55,
            "reason": f"Weak contradiction cue + strong topic overlap "
                      f"({', '.join(sorted(shared)[:3])})",
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

    # No category on either side → cannot judge "same topic, newer approach".
    # Guards both the empty-string case ("" == "") and a missing category.
    if not source_category or source_category != target_category:
        return None

    # Real version strings only — "v2.0.1" / "1.2.3". A bare decimal ("2.5")
    # is not a version and must not trigger supersedes on its own (review #41).
    version_pattern = r"\bv?\d+\.\d+\.\d+\b"
    # Explicit newer/older language (review #41: still a valid signal).
    word_pattern = r"\b(newer|older|deprecated|legacy|current|latest|neu|alt|veraltet|aktuell|neueste)\b"

    has_version_source = re.search(version_pattern, source_lower) is not None
    has_version_target = re.search(version_pattern, target_lower) is not None
    has_word_source = re.search(word_pattern, source_lower) is not None
    has_word_target = re.search(word_pattern, target_lower) is not None

    # A real 3-component version string or explicit newer/older language
    # (review #41: the bug was bare decimals like "2.5" matching the old
    # pattern — those no longer match the tightened regex).
    has_version = has_version_source or has_version_target
    has_word = has_word_source or has_word_target
    if has_version or has_word:
        return {
            "relation": "supersedes",
            "confidence": 0.70,  # Lower confidence — manual verification advised
            "reason": "Version string + newer/older language in same-category facts → supersedes",
        }

    return None

    return None


# H205: guard — with only 1–2 distinct keywords a single shared long word
# trivially produced a score of 1.0 and cleared the 0.80 "references"
# threshold. Both sides must carry a minimal keyword mass to be comparable.
MIN_KEYWORDS_FOR_OVERLAP = 3


def _keyword_overlap(text_a: str, text_b: str) -> float:
    """Compute the Jaccard similarity of the two keyword sets.

    Only considers words ≥ 4 chars (filters out stop words implicitly).
    Jaccard = ``|A ∩ B| / |A ∪ B|``. The previous implementation divided by
    ``min(len)`` — a *coverage* ratio, not Jaccard — which returned 1.0
    whenever one set was a subset of the other (H205).
    """
    words_a = set(re.findall(r"\b[a-zA-ZäöüßÄÖÜ]{4,}\b", text_a.lower()))
    words_b = set(re.findall(r"\b[a-zA-ZäöüßÄÖÜ]{4,}\b", text_b.lower()))

    if len(words_a) < MIN_KEYWORDS_FOR_OVERLAP or len(words_b) < MIN_KEYWORDS_FOR_OVERLAP:
        return 0.0

    intersection = words_a & words_b
    union = words_a | words_b
    return len(intersection) / len(union) if union else 0.0
