"""Reranker for the Nexus Memory Hermes plugin (roadmap 1.2).

Bridges ``nexus.retrieval.HybridRetriever``'s reranking (local CrossEncoder only)
into the Hermes plugin's ``_recall`` pipeline. Voyage Rerank was removed on
2026-10-17 (zero-Voyage policy); "auto" and empty now map to the LLM reranker,
which is the new default.

The plugin's recall path is: embed query -> Qdrant vector search ->
graph-boost -> return. This module inserts an optional rerank step between
vector search and graph-boost, re-ranking the top-N pool of candidates.

Design constraints:
- Fail-open: any error returns the original (vector-ranked) results.
- Lazy model/API init: nothing is loaded or called until rerank is enabled.
- Config-driven: ``~/.hermes/config.yaml`` -> ``nexus-memory.rerank``.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("nexus.hermes_plugin.rerank")

# Defaults (conservative): rerank only the top pool, return at most this many.
DEFAULT_POOL_K = 20
DEFAULT_RERANKER = "llm"  # LLM rerank is the default (zero-Voyage policy)
MAX_DOC_CHARS = 1000  # truncation cap for cross-encoder documents
LLM_SHOW = 300  # characters per candidate shown to the LLM

# Module flag: log LLM rerank unavailability exactly once per process.
_LLM_UNAVAILABLE_LOGGED = False


def _resolve_reranker(reranker: str, _voyage_api_key: Optional[str] = None) -> str:
    """Resolve reranker name; "auto" and empty map to the LLM reranker.

    Voyage Rerank has been removed, so "voyage" raises ValueError (fail-closed).
    Explicit "cross-encoder" is still available as an opt-in fallback.
    """
    if reranker == "voyage":
        raise ValueError('Voyage reranker has been removed; use "llm" or "cross-encoder"')
    if reranker and reranker != "auto":
        return reranker
    return "llm"


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
    reranker = _resolve_reranker(reranker)

    if reranker == "llm":
        # LLM sees every pool candidate, truncated to LLM_SHOW chars, newlines flattened.
        results = [
            {
                "_idx": i,
                "text": (
                    ((p.payload or {}).get("content") or "").replace("\n", " ")
                )[:LLM_SHOW],
            }
            for i, p in enumerate(pool)
        ]
    elif reranker == "cross-encoder":
        texts = [
            ((p.payload or {}).get("content") or "")[:MAX_DOC_CHARS] for p in pool
        ]
        # Points without text cannot be cross-encoded - keep their relative order.
        results = [
            {"_idx": i, "text": text} for i, text in enumerate(texts) if text
        ]
        if not results:
            return points
    else:
        return points

    try:
        if reranker == "cross-encoder":
            ranked = _rerank_local(query, results)
        else:
            ranked = _rerank_llm(query, results, top_k=max(pool_k, 1))
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


def _llm_unavailable(reason: str) -> None:
    """Log LLM rerank unavailability exactly once per process."""
    global _LLM_UNAVAILABLE_LOGGED
    if not _LLM_UNAVAILABLE_LOGGED:
        logger.warning("llm rerank unavailable (...): %s", reason)
        _LLM_UNAVAILABLE_LOGGED = True


def _get_llm_credentials() -> Tuple[Optional[str], Optional[str], str]:
    """Resolve LLM API credentials for reranking.

    Priority:
    1. Environment variables ``NEXUS_RERANK_LLM_API_KEY`` / ``NEXUS_RERANK_LLM_BASE_URL``.
    2. Hermes credential pool entry ``ollama-cloud`` (``runtime_api_key`` or
       ``access_token`` plus ``runtime_base_url`` or ``base_url``).

    Returns ``(key, base_url, model)``. ``model`` defaults to
    ``deepseek-v4.1-flash:1017-cloud`` and can be overridden via
    ``NEXUS_RERANK_LLM_MODEL``.
    """
    key = os.environ.get("NEXUS_RERANK_LLM_API_KEY")
    base = os.environ.get("NEXUS_RERANK_LLM_BASE_URL")
    model = os.environ.get("NEXUS_RERANK_LLM_MODEL", "deepseek-v4.1-flash:1017-cloud")
    if key and base:
        return key, base, model

    agent_path = os.path.expanduser(os.path.join("~", ".hermes", "hermes-agent"))
    sys_path_inserted = False
    try:
        if agent_path not in sys.path and os.path.isdir(agent_path):
            sys.path.insert(0, agent_path)
            sys_path_inserted = True
        from agent.credential_pool import load_pool

        pool = load_pool("ollama-cloud")
        entries = pool.entries()
        if entries:
            e = entries[0]
            key = getattr(e, "runtime_api_key", None) or getattr(e, "access_token", None)
            base = getattr(e, "runtime_base_url", None) or getattr(e, "base_url", None)
            if key and base:
                return key, base, model
    except Exception:
        pass
    finally:
        if sys_path_inserted:
            sys.path.remove(agent_path)
    return None, None, model


def _rerank_llm(
    query: str, results: List[Dict[str, Any]], top_k: int = 5
) -> List[Dict[str, Any]]:
    """Rerank candidates by asking the runtime LLM to pick the best ones.

    Builds a numbered list of candidate snippets (180 chars, newlines flattened),
    sends a single chat-completion request, and parses the returned indices.

    Fail-open: any problem (missing credentials, HTTP/timeout/JSON error, or no
    usable numbers in the response) logs one warning per process and returns an
    empty list so ``rerank_points`` falls back to the original order.
    """
    key, base, model = _get_llm_credentials()
    if not key or not base:
        _llm_unavailable("no key/base configured")
        return []

    lines = [
        f"[{r['_idx']}] {r['text'].replace(chr(10), ' ')[:LLM_SHOW]}"
        for r in results
    ]
    liste = "\n".join(lines)
    prompt = (
        "Waehle aus den nummerierten Textausschnitten die 5 aus, die die Frage am besten "
        "beantworten. Antworte NUR mit den Nummern, kommagetrennt, keine Erklaerung.\n\n"
        f"Frage: {query}\n\n{liste}\n\nNummern:"
    )

    body = json.dumps(
        {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 6000,
            "temperature": 0,
            "reasoning_effort": "low",
        }
    ).encode("utf-8")
    def _try_rank() -> Optional[List[Dict[str, Any]]]:
        """One HTTP attempt. Returns the ranked list, or None if the response is
        empty/unusable and therefore retryable. Any HTTP/JSON error is raised so
        the outer caller can fail-open with a single warning.
        """
        req = urllib.request.Request(
            base.rstrip("/") + "/chat/completions",
            data=body,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=90) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        choices = data.get("choices") or [{}]
        txt = (choices[0].get("message") or {}).get("content") or ""
        if not txt:
            return None

        chosen: List[int] = []
        seen: set[int] = set()
        for part in txt.replace(";", ",").split(","):
            part = part.strip().strip("[]().")
            if part.isdigit():
                idx = int(part)
                if 0 <= idx < len(results) and idx not in seen:
                    chosen.append(idx)
                    seen.add(idx)

        if not chosen:
            return None

        by_idx = {r["_idx"]: r for r in results}
        ranked: List[Dict[str, Any]] = []
        for idx in chosen[:top_k]:
            if idx in by_idx:
                ranked.append(by_idx[idx])
        # Append any unselected candidates in their original order.
        for r in results:
            if r["_idx"] not in seen:
                ranked.append(r)
        return ranked

    try:
        ranked = _try_rank()
    except Exception as exc:
        _llm_unavailable(f"{exc}")
        return []

    if ranked is None:
        # Exactly one retry for empty or unusable model response.
        try:
            ranked = _try_rank()
        except Exception as exc:
            _llm_unavailable(f"{exc}")
            return []
        if ranked is None:
            _llm_unavailable("empty or unusable response after retry")
            return []

    return ranked
