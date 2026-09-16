"""Matcher — Qdrant-native similarity matching for auto-discovery.

Strategy: For each canonical fact, use its stored embedding to query Qdrant
for the ``k`` most similar facts. This is O(n·k) instead of O(n²).

The AutoDiscovery class combines matcher + classifier + dedup into a pipeline.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Optional

from nexus.config import get_collection

_logger = logging.getLogger(__name__)

try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

# ── Default embed provider settings ─────────────────────────────────────────

# W37: how many candidates per fact. Used as the ``top_k`` default below —
# before this the constant was dead and every function hardcoded its own 5.
DEFAULT_LIMIT = 5


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
            # Review #46: a failed scroll used to `break` and return a silently
            # truncated corpus. Raise so callers can surface the failure.
            _logger.error("Qdrant scroll failed: %s", e)
            raise RuntimeError(f"Qdrant scroll failed: {e}") from e

        try:
            data = r.json().get("result") or {}
        except ValueError as e:
            # W37: a 2xx response with a non-JSON body (e.g. a proxy/gateway
            # error page) raised a raw ValueError/JSONDecodeError — not the
            # RuntimeError this module promises callers.
            _logger.error("Qdrant scroll returned invalid JSON: %s", e)
            raise RuntimeError(f"Qdrant scroll returned invalid JSON: {e}") from e
        batch = data.get("points") or []
        # W37: read the continuation token BEFORE deciding on termination. The
        # previous `if not batch: break` ended pagination even when the server
        # still had pages left (empty-but-not-final page) — the same silent
        # truncation Review #46 removed for the error path.
        next_offset = data.get("next_page_offset")
        # W27: normalize point IDs to str — search_similar_facts() also
        # returns str ids; mixed int/str broke self-match guards and
        # crashed sorted() on integer-ID collections.
        for p in batch:
            p["id"] = str(p.get("id", ""))
        if batch:
            points.extend(batch)
        elif next_offset is None or next_offset == offset:
            # Only a final (None) — or non-advancing — offset ends the scan.
            break

        offset = next_offset
        # H218: Qdrant point IDs can be 0, so `if not offset` treated a valid
        # next_page_offset of 0 as the end of pagination. `is None` is correct.
        if offset is None:
            break

    _logger.debug("Scrolled %d facts from Qdrant collection '%s'", len(points), collection)
    return points


def search_similar_facts(
    query_vector: list[float],
    qdrant_url: str = "http://localhost:6333",
    collection: Optional[str] = None,
    top_k: int = DEFAULT_LIMIT,
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
        # Review #46: returning [] made an outage indistinguishable from
        # "no matches". Raise so callers can record the error.
        _logger.error("Qdrant search failed: %s", e)
        raise RuntimeError(f"Qdrant search failed: {e}") from e

    try:
        results = r.json().get("result") or []
    except ValueError as e:
        # W37: same gap as in scroll_facts — a non-JSON 2xx body must surface
        # as the RuntimeError callers catch, not a raw JSONDecodeError.
        _logger.error("Qdrant search returned invalid JSON: %s", e)
        raise RuntimeError(f"Qdrant search returned invalid JSON: {e}") from e
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
    top_k: int = DEFAULT_LIMIT,
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

    The per-fact searches run concurrently (thread pool, max 4 workers), but
    results are consumed in the original fact order (``executor.map``), so the
    output is deterministic and identical to a sequential run.

    The self-hit is dropped by id after over-fetching one extra result
    (``top_k + 1``). That assumes the fact's *stored* vector still ranks it
    first; when a caller supplies a vector that differs from the stored one,
    the self-hit can fall outside the window and a real candidate just inside
    it is lost. A failure of an individual search is logged and skipped (one
    fact cannot abort the batch).

    Unordered pair dedup: candidates are keyed by ``frozenset({source, target})``.
    If both A→B and B→A are discovered, only the FIRST one (in fact order)
    survives — the reverse direction is dropped as a duplicate pair.
    """
    collection = get_collection(collection)
    candidates: list[dict] = []
    # H219: normalise both sides to str. search_similar_facts() stringifies
    # hit ids, while scroll_facts() returns raw JSON ids (int for integer
    # point ids). Without this, `hit_id not in fact_ids` was always True and
    # every candidate was silently discarded.
    # W37: `f.get("id", "")` only substitutes the default when the key is
    # absent — an id that is present but None became the truthy literal "None"
    # and was admitted to fact_ids / the queryable filter. `or ""` normalizes
    # both cases.
    fact_ids = {str(f.get("id") or "") for f in facts}

    # Only facts carrying both an id and a vector can be queried at all.
    queryable = [f for f in facts if f.get("vector") and str(f.get("id") or "")]
    if not queryable:
        # W37: an empty result here is otherwise indistinguishable from "no
        # similar facts found" for the caller.
        _logger.warning(
            "Matcher: none of the %d facts carry an id and a vector — "
            "similarity matching skipped", len(facts),
        )
        return candidates

    def _search(fact: dict) -> list[dict]:
        # W37: one failing per-fact search must not abort the whole batch —
        # executor.map re-raises the first RuntimeError and discards every hit
        # list already produced. Mirrors the per-fact error isolation in
        # AutoDiscovery.discover_all.
        try:
            return search_similar_facts(
                query_vector=fact["vector"],
                qdrant_url=qdrant_url,
                collection=collection,
                top_k=top_k + 1,  # +1 because the fact itself will be #1
            )
        except RuntimeError as e:
            _logger.warning("Matcher: search failed for fact %s: %s", fact.get("id"), e)
            return []

    # Parallelise the per-fact Qdrant round trips (was one blocking request
    # per fact). executor.map preserves input order, so the hit lists line up
    # with `queryable` and the result stays deterministic.
    with ThreadPoolExecutor(max_workers=4) as executor:
        hit_lists = list(executor.map(_search, queryable))

    seen_pairs: set[frozenset] = set()
    for fact, hits in zip(queryable, hit_lists):
        fact_id = str(fact.get("id") or "")
        payload = fact.get("payload", {})

        for hit in hits:
            hit_id = hit["id"]
            score = hit["score"]

            # Skip self-match and facts not in our set
            if hit_id == fact_id or hit_id not in fact_ids:
                continue

            if score < threshold:
                continue

            # Unordered pair dedup — the first direction wins.
            pair_key = frozenset({fact_id, hit_id})
            if pair_key in seen_pairs:
                continue
            seen_pairs.add(pair_key)

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
