"""Deduplication — check edges to avoid re-discovering known relations.

v2.1.0: Checks against EdgeStore. Supports both active and proposed edges.
v2.2.0: EdgeStore backed by Qdrant-Payloads (was SQLite). API unchanged.
"""

from __future__ import annotations

import logging
from typing import Optional

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
    new = []
    skipped = 0
    for c in candidates:
        source = c.get("source", "")
        target = c.get("target", "")
        relation = c.get("relation", "")

        if not source or not target or not relation:
            continue

        if store.has_any_edge(source, target, relation):
            skipped += 1
            _logger.debug(
                "Dedup skipped: %s --[%s]--> %s (already exists)",
                source, relation, target,
            )
            continue

        new.append(c)

    if skipped:
        _logger.info("Dedup: %d candidates skipped, %d new", skipped, len(new))
    return new


def count_existing(store: EdgeStore, source: str, target: str) -> int:
    """Count how many edges (any status) exist between two facts.

    v2.2.0: Uses EdgeStore.list_edges() instead of raw SQL.
    """
    edges = store.list_edges(fact_id=source, status=None)
    return sum(
        1 for e in edges if e.target_fact_id == target
    )
