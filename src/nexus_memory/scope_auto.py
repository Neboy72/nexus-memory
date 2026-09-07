#!/usr/bin/env python3
"""Nexus Memory — Scope Auto-Inference (self-organizing memory areas).

Das Gedächtnis ordnet sich selbst (Nebo-Gesetz 07.09.: volle Automatik oder
useless). Kein User tippt je NEXUS_SCOPE — dieses Modul ist der Produkt-Pfad.

Zentren-Prinzip: Jeder existierende Scope bekommt ein Schwerpunkt-Profil
(Zentroid = gemittelte Embeddings seiner Memories). Beim Speichern erbt ein
neuer Text das klar passende Regal; beim automatischen Vorhalten wird nur das
passende Regal + 'default' ausgespielt. Zweideutig/kein Match = fail-open
(ungefiltert, exakt altes Verhalten).

Invarianten:
1. Ohne Scopes im System: identisches Verhalten wie heute (0 Regale = 0 Filter).
2. Falsches Etikett verbirgt NIEMALS etwas — nur das AUTOMATISCHE Vorhalten
   wird enger; explizite Fragen finden immer alles.
3. NEXUS_SCOPE env = manueller Override (interne Hausarbeit), Auto ist der
   Produkt-Pfad.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Optional

# Conservative thresholds (Spec /tmp/kiosha-think-gate.lock):
# - Under-tagging is harmless (everything visible, like today).
# - Over-tagging is dangerous (apparent forgetting) → require a clear margin.
SCOPE_MATCH_THRESHOLD = float(os.getenv("NEXUS_SCOPE_AUTO_THRESHOLD", "0.65"))
SCOPE_MARGIN = float(os.getenv("NEXUS_SCOPE_AUTO_MARGIN", "0.05"))
CENTROID_TTL_SECONDS = int(os.getenv("NEXUS_SCOPE_CACHE_TTL", "300"))
MAX_POINTS_PER_SCOPE = 200


def _normalize_scope(scope) -> str:
    """Mirror of the server contract (kept inline, no cross-import)."""
    import re
    if isinstance(scope, str):
        s = scope.strip().lower()
        if s and re.match(r"^[a-z0-9][a-z0-9-]{0,39}$", s):
            return s
    return "default"


class ScopeCentroids:
    """Cached scope centroids read from the live Qdrant collection.

    Centroid = mean of the (normalized) vectors of canonical points that carry
    the scope. Non-'default' scopes only. Pure functions, no LLM, no cost.
    """

    # Known scope names for the centroid scroll filter, discovered from live
    # data via Qdrant's match-except (scope != default is a POSITIVE match on
    # existing field values — points WITHOUT a scope field are excluded, which
    # must_not cannot do). Without this, the default-only universe (~8k
    # canonical points) crowds scoped points out of the 1000-point window.
    _SCOPE_PROBE_LIMIT = 500

    def __init__(self, client: Any, collection: str):
        self._client = client
        self._collection = collection
        self._cache: Optional[dict[str, list[float]]] = None
        self._cache_at: float = 0.0
        self._known_scopes: list[str] = self._probe_scopes()

    def _probe_scopes(self) -> list[str]:
        """Discover existing scope values via match-except. Fail-open to []."""
        try:
            points, _ = self._client.scroll(
                collection_name=self._collection,
                scroll_filter={
                    "must": [
                        {"key": "scope", "match": {"except": ["default"]}},
                    ],
                },
                with_payload=True,
                with_vectors=False,
                limit=self._SCOPE_PROBE_LIMIT,
            )
        except Exception as exc:
            logging.info("scope_auto: scope probe failed (%s) — fail-open", exc)
            return []
        found = set()
        for p in points:
            scope = ((p.payload or {}).get("scope") or "").strip().lower()
            if scope and scope != "default":
                found.add(scope)
        return sorted(found)

    def invalidate(self) -> None:
        self._cache = None
        self._cache_at = 0.0

    def _fetch(self) -> dict[str, list[float]]:
        """Read scoped canonical points and compute centroids. Fail-open to {}."""
        try:
            points, _ = self._client.scroll(
                collection_name=self._collection,
                scroll_filter={
                    "must": [
                        {"key": "lifecycle_status", "match": {"value": "canonical"}},
                        # Scroll ONLY points that carry a real scope — the
                        # default-only universe (~8k points) would otherwise
                        # crowd scoped points out of the 1000-point window.
                        {"key": "scope", "match": {"any": self._known_scopes}},
                    ]
                },
                with_payload=True,
                with_vectors=True,
                limit=1000,
            )
        except Exception as exc:  # Qdrant down etc. → no filtering at all
            logging.info("scope_auto: centroid fetch failed (%s) — fail-open", exc)
            return {}

        sums: dict[str, tuple[list[float], int]] = {}
        for p in points:
            pl = p.payload or {}
            scope = (pl.get("scope") or "default").strip().lower() or "default"
            if scope == "default":
                continue
            vec = p.vector
            if vec is None:
                continue
            if isinstance(vec, dict):  # named vectors — take the first entry
                vec = next(iter(vec.values())) if vec else None
            if not vec:
                continue
            s, n = sums.get(scope, ([0.0] * len(vec), 0))
            if len(vec) != len(s):  # mixed dims shouldn't happen; skip safely
                continue
            sums[scope] = ([a + b for a, b in zip(s, vec)], n + 1)

        centroids: dict[str, list[float]] = {}
        for scope, (s, n) in sums.items():
            if n <= 0:
                continue
            norm = sum(x * x for x in s) ** 0.5 or 1.0
            centroids[scope] = [x / norm for x in s]
        return centroids

    def get(self) -> dict[str, list[float]]:
        now = time.monotonic()
        if self._cache is None or now - self._cache_at > CENTROID_TTL_SECONDS:
            self._cache = self._fetch()
            self._cache_at = now
        return self._cache


def _cosine(a: list[float], b: list[float]) -> float:
    na = sum(x * x for x in a) ** 0.5 or 1.0
    nb = sum(x * x for x in b) ** 0.5 or 1.0
    return sum(x * y for x, y in zip(a, b)) / (na * nb)


def infer_scope(vector: list[float], centroids: dict[str, list[float]]) -> str:
    """Return the auto-inferred scope for a vector, or 'default'.

    Conservative by design: a match needs BOTH a clear similarity AND a clear
    margin over the runner-up. Anything ambiguous degrades to 'default' —
    under-tagging is harmless, over-tagging would cause apparent forgetting.
    """
    if not vector or not centroids:
        return "default"
    scored = sorted(
        ((scope, _cosine(vector, c)) for scope, c in centroids.items()),
        key=lambda t: t[1],
        reverse=True,
    )
    if not scored:
        return "default"
    best_scope, best_score = scored[0]
    if best_score < SCOPE_MATCH_THRESHOLD:
        return "default"
    if len(scored) > 1 and (best_score - scored[1][1]) < SCOPE_MARGIN:
        return "default"
    return best_scope


def prefetch_filter_scopes(
    query_vector: list[float],
    centroids: dict[str, list[float]],
    my_scope: str = "",
) -> Optional[set[str]]:
    """Which scopes should auto-prefetch surface for this query? Or None = no filter.

    Returns None (fail-open, show everything) unless the query clearly belongs
    to exactly one non-default scope. Returned set always includes 'default'
    plus the matched scope (plus the agent's own manual override if set).
    """
    inferred = infer_scope(query_vector, centroids)
    if inferred == "default":
        return None
    allowed = {"default", inferred}
    if my_scope:
        allowed.add(my_scope)
    return allowed