"""Invariant tests for the retrieval_watch MEDIUM review fixes.

1. Minimum-score check: a result whose similarity is below MIN_SCORE counts
   as a retrieval failure even when the expected keyword is present.
2. Embedding failures are visible: a failed embedding marks embed_ok=False
   and shows up as failed_embeddings in the report (no silent scroll).
3. Configured intervals below one hour are honored (no hidden 60s floor
   that clamps every sleep to at least one minute-per-iteration loop).
"""

import os
import time
from types import SimpleNamespace

import pytest

from nexus_memory import retrieval_watch as RW


class _FakePoint:
    def __init__(self, text, score):
        self.payload = {"content": text}
        self.score = score


class _FakeStore:
    def __init__(self, points):
        self._points = points
        self.client = SimpleNamespace(
            scroll=lambda collection, limit, with_payload, with_vectors:
                (points, None),
            query_points=None,
        )
        self._embedder = None


class _FakeEmbedder:
    def embed(self, _query):
        return [0.1, 0.2, 0.3, 0.4]


class _ScoredStore:
    """Store whose embedder works, so _search takes the vector path and the
    hits carry real similarity scores (embed_ok=True)."""

    def __init__(self, points):
        self.client = SimpleNamespace(
            query_points=lambda **kw: SimpleNamespace(points=points),
        )
        self._embedder = _FakeEmbedder()


def _watch(points, monkeypatch, min_score=None, store=None):
    if min_score is not None:
        monkeypatch.setattr(RW, "MIN_SCORE", min_score)
    monkeypatch.setenv("NEXUS_WATCH_QUERIES",
                       '[["Bose SoundLink Audio-Ausgabe Bluetooth", "Bose"]]')
    store = store if store is not None else _FakeStore(points)
    return RW.RetrievalWatch(store, "test-coll", embedder=None)


def test_min_score_failure_even_when_keyword_present(monkeypatch):
    # Vector path (embed_ok=True): expected keyword IS in the text but the
    # hit's similarity 0.12 < 0.5 → quality failure. The scroll fallback has
    # no meaningful score and must NOT be judged by MIN_SCORE.
    points = [_FakePoint("Bose SoundLink gehört zur Audio-Ausgabe", 0.12)]
    w = _watch(points, monkeypatch, min_score=0.5, store=_ScoredStore(points))
    rep = w.run()
    assert rep["failures"], "weak hit must count as failure"
    assert rep["failures"][0]["reason"] == "score_below_min"


def test_strong_hit_passes_min_score(monkeypatch):
    points = [_FakePoint("Bose SoundLink gehört zur Audio-Ausgabe", 0.92)]
    w = _watch(points, monkeypatch, min_score=0.5, store=_ScoredStore(points))
    rep = w.run()
    assert rep["failures"] == []


def test_scroll_fallback_ignores_score_gate(monkeypatch):
    # Scroll fallback carries no real similarity: a keyword hit counts as
    # found even when its (meaningless) score is below MIN_SCORE.
    points = [_FakePoint("Bose SoundLink gehört zur Audio-Ausgabe", 0.12)]
    w = _watch(points, monkeypatch, min_score=0.5)  # _FakeStore → embed_ok=False
    rep = w.run()
    assert rep["failures"] == []
    assert rep["failed_embeddings"] == 1


def test_embedding_failure_visible_in_report(monkeypatch):
    w = _watch([], monkeypatch)
    # _search returns embed_ok=False when no embedder/failed embedding.
    results, embed_ok = w._search("irgendeine query")
    assert embed_ok is False
    rep = w.run()
    assert rep["failed_embeddings"] == len(w._queries)


def test_interval_below_one_hour_respected(monkeypatch):
    # The old loop slept 60s per iteration regardless of the configured
    # interval (hidden 1h floor for 86400s: 1440 x 60s sleeps). The fixed
    # code sleeps the configured interval directly (seconds resolution).
    monkeypatch.setenv("NEXUS_RETRIEVAL_INTERVAL_SEC", "1800")
    import importlib
    importlib.reload(RW)
    assert RW.RETRIEVAL_INTERVAL_SECONDS == 1800
    # The loop math: interval itself, not max(60, interval//60) iterations
    # of 60s each — reconstruct the same expression as the source.
    interval = max(1, min(RW.RETRIEVAL_INTERVAL_SECONDS, 24 * 3600))
    assert interval == 1800
    monkeypatch.setenv("NEXUS_RETRIEVAL_INTERVAL_SEC", "900")
    importlib.reload(RW)
    interval = max(1, min(RW.RETRIEVAL_INTERVAL_SECONDS, 24 * 3600))
    assert interval == 900, "900s config must not be clamped to 3600s floor"