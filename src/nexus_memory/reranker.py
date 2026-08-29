"""Reranker for the Nexus Memory Hermes plugin (roadmap 1.2).

Bridges ``nexus.retrieval.HybridRetriever``'s reranking (Voyage Rerank API
or local CrossEncoder) into the Hermes plugin's ``_recall`` pipeline.

The plugin's recall path is: embed query -> Qdrant vector search ->
graph-boost -> return. This module inserts an optional rerank step between
vector search and graph-boost, re-ranking the top-N pool of candidates.

Design constraints:
- Fail-open: any error returns the original (vector-ranked) results.
- Lazy model/API init: nothing is loaded or called until rerank is enabled.
- Config-driven: ``~/.hermes/config.yaml`` -> ``nexus-memory.rerank``.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger("nexus.hermes_plugin.rerank")

# Defaults (conservative): rerank only the top pool, return at most this many.
DEFAULT_POOL_K = 20
DEFAULT_RERANKER = "auto"  # "auto" = voyage if key present, else local cross-encoder
MAX_DOC_CHARS = 1000  # truncation cap for reranker documents


def _resolve_reranker(reranker: str, voyage_api_key: Optional[str]) -> str:
    """Resolve "auto" to a concrete reranker based on what the user HAS.

    User with VOYAGE_API_KEY -> Voyage Rerank (best quality, API-cost).
    Everyone else -> local CrossEncoder (free, CPU, ~50ms per 50 docs).
    This is how the same code adapts from user to user without config.
    """
    if reranker != "auto":
        return reranker
    return "voyage" if voyage_api_key else "cross-encoder"


def load_rerank_config(config_path: Optional[str] = None) -> Dict[str, Any]:
    """Read rerank settings from ``~/.hermes/config.yaml`` (nexus-memory block).

    Returns a dict with keys: ``enabled`` (bool), ``reranker`` (str),
    ``pool_k`` (int), ``voyage_api_key`` (str). All values have safe
    defaults when the block is missing.

    Env overrides: ``NEXUS_RERANK=0/1`` and ``NEXUS_RERANKER``.
    """
    cfg: Dict[str, Any] = {
        "enabled": False,
        "reranker": DEFAULT_RERANKER,
        "pool_k": DEFAULT_POOL_K,
        "voyage_api_key": os.environ.get("VOYAGE_API_KEY", ""),
    }
    try:
        import os as _os

        path = config_path or _os.path.expanduser(
            os.path.join("~", ".hermes", "config.yaml")
        )
        if os.path.exists(path):
            import yaml

            with open(path) as f:
                full = yaml.safe_load(f) or {}
            block = full.get("nexus-memory", {}) or {}
            cfg["enabled"] = bool(block.get("rerank", False))
            cfg["reranker"] = str(block.get("reranker", DEFAULT_RERANKER))
            cfg["pool_k"] = int(block.get("rerank_pool", DEFAULT_POOL_K))
            key = block.get("voyage_api_key", "")
            if key:
                cfg["voyage_api_key"] = key
    except Exception as exc:  # fail-open: defaults stand
        logger.debug("rerank config read skipped: %s", exc)

    # Env overrides win over config file.
    env_enabled = os.environ.get("NEXUS_RERANK")
    if env_enabled is not None:
        cfg["enabled"] = env_enabled not in ("0", "false", "False", "")
    env_reranker = os.environ.get("NEXUS_RERANKER")
    if env_reranker:
        cfg["reranker"] = env_reranker
    return cfg


def rerank_points(
    query: str,
    points: List[Any],
    *,
    reranker: str,
    pool_k: int = DEFAULT_POOL_K,
    voyage_api_key: Optional[str] = None,
) -> List[Any]:
    """Re-rank Qdrant ScoredPoints by true query<->document relevance.

    Takes the top ``pool_k`` points by vector score, re-scores them with
    the chosen reranker and returns the re-ordered list (same point
    objects, possibly re-ordered and capped to ``pool_k``).

    The pool comes pre-sorted by vector score (Qdrant guarantees this),
    so "top pool_k" is just ``points[:pool_k]``.
    """
    if not points:
        return points
    pool = list(points)[: max(pool_k, 1)]
    texts = [
        ((p.payload or {}).get("content") or "")[:MAX_DOC_CHARS] for p in pool
    ]
    # Points without text cannot be cross-encoded - keep their relative order.
    results = [
        {"_idx": i, "text": text} for i, text in enumerate(texts) if text
    ]
    if not results:
        return points

    reranker = _resolve_reranker(reranker, voyage_api_key)
    try:
        if reranker == "voyage" and voyage_api_key:
            ranked = _rerank_voyage(query, results, voyage_api_key)
        elif reranker == "cross-encoder":
            ranked = _rerank_local(query, results)
        else:
            return points
    except Exception as exc:
        logger.warning("rerank failed (fail-open): %s", exc)
        return points

    if not ranked:
        return points
    order = [r["_idx"] for i, r in enumerate(ranked)]
    ordered = [pool[i] for i in order]
    # Pool candidates that the reranker dropped (empty content etc.) are
    # re-appended at the end in their original order - a rerank failure
    # must never silently lose results.
    seen = set(order)
    missing = [pool[i] for i in range(len(pool)) if i not in seen]
    ordered.extend(missing)
    # Append points that were not part of the rerank pool (beyond pool_k).
    ordered.extend(points[len(pool):])
    return ordered


# requests is optional at import time (reranker="voyage" only needs it).
try:
    import requests as _requests
except ImportError:
    _requests = None


def _rerank_voyage(
    query: str, results: List[Dict[str, Any]], voyage_api_key: str
) -> List[Dict[str, Any]]:
    """Voyage Rerank API (rerank-2). Returns results sorted by relevance."""
    if _requests is None:
        raise RuntimeError("requests not installed")

    docs = [r["text"][:MAX_DOC_CHARS] for r in results]
    resp = _requests.post(
        "https://api.voyageai.com/v1/rerank",
        headers={"Authorization": f"Bearer {voyage_api_key}"},
        json={
            "query": query,
            "documents": docs,
            "model": "rerank-2",
            "top_k": len(docs),
        },
        timeout=15,
    )
    if resp.status_code >= 400:
        logger.warning(
            "Voyage rerank HTTP %s - falling back to original order",
            resp.status_code,
        )
        raise RuntimeError(f"voyage rerank HTTP {resp.status_code}")
    ranking = resp.json().get("data", [])
    ranked: List[Dict[str, Any]] = []
    seen_idx = set()
    for item in sorted(
        ranking, key=lambda x: x.get("relevance_score", 0), reverse=True
    ):
        idx = item.get("index", 0)
        if 0 <= idx < len(results) and idx not in seen_idx:
            seen_idx.add(idx)
            r = dict(results[idx])
            r["_rerank_score"] = item.get("relevance_score", 0.0)
            ranked.append(r)
    return ranked


def _rerank_local(
    query: str, results: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Local Cross-Encoder via sentence-transformers (CPU, ~50ms/50 docs)."""
    from nexus.retrieval import _get_cross_encoder

    ce = _get_cross_encoder()
    if ce is None:
        return []
    pairs = [(query, r["text"][:MAX_DOC_CHARS]) for r in results]
    scores = ce.predict(pairs)
    indexed = list(enumerate(results))
    indexed.sort(key=lambda x: float(scores[x[0]]), reverse=True)
    return [r for _, r in indexed]