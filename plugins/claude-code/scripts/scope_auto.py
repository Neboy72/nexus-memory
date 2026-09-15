#!/usr/bin/env python3
"""Nexus Memory — Scope Auto-Inference (shared lib for Claude-Code hooks).

Self-organizing memory areas (Nebo law 07.09: full automation or useless).
No user ever types a scope: centroids are read from existing scoped canonical
points, and a memory/prompt is only tagged/filtered on a CLEAR match.

Conservative rule (identical thresholds to the MCP server's scope_auto.py):
  - cosine similarity must be >= SIM_THRESHOLD (default 0.65)
  - AND >= MARGIN (default 0.05) above the runner-up centroid
Otherwise: no area (default / no filtering). Fail-open everywhere.
Zero LLM cost: pure vector math.

Note: Claude-Code hooks are short-lived processes — centroids are fetched
per call (no cache). The fetch is one scroll request; fine at hook cadence.
"""

import logging
import os
import re
import time

QDRANT_URL = "http://localhost:6333"
COLLECTION = "nexus"

_SCOPE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")

# Inference thresholds — read from the SAME env vars with the SAME defaults as
# src/nexus_memory/scope_auto.py (SCOPE_MATCH_THRESHOLD / SCOPE_MARGIN). H255:
# the hook used to hardcode 0.72 while the MCP server defaulted to 0.65, so
# the same memory could be tagged on one path and filtered out on the other.
SIM_THRESHOLD = float(os.getenv("NEXUS_SCOPE_AUTO_THRESHOLD", "0.65"))
MARGIN = float(os.getenv("NEXUS_SCOPE_AUTO_MARGIN", "0.05"))


def _cosine(a, b):
    """Cosine similarity; 0.0 on any degenerate input (fail-open)."""
    try:
        if not a or not b or len(a) != len(b):
            return 0.0
        num = sum(x * y for x, y in zip(a, b))
        na = sum(x * x for x in a) ** 0.5
        nb = sum(x * x for x in b) ** 0.5
        if na == 0 or nb == 0:
            return 0.0
        return num / (na * nb)
    except Exception:
        return 0.0


# Scroll pagination guard: hooks are short-lived, so cap the work hard.
_PAGE_LIMIT = 1000
_MAX_PAGES = 3


def fetch_centroids(qdrant_url: str = QDRANT_URL, collection: str = COLLECTION,
                    timeout: float = 3.0) -> dict:
    """Read scope centroids from Qdrant. Returns {scope: centroid_vector}.

    Fail-open: any error → {} (no areas → no filtering anywhere).
    Reads canonical scoped points only.

    Scope clause: `match: {"except": ["default"]}` is a POSITIVE match on
    existing non-default scope values (mirrors the server's _probe_scopes
    predicate). Without it, the ~8k default-only canonical points crowd the
    scoped points out of the scroll window → empty centroids → dead scope
    inference. Pagination follows next_page_offset for up to _MAX_PAGES.
    """
    import json
    import urllib.request

    try:
        scroll_filter = {"must": [
            {"key": "lifecycle_status", "match": {"value": "canonical"}},
            {"key": "scope", "match": {"except": ["default"]}},
        ]}
        points = []
        offset = None
        for _ in range(_MAX_PAGES):
            body = {
                "limit": _PAGE_LIMIT,
                "with_payload": True,
                "with_vector": True,
                "filter": scroll_filter,
            }
            if offset is not None:
                body["offset"] = offset
            req = urllib.request.Request(
                f"{qdrant_url}/collections/{collection}/points/scroll",
                data=json.dumps(body).encode(),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read())
            result = data.get("result") or {}
            points.extend(result.get("points") or [])
            offset = result.get("next_page_offset")
            if not offset:
                break
    except Exception as exc:
        logging.info("scope_auto: centroid fetch failed (%s) — fail-open", exc)
        return {}

    # Parsing/aggregation stays INSIDE a guard: a malformed point (non-dict
    # payload, non-string scope, non-list/non-numeric vector) must degrade to
    # {} — never raise out of the short-lived hook.
    try:
        sums = {}
        counts = {}
        for p in points:
            if not isinstance(p, dict):
                continue
            payload = p.get("payload") or {}
            if not isinstance(payload, dict):
                continue
            raw_scope = payload.get("scope")
            scope = (raw_scope.strip().lower() if isinstance(raw_scope, str) else "") or "default"
            if scope == "default":
                continue
            vec = p.get("vector")
            if not isinstance(vec, list) or not vec:
                continue
            if not all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in vec):
                continue
            if scope in sums:
                if len(vec) != len(sums[scope]):
                    continue  # dimension mismatch — skip safely
                sums[scope] = [x + y for x, y in zip(sums[scope], vec)]
                counts[scope] += 1
            else:
                sums[scope] = list(vec)
                counts[scope] = 1

        cents = {}
        for scope, total in sums.items():
            n = counts[scope]
            norm = sum(x * x for x in total) ** 0.5
            if n > 0 and norm > 0:
                cents[scope] = [x / norm for x in total]  # normalized centroid
        return cents
    except Exception as exc:
        logging.info("scope_auto: centroid parse failed (%s) — fail-open", exc)
        return {}


def infer_scope(vector, centroids: dict) -> str:
    """Clear-closest area for a vector, else 'default'. Pure math, fail-open."""
    try:
        if not vector or not centroids:
            return "default"
        scored = sorted(
            ((scope, _cosine(vector, c)) for scope, c in centroids.items()),
            key=lambda kv: kv[1], reverse=True,
        )
        top_scope, top = scored[0]
        runner_up = scored[1][1] if len(scored) > 1 else 0.0
        if top >= SIM_THRESHOLD and top - runner_up >= MARGIN:
            return top_scope
    except Exception as exc:
        logging.info("scope_auto: inference failed (%s) — fail-open", exc)
    return "default"


def prefetch_allowed_scopes(vector, centroids: dict, manual_scope: str):
    """Allowed scope set for auto-prefetch filtering.

    Returns a set (e.g. {'default', 'voice'}) when the query clearly belongs
    to one area, or None when there is NO clear match (→ no filtering,
    old behavior). A manual scope override always wins.

    H256: delegates to ``infer_scope`` instead of duplicating the scoring +
    threshold block (drift risk). Equivalence: the old code returned None
    exactly when no scope cleared BOTH the absolute threshold and the margin —
    which is precisely the condition under which ``infer_scope`` returns
    "default"; that is mapped back to None here.
    """
    try:
        inferred = infer_scope(vector, centroids)
        if inferred == "default":
            return None
        allowed = {"default", inferred}
        if manual_scope:
            allowed.add(manual_scope)
        return allowed
    except Exception as exc:
        logging.info("scope_auto: prefetch inference failed (%s) — fail-open", exc)
        return None