"""Matcher — Qdrant-native similarity matching for auto-discovery.

Strategy: For each canonical fact, use its stored embedding to query Qdrant
for the ``k`` most similar facts. This is O(n·k) instead of O(n²).

The AutoDiscovery class combines matcher + classifier + dedup into a pipeline.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from nexus.config import get_collection

_logger = logging.getLogger(__name__)

try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

# ── Default embed provider settings ─────────────────────────────────────────

EMBEDDING_FIELD = "embedding"  # Default Qdrant vector field name
DEFAULT_LIMIT = 5              # How many candidates per fact


def scroll_facts(
    qdrant_url: str = "http://localhost:6333",
    collection: Optional[str] = None,
    with_vectors: bool = True,
    limit_per_scroll: int = 100,
) -> list[dict]:
    """Scroll all canonical facts from Qdrant with their vectors.

    Only returns entries where ``type`` == ``"memory"`` (skip conversation turns).

    Returns:
        List of point dicts with ``id``, ``payload``, and optionally ``vector``.
    """
    collection = get_collection(collection)
    if not HAS_REQUESTS:
        raise ImportError("requests is required: pip install requests")

    points: list[dict] = []
    offset: Any = None

    while True:
        body: dict[str, Any] = {
            "limit": limit_per_scroll,
            "with_payload": True,
            "with_vector": with_vectors,
            "filter": {
                "must": [{"key": "type", "match": {"value": "memory"}}]
            },
        }
        if offset is not None:
            body["offset"] = offset

        try:
            r = requests.post(
                f"{qdrant_url}/collections/{collection}/points/scroll",
                json=body,
                timeout=30,
            )
            r.raise_for_status()
        except requests.RequestException as e:
            _logger.error("Qdrant scroll failed: %s", e)
            break

        data = r.json().get("result", {})
        batch = data.get("points", [])
        if not batch:
            break

        points.extend(batch)
        offset = data.get("next_page_offset")
        if not offset:
            break

    _logger.debug("Scrolled %d facts from Qdrant collection '%s'", len(points), collection)
    return points


def search_similar_facts(
    query_vector: list[float],
    qdrant_url: str = "http://localhost:6333",
    collection: Optional[str] = None,
    top_k: int = 5,
) -> list[dict]:
    """Search Qdrant for facts similar to a given query vector.

    Returns:
        List of hit dicts with ``id``, ``score``, ``payload``.
    """
    collection = get_collection(collection)
    if not HAS_REQUESTS:
        raise ImportError("requests is required: pip install requests")

    try:
        r = requests.post(
            f"{qdrant_url}/collections/{collection}/points/search",
            json={
                "vector": query_vector,
                "limit": top_k,
                "with_payload": True,
                "filter": {
                    "must": [{"key": "type", "match": {"value": "memory"}}]
                },
            },
            timeout=10,
        )
        r.raise_for_status()
    except requests.RequestException as e:
        _logger.error("Qdrant search failed: %s", e)
        return []

    results = r.json().get("result", [])
    return [
        {
            "id": str(point.get("id", "")),
            "score": point.get("score", 0.0),
            "payload": point.get("payload", {}),
        }
        for point in results
    ]


def match_facts_against_each_other(
    facts: list[dict],
    qdrant_url: str = "http://localhost:6333",
    collection: Optional[str] = None,
    top_k: int = 5,
    threshold: float = 0.85,
) -> list[dict]:
    """For each fact, find similar facts from Qdrant using its own vector.

    Args:
        facts: List of fact dicts (must have ``id`` and ``vector``).
        qdrant_url: Qdrant HTTP API URL.
        collection: Qdrant collection name.
        top_k: How many similar facts to retrieve per query.
        threshold: Minimum similarity score to include a candidate.

    Returns:
        List of candidate dicts::
            {
                "source": str,
                "target": str,
                "similarity": float,
                "source_payload": dict,
                "target_payload": dict,
            }
    """
    collection = get_collection(collection)
    candidates: list[dict] = []
    fact_ids = {f.get("id", "") for f in facts}

    for fact in facts:
        fact_id = fact.get("id", "")
        vector = fact.get("vector")
        payload = fact.get("payload", {})

        if not vector or not fact_id:
            continue

        hits = search_similar_facts(
            query_vector=vector,
            qdrant_url=qdrant_url,
            collection=collection,
            top_k=top_k + 1,  # +1 because the fact itself will be #1
        )

        for hit in hits:
            hit_id = hit["id"]
            score = hit["score"]

            # Skip self-match and facts not in our set
            if hit_id == fact_id or hit_id not in fact_ids:
                continue

            if score < threshold:
                continue

            candidates.append({
                "source": fact_id,
                "target": hit_id,
                "similarity": score,
                "source_payload": payload,
                "target_payload": hit.get("payload", {}),
            })

    _logger.debug(
        "Matcher: %d facts queried, %d candidates found (threshold=%.2f, top_k=%d)",
        len(facts), len(candidates), threshold, top_k,
    )
    return candidates
