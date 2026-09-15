"""Deduplication — check edges to avoid re-discovering known relations.

v2.1.0: Checks against EdgeStore. Supports both active and proposed edges.
v2.2.0: EdgeStore backed by Qdrant-Payloads (was SQLite). API unchanged.
"""

from __future__ import annotations

import logging

from nexus.graph.store import EdgeStore

_logger = logging.getLogger(__name__)


def filter_new_edges(
    candidates: list[dict],
    store: EdgeStore,
) -> list[dict]:
    """Filter out candidates that already exist in the edge store.

    Args:
        candidates: List of ``{"source", "target", "relation", ...}`` dicts.
        store: An initialised ``EdgeStore`` instance.

    Returns:
        Only the candidates that do NOT already have an edge (any status)
        between the same source-target-relation triple.
    """
    # H196: batch-preload existing edges per UNIQUE source instead of one
    # scroll roundtrip per candidate (store.has_any_edge was N requests).
    # list_edges is used read-only; the has_any_edge semantics (any status,
    # directed source→target, same relation) are reproduced locally from the
    # cached edges. M unique sources ≤ N candidates → M requests.
    edges_by_source: dict[str, list] = {}

    def _edges_for(source: str) -> list:
        cached = edges_by_source.get(source)
        if cached is None:
            cached = store.list_edges(fact_id=source, status=None)
            edges_by_source[source] = cached
        return cached

    new = []
    skipped = 0
    skipped_malformed = 0
    for c in candidates:
        source = c.get("source", "")
        target = c.get("target", "")
        relation = c.get("relation", "")

        if not source or not target or not relation:
            # H198: a candidate missing source/target/relation can never be
            # deduped or stored — count it so the summary line reflects it
            # instead of silently shrinking `new`.
            skipped_malformed += 1
            _logger.warning(
                "Dedup skipped malformed candidate (missing source/target/relation): %r",
                c,
            )
            continue

        # list_edges is bidirectional → also require source_fact_id so only
        # edges that really originate at `source` count (matches has_any_edge).
        exists = any(
            e.source_fact_id == source
            and e.target_fact_id == target
            and e.relation == relation
            for e in _edges_for(source)
        )
        if exists:
            skipped += 1
            _logger.debug(
                "Dedup skipped: %s --[%s]--> %s (already exists)",
                source, relation, target,
            )
            continue

        new.append(c)

    if skipped or skipped_malformed:
        _logger.info(
            "Dedup: %d candidates skipped, %d malformed, %d new",
            skipped, skipped_malformed, len(new),
        )
    return new


def count_existing(store: EdgeStore, source: str, target: str) -> int:
    """Count how many edges (any status) go FROM ``source`` TO ``target``.

    v2.2.0: Uses EdgeStore.list_edges() instead of raw SQL.
    H197: ``list_edges(fact_id=...)`` is BIDIRECTIONAL (it also returns edges
    where ``source`` is the target), so we additionally filter on
    ``source_fact_id`` — only edges that really originate at ``source`` are
    counted, never mirrored/incoming ones.
    """
    edges = store.list_edges(fact_id=source, status=None)
    return sum(
        1 for e in edges
        if e.source_fact_id == source and e.target_fact_id == target
    )
