#!/usr/bin/env python3
"""Nexus Memory — Scope Auto-Inference (shared lib for Claude-Code hooks).

Self-organizing memory areas (Nebo law 07.09: full automation or useless).
No user ever types a scope: centroids are read from existing scoped canonical
points, and a memory/prompt is only tagged/filtered on a CLEAR match.

Conservative rule (identical to the MCP server's scope_auto.py):
  - cosine similarity must be >= 0.72 (absolute)
  - AND >= 0.05 above the runner-up centroid
Otherwise: no area (default / no filtering). Fail-open everywhere.
Zero LLM cost: pure vector math.

Note: Claude-Code hooks are short-lived processes — centroids are fetched
per call (no cache). The fetch is one scroll request; fine at hook cadence.
"""

import logging
import re
import time

QDRANT_URL = "http://localhost:6333"
COLLECTION = "nexus"

_SCOPE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")

# Inference thresholds — must stay in sync with src/nexus_memory/scope_auto.py
SIM_THRESHOLD = 0.72
MARGIN = 0.05


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


def fetch_centroids(qdrant_url: str = QDRANT_URL, collection: str = COLLECTION,
                    timeout: float = 3.0) -> dict:
    """Read scope centroids from Qdrant. Returns {scope: centroid_vector}.

    Fail-open: any error → {} (no areas → no filtering anywhere).
    Reads canonical scoped points only (client-side filter after scroll).
    """
    import json
    import urllib.request

    try:
        body = json.dumps({
            "limit": 1000,
            "with_payload": True,
            "with_vector": True,
            "filter": {"must": [
                {"key": "lifecycle_status", "match": {"value": "canonical"}},
            ]},
        }).encode()
        req = urllib.request.Request(
            f"{qdrant_url}/collections/{collection}/points/scroll",
            data=body, headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
        points = data.get("result", {}).get("points", [])
    except Exception as exc:
        logging.info("scope_auto: centroid fetch failed (%s) — fail-open", exc)
        return {}

    sums = {}
    counts = {}
    for p in points:
        payload = p.get("payload") or {}
        scope = (payload.get("scope") or "default").strip().lower() or "default"
        if scope == "default":
            continue
        vec = p.get("vector")
        if not vec:
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
    """
    try:
        if not vector or not centroids:
            return None
        scored = sorted(
            ((scope, _cosine(vector, c)) for scope, c in centroids.items()),
            key=lambda kv: kv[1], reverse=True,
        )
        if not scored:
            return None
        top_scope, top = scored[0]
        runner_up = scored[1][1] if len(scored) > 1 else 0.0
        if top >= SIM_THRESHOLD and top - runner_up >= MARGIN:
            allowed = {"default", top_scope}
            if manual_scope:
                allowed.add(manual_scope)
            return allowed
    except Exception as exc:
        logging.info("scope_auto: prefetch inference failed (%s) — fail-open", exc)
    return None