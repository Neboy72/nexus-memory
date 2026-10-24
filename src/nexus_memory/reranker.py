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


#: Regex-free marker for ``api_key: env:NAME`` indirection in config.yaml.
_ENV_REF_PREFIX = "env:"


def _resolve_api_key(value: Optional[str]) -> Optional[str]:
    """Expand ``env:NAME`` to ``os.environ["NAME"]``.

    Returns the literal string for any other value.  A missing referenced
    variable becomes ``None`` so the caller can treat the endpoint as
    unconfigured.
    """
    if not value or not value.startswith(_ENV_REF_PREFIX):
        return value
    name = value[len(_ENV_REF_PREFIX):]
    return os.environ.get(name)


def resolve_llm_endpoint(
    config_path: Optional[str] = None,
) -> Tuple[Optional[str], Optional[str], Optional[str], bool]:
    """Resolve LLM base URL, API key, and model for the reranker.

    Priority:
    1. Environment variables ``NEXUS_RERANK_LLM_BASE_URL``,
       ``NEXUS_RERANK_LLM_API_KEY``, ``NEXUS_RERANK_LLM_MODEL``.
    2. ``~/.hermes/config.yaml`` (or *config_path*): ``model.default``,
       ``model.provider`` -> ``providers.<provider>.base_url`` and
       ``providers.<provider>.api_key``.  ``api_key`` may be ``env:NAME``,
       which is resolved via ``os.environ["NAME"]``.

    There is no built-in fallback model. The endpoint is only considered
    configured when ``base_url``, ``api_key`` and ``model`` are all present.
    Otherwise ``configured`` is False, the rerank step is skipped, and the
    original vector order is preserved.

    Returns ``(base_url, api_key, model, configured)``.  The API key is never
    logged; only the model name and the host of ``base_url`` appear in logs.
    """
    # --- Source 1: dedicated environment variables ---------------------------------
    base_url = os.environ.get("NEXUS_RERANK_LLM_BASE_URL")
    api_key = _resolve_api_key(os.environ.get("NEXUS_RERANK_LLM_API_KEY"))
    model = os.environ.get("NEXUS_RERANK_LLM_MODEL")
    if base_url and api_key and model:
        logger.debug("rerank llm endpoint from env: model=%s host=%s", model, _host_of(base_url))
        return base_url, api_key, model, True

    # --- Source 2: Hermes config.yaml ---------------------------------------------
    cfg_path = config_path or os.path.expanduser(os.path.join("~", ".hermes", "config.yaml"))
    try:
        import yaml
        with open(cfg_path) as f:
            cfg = yaml.safe_load(f) or {}

        model_cfg = cfg.get("model", {}) or {}
        config_model = (model_cfg.get("default", "") or "").strip()
        provider = model_cfg.get("provider", "") or ""
        providers = cfg.get("providers", {}) or {}
        provider_block: Dict[str, Any] = {}
        if provider and provider.startswith("custom:"):
            provider_block = providers.get(provider[7:], {}) or {}
        elif provider:
            provider_block = providers.get(provider, {}) or {}

        config_base_url = (provider_block.get("base_url", "") or "").strip()
        raw_api_key = provider_block.get("api_key", "")
        config_api_key = _resolve_api_key(raw_api_key)

        if not model:
            model = config_model or None
        if not base_url:
            base_url = config_base_url or None
        if not api_key:
            api_key = config_api_key

        if base_url and api_key and model:
            logger.debug(
                "rerank llm endpoint from config: model=%s host=%s",
                model, _host_of(base_url),
            )
            return base_url, api_key, model, True
    except Exception as exc:  # fail-open: fall through to defaults
        # Only the exception type: a YAML error message quotes the offending
        # line, which may be a credential line.
        logger.debug("rerank llm config read skipped: %s", type(exc).__name__)

    # --- Source 3: no built-in fallback model; endpoint is only configured if all
    # three pieces (base_url, api_key, model) are known. Otherwise the reranker is
    # deliberately skipped and the original vector order is preserved.
    configured = bool(base_url and api_key and model)
    if configured:
        logger.debug("rerank llm endpoint resolved: model=%s host=%s", model, _host_of(base_url or ""))
    return base_url, api_key, model, configured


def _host_of(base_url: Optional[str]) -> str:
    """Return hostname of *base_url* for logging; never raises."""
    if not base_url:
        return ""
    try:
        from urllib.parse import urlparse
        return (urlparse(base_url).hostname or "")[:120]
    except Exception:
        return ""


def load_rerank_config(config_path: Optional[str] = None) -> Dict[str, Any]:
    """Read rerank settings from ``~/.hermes/config.yaml`` (nexus-memory block).

    Returns a dict with keys: ``enabled`` (bool), ``reranker`` (str),
    ``pool_k`` (int), ``llm_base_url`` (Optional[str]), ``llm_model`` (Optional[str]),
    ``llm_configured`` (bool). All values have safe defaults when the block is
    missing.

    Env overrides: ``NEXUS_RERANK=0/1`` and ``NEXUS_RERANKER``.
    LLM endpoint resolution follows ``resolve_llm_endpoint()``.
    """
    cfg: Dict[str, Any] = {
        "enabled": False,
        "reranker": DEFAULT_RERANKER,
        "pool_k": DEFAULT_POOL_K,
        "llm_base_url": None,
        "llm_model": None,
        "llm_configured": False,
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

    # Resolve the LLM endpoint once, sharing the same config path.
    llm_base_url, llm_api_key, llm_model, llm_configured = resolve_llm_endpoint(
        config_path=config_path
    )
    cfg["llm_base_url"] = llm_base_url
    cfg["llm_model"] = llm_model
    cfg["llm_configured"] = llm_configured and bool(llm_api_key)
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


def _get_llm_credentials() -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Resolve LLM API credentials for reranking.

    Thin wrapper around ``resolve_llm_endpoint()`` that returns the credentials
    in the order expected by ``_rerank_llm``: ``(api_key, base_url, model)``.

    There is no built-in fallback. If any of the three values is missing, the
    caller receives ``None`` for all of them and must skip the LLM rerank step.
    """
    base_url, api_key, model, configured = resolve_llm_endpoint()
    if not configured:
        return None, None, None
    return api_key, base_url, model


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
    if not key or not base or not model:
        _llm_unavailable("no key/base/model configured")
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
