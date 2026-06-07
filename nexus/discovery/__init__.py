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
from typing import Any, Optional

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
    """Extract content text from a Qdrant point payload."""
    return payload.get("content", "") or payload.get("text", "")


def _extract_category(payload: dict) -> str:
    """Extract category from a Qdrant point payload."""
    return payload.get("category", "")


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
        facts = scroll_facts(
            qdrant_url=self._qdrant_url,
            collection=self._collection,
            with_vectors=True,
        )

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
            payload = fact.get("payload", {})
            vector = fact.get("vector")

            if not vector:
                continue

            queries_run += 1

            hits = search_similar_facts(
                query_vector=vector,
                qdrant_url=self._qdrant_url,
                collection=self._collection,
                top_k=self._top_k + 1,  # +1 because self-match is #1
            )

            for hit in hits:
                hit_id = hit["id"]
                hit_payload = hit.get("payload", {})
                score = hit["score"]

                # Skip self-match
                if hit_id == fact_id:
                    continue

                # Directional dedup: only process A↔B once
                pair_key = tuple(sorted([fact_id, hit_id]))
                if pair_key in seen_pairs:
                    continue
                seen_pairs.add(pair_key)

                if score < MIN_DISCOVERY_THRESHOLD:
                    continue

                # 3. Classify relation
                classification = classify_relation(
                    source_content=_extract_content(payload),
                    target_content=_extract_content(hit_payload),
                    source_category=_extract_category(payload),
                    target_category=_extract_category(hit_payload),
                    source_id=fact_id,
                    target_id=hit_id,
                    similarity_score=score,
                )

                if classification is None:
                    continue

                # Stable direction: source < target alphabetically
                source_id, target_id = sorted([fact_id, hit_id])

                all_candidates.append({
                    "source": source_id,
                    "target": target_id,
                    "relation": classification["relation"],
                    "confidence": classification.get("confidence", 0.0),
                    "reason": classification.get("reason", ""),
                    "similarity_score": score,
                })

        # 4. Dedup against existing SQLite edges
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

            classification = classify_relation(
                source_content=content,
                target_content=_extract_content(hit_payload),
                source_category=category,
                target_category=_extract_category(hit_payload),
                source_id=fact_id,
                target_id=hit_id,
                similarity_score=hit["score"],
            )

            if classification is None:
                continue

            source, target = sorted([fact_id, hit_id])
            candidates.append({
                "source": source,
                "target": target,
                "relation": classification["relation"],
                "confidence": classification.get("confidence", 0.0),
                "similarity_score": hit["score"],
                "reason": classification.get("reason", ""),
                "target_content": _extract_content(hit_payload)[:200],
            })

        return candidates
