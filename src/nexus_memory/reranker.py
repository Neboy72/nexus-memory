"""Reranker for the Nexus Memory Hermes plugin (roadmap 1.2).

Bridges ``nexus.retrieval.HybridRetriever``'s reranking (local CrossEncoder only)
into the Hermes plugin's ``_recall`` pipeline. Voyage Rerank was removed on
2026-10-17 (zero-Voyage policy); "auto" maps to the local cross-encoder.

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
DEFAULT_RERANKER = "cross-encoder"  # local cross-encoder only (zero-Voyage policy)
MAX_DOC_CHARS = 1000  # truncation cap for reranker documents


def _resolve_reranker(reranker: str, _voyage_api_key: Optional[str] = None) -> str:
    """Resolve reranker name; "auto" is kept for backwards compatibility.

    Voyage Rerank has been removed, so "auto" always maps to the local
    cross-encoder. Explicit "voyage" raises ValueError (fail-closed).
    """
    if reranker == "voyage":
        raise ValueError('Voyage reranker has been removed; use "cross-encoder" or "off"')
    if reranker and reranker != "auto":
        return reranker
    return "cross-encoder"


def load_rerank_config(config_path: Optional[str] = None) -> Dict[str, Any]:
    """Read rerank settings from ``~/.hermes/config.yaml`` (nexus-memory block).

    Returns a dict with keys: ``enabled`` (bool), ``reranker`` (str),
    ``pool_k`` (int). All values have safe defaults when the block is missing.

    Env overrides: ``NEXUS_RERANK=0/1`` and ``NEXUS_RERANKER``.
    """
    cfg: Dict[str, Any] = {
        "enabled": False,
        "reranker": DEFAULT_RERANKER,
        "pool_k": DEFAULT_POOL_K,
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

    except Exception as exc:  # fail-open: defaults stand
        logger.debug("rerank config read skipped: %s", exc)

    # Env overrides win over config file. Accepted falsy tokens match the
    # shared boolean-env vocabulary (case-insensitive): 0/false/no/off/n/"".
    env_enabled = os.environ.get("NEXUS_RERANK")
    if env_enabled is not None:
        cfg["enabled"] = env_enabled.strip().lower() not in (
            "0", "false", "no", "off", "n", "")
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
    _voyage_api_key: Optional[str] = None,
) -> List[Any]:
    """Re-rank Qdrant ScoredPoints by true query<->document relevance.

    Takes the top ``pool_k`` points by vector score, re-scores them with
    the chosen reranker and returns the re-ordered list (same point objects,
    re-ordered; only the re-ranked prefix is limited to pool_k — dropped pool
    candidates are re-appended and out-of-pool points are preserved, so
    ``len(result) == len(points)``).

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

    reranker = _resolve_reranker(reranker)
    try:
        if reranker == "cross-encoder":
            ranked = _rerank_local(query, results)
        else:
            return points
    except Exception as exc:
        logger.warning("rerank failed (fail-open): %s", exc)
        return points

    if not ranked:
        return points
    order = [r["_idx"] for r in ranked]
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


def _safe_score(v: Any) -> float:
    """Coerce a reranker score to float, defaulting to 0.0 on bad input.

    The API may return ``null`` (→ None) or a string; comparing/sorting the
    raw value raised TypeError. bool is rejected too (it is an int subclass
    and never a real relevance score).
    """
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0.0



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
    # A cross-encoder that returns fewer scores than pairs would raise
    # IndexError in the sort key. Fail open to the caller's original order.
    if len(scores) != len(pairs):
        logger.warning(
            "cross-encoder returned %d scores for %d pairs - "
            "keeping original order",
            len(scores), len(pairs),
        )
        return []

    def _safe(i: int) -> float:
        try:
            return float(scores[i])
        except (TypeError, ValueError, IndexError):
            return -1.0

    indexed = list(enumerate(results))
    indexed.sort(key=lambda x: _safe(x[0]), reverse=True)
    return [r for _, r in indexed]