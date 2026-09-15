"""Auto-Discovery — Automatische Relationserkennung zwischen Facts.

v2.1.0: Erkennt semantische Relationen zwischen Facts ohne LLM.
Nutzt Qdrant-native Vector Search + Regex-Heuristiken.
Keine neuen Dependencies. Null Token-Kosten.

v2.2.0: Edges in Qdrant-Payloads statt SQLite.

Ablauf:
  1. Scanne alle canonical Facts aus Qdrant (scroll)
  2. For each fact: Qdrant search for similar facts (O(n·k) instead of O(n²))
  3. Threshold-Filter (≥ 0.85)
  4. Heuristische Klassifikation der Relation
  5. Dedup-Check gegen Qdrant-Payloads (statt SQLite)
  6. Insert als active (confidence ≥ 0.85) oder proposed (< 0.85)

Usage::

    from nexus.discovery import AutoDiscovery

    ad = AutoDiscovery()
    ad.initialize()
    results = ad.discover_all()
"""

from __future__ import annotations

import logging
from typing import Optional

from nexus.discovery.matcher import scroll_facts, search_similar_facts
from nexus.discovery.classifier import classify_relation
from nexus.discovery.dedup import filter_new_edges
from nexus.graph.store import EdgeStore

_logger = logging.getLogger(__name__)

DEFAULT_QDRANT_URL = "http://localhost:6333"
DEFAULT_COLLECTION = None  # Kein Default — muss aus Config kommen
DEFAULT_TOP_K = 5

# Confidence thresholds (from Miosha review, confirmed 27.05.2026)
AUTO_ACTIVE_THRESHOLD = 0.85  # ≥ 0.85 → insert as active
MIN_DISCOVERY_THRESHOLD = 0.70  # < 0.70 → skip (too noisy)


def classify_confidence(confidence: float) -> dict:
    """Determine if and how an edge should be inserted based on confidence.

    Returns:
        ``{"should_insert": bool, "as_proposed": bool}``.
    """
    if confidence < MIN_DISCOVERY_THRESHOLD:
        return {"should_insert": False, "as_proposed": False}
    if confidence < AUTO_ACTIVE_THRESHOLD:
        return {"should_insert": True, "as_proposed": True}
    return {"should_insert": True, "as_proposed": False}


def _extract_content(payload: dict) -> str:
    """Extract content text from a Qdrant point payload.

    Handles both plain-string and dict-wrapped content fields.
    Some Qdrant points store content as {content: "...", category: "..."}
    instead of a plain string — this unpacks those safely.
    """
    content = payload.get("content", "") or payload.get("text", "")
    if isinstance(content, dict):
        return content.get("content", "") or content.get("text", "")
    return content


def _extract_category(payload: dict) -> str:
    """Extract category from a Qdrant point payload.

    Handles dict-wrapped content fields where category may be nested.
    """
    cat = payload.get("category", "")
    if not cat:
        content = payload.get("content", "")
        if isinstance(content, dict):
            return content.get("category", "")
    return cat


class AutoDiscovery:
    """Automatic relation discovery between canonical facts.

    Scans all canonical facts in Qdrant, finds similar pairs,
    classifies relations, and stores edges in Qdrant-Payloads.
    """

    def __init__(
        self,
        store: Optional[EdgeStore] = None,
        qdrant_url: str = DEFAULT_QDRANT_URL,
        collection: Optional[str] = DEFAULT_COLLECTION,
        top_k: int = DEFAULT_TOP_K,
    ):
        if collection is None:
            raise ValueError(
                "collection must be explicitly provided. "
                "Set it in your config (e.g., plugins.nexus-memory.nexus_collection) "
                "and pass it to AutoDiscovery(). "
                "Code-default removed to prevent silent collection mismatches."
            )
        self._qdrant_url = qdrant_url
        self._collection = collection
        self._top_k = top_k
        self._store = store or EdgeStore(
            qdrant_url=qdrant_url,
            collection=collection,
        )

    @property
    def store(self) -> EdgeStore:
        return self._store

    def initialize(self) -> None:
        """Ensure the EdgeStore schema exists."""
        self._store.initialize()
        _logger.info(
            "AutoDiscovery initialized (Qdrant=%s, collection=%s)",
            self._qdrant_url, self._collection,
        )

    # ── Main pipeline ─────────────────────────────────────────────────────

    def discover_all(
        self,
        categories: Optional[list[str]] = None,
    ) -> dict:
        """Run full discovery pipeline: scan → match → classify → dedup → store.

        Args:
            categories: Optional filter — only discover within these categories.

        Returns:
            Summary dict with stats.
        """
        # 1. Scroll all canonical facts
        try:
            facts = scroll_facts(
                qdrant_url=self._qdrant_url,
                collection=self._collection,
                with_vectors=True,
            )
        except RuntimeError as e:
            # Review #46: matcher raises instead of returning partial/empty —
            # surface it as a failed run instead of "no facts found".
            _logger.error("Discovery scroll failed: %s", e)
            return {
                "total_facts_scanned": 0,
                "similarity_queries_run": 0,
                "candidates_found": 0,
                "after_dedup": 0,
                "inserted_active": 0,
                "inserted_proposed": 0,
                "errors": [str(e)],
                "status": "scroll_failed",
            }

        # Filter by category if specified
        if categories:
            facts = [
                f for f in facts
                if _extract_category(f.get("payload", {})) in categories
            ]

        if not facts:
            return {
                "total_facts_scanned": 0,
                "similarity_queries_run": 0,
                "candidates_found": 0,
                "after_dedup": 0,
                "inserted_active": 0,
                "inserted_proposed": 0,
                "errors": [],
                "status": "no_facts",
            }

        # Build content-by-id lookup
        fact_map = {f["id"]: f for f in facts}

        # 2. For each fact: find similar via Qdrant
        all_candidates: list[dict] = []
        seen_pairs: set[tuple[str, str]] = set()
        errors: list[str] = []
        queries_run = 0

        for fact in facts:
            fact_id = fact["id"]
            try:
                payload = fact.get("payload", {})
                vector = fact.get("vector")

                if not vector:
                    continue

                queries_run += 1

                try:
                    hits = search_similar_facts(
                        query_vector=vector,
                        qdrant_url=self._qdrant_url,
                        collection=self._collection,
                        top_k=self._top_k + 1,  # +1 because self-match is #1
                    )
                except RuntimeError as e:
                    # Review #46: record the outage, keep going with other facts.
                    _logger.warning("Discovery search failed for %s: %s", fact_id, e)
                    errors.append(str(e))
                    continue

                for hit in hits:
                    hit_id = hit["id"]
                    hit_payload = hit.get("payload", {})
                    score = hit["score"]

                    # Skip self-match
                    if hit_id == fact_id:
                        continue

                    # H195: the category filter was applied to the scanned SOURCE
                    # facts only — hits from other categories still flowed in and
                    # produced candidates whose target lives in a filtered-out
                    # category. Apply the same filter to the hit side.
                    if categories and _extract_category(hit_payload) not in categories:
                        continue

                    # Directional dedup: only process A↔B once
                    pair_key = tuple(sorted([fact_id, hit_id]))
                    if pair_key in seen_pairs:
                        continue
                    seen_pairs.add(pair_key)

                    if score < MIN_DISCOVERY_THRESHOLD:
                        continue

                    # 3. Stable direction: source < target alphabetically.
                    #    Must be decided BEFORE classification — otherwise the
                    #    relation is classified fact→hit but stored target→source
                    #    whenever hit_id < fact_id, inverting the direction.
                    source_id, target_id = sorted([fact_id, hit_id])
                    if source_id == fact_id:
                        source_payload, target_payload = payload, hit_payload
                    else:
                        source_payload, target_payload = hit_payload, payload

                    classification = classify_relation(
                        source_content=_extract_content(source_payload),
                        target_content=_extract_content(target_payload),
                        source_category=_extract_category(source_payload),
                        target_category=_extract_category(target_payload),
                        source_id=source_id,
                        target_id=target_id,
                        similarity_score=score,
                    )

                    if classification is None:
                        continue

                    all_candidates.append({
                        "source": source_id,
                        "target": target_id,
                        "relation": classification["relation"],
                        "confidence": classification.get("confidence", 0.0),
                        "reason": classification.get("reason", ""),
                        "similarity_score": score,
                    })
            except Exception as e:
                # H194: one malformed fact/hit/classification must not abort the
                # whole discovery run — record it and continue with the next fact.
                _logger.warning("Discovery classify failed for %s: %s", fact_id, e)
                errors.append(f"{fact_id}: {e}")
                continue

        # 4. Dedup against existing edges in the EdgeStore
        #    (Qdrant-payload backed since v2.2.0)
        unique_candidates = filter_new_edges(all_candidates, self._store)

        # 5. Insert into store
        inserted_active = 0
        inserted_proposed = 0

        for candidate in unique_candidates:
            confidence = candidate.get("confidence", 0.0)
            decision = classify_confidence(confidence)

            if not decision["should_insert"]:
                continue

            metadata = {
                "confidence": confidence,
                "similarity_score": candidate.get("similarity_score", 0.0),
                "reason": candidate.get("reason", ""),
                "discovered_by": "v2.1.0-auto-discovery",
            }

            try:
                if decision["as_proposed"]:
                    self._store.add_proposed_edge(
                        source_fact_id=candidate["source"],
                        target_fact_id=candidate["target"],
                        relation=candidate["relation"],
                        reason=candidate.get("reason"),
                        confidence=confidence,
                        metadata=metadata,
                    )
                    inserted_proposed += 1
                else:
                    self._store.add_edge(
                        source_fact_id=candidate["source"],
                        target_fact_id=candidate["target"],
                        relation=candidate["relation"],
                        reason=candidate.get("reason"),
                        metadata=metadata,
                    )
                    inserted_active += 1
            except Exception as e:
                err_msg = (
                    f"Failed to insert edge "
                    f"({candidate['source']} --[{candidate['relation']}]--> "
                    f"{candidate['target']}): {e}"
                )
                _logger.warning(err_msg)
                errors.append(err_msg)

        summary = {
            "total_facts_scanned": len(facts),
            "similarity_queries_run": queries_run,
            "candidates_found": len(all_candidates),
            "after_dedup": len(unique_candidates),
            "inserted_active": inserted_active,
            "inserted_proposed": inserted_proposed,
            "errors": errors,
            "status": "ok",
        }

        _logger.info(
            "Discovery: %d facts → %d candidates → "
            "%d active + %d proposed (%d errors)",
            len(facts), len(all_candidates),
            inserted_active, inserted_proposed, len(errors),
        )
        return summary

    # ── Single fact discovery (for testing / manual use) ──────────────────

    def discover_for_fact(
        self,
        fact_id: str,
        content: str,
        category: str,
        vector: list[float],
    ) -> list[dict]:
        """Discover relations for a single fact (no store insertion).

        Args:
            fact_id: The fact ID to discover from.
            content: The fact content text.
            category: The fact category.
            vector: The embedding vector.

        Returns:
            List of candidate dicts.
        """
        if not vector:
            return []

        hits = search_similar_facts(
            query_vector=vector,
            qdrant_url=self._qdrant_url,
            collection=self._collection,
            top_k=self._top_k + 1,
        )

        candidates = []
        for hit in hits:
            hit_id = hit["id"]
            hit_payload = hit.get("payload", {})
            if hit_id == fact_id:
                continue

            # Stable direction first — see discover_all() for why the
            # sort must happen before classification, not after.
            source, target = sorted([fact_id, hit_id])
            hit_content = _extract_content(hit_payload)
            hit_category = _extract_category(hit_payload)
            if source == fact_id:
                src_content, src_category = content, category
                tgt_content, tgt_category = hit_content, hit_category
            else:
                src_content, src_category = hit_content, hit_category
                tgt_content, tgt_category = content, category

            classification = classify_relation(
                source_content=src_content,
                target_content=tgt_content,
                source_category=src_category,
                target_category=tgt_category,
                source_id=source,
                target_id=target,
                similarity_score=hit["score"],
            )

            if classification is None:
                continue

            candidates.append({
                "source": source,
                "target": target,
                "relation": classification["relation"],
                "confidence": classification.get("confidence", 0.0),
                "similarity_score": hit["score"],
                "reason": classification.get("reason", ""),
                "target_content": tgt_content[:200],
            })

        return candidates
