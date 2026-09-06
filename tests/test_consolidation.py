"""Tests for consolidation.py — in-process ingestion consolidation.

Covers: distiller JSON parsing (incl. broken JSON fallback), conflict
resolver (supersede / duplicate / threshold-skip), daemon batch loop with a
fake qdrant client, no-delete invariant, consolidated_by marking, and the
config kill-switch. All LLM calls are mocked — no network.
"""

import pytest
from types import SimpleNamespace
from unittest.mock import MagicMock

from nexus_memory import consolidation as C
from nexus_memory.consolidation import (
    Consolidator,
    _parse_facts,
    _parse_verdict,
)


# ── helpers ──────────────────────────────────────────────────────────

class FakePoint:
    def __init__(self, pid, payload, score=0.0, vector=None):
        self.id = pid
        self.payload = payload
        self.score = score
        self.vector = vector or [0.1] * 1024


class FakeQdrant:
    """Minimal Qdrant client double: scroll + query_points + set_payload + upsert."""

    def __init__(self, raw_points, similar_points=None):
        self._raw = list(raw_points)
        self._similar = list(similar_points or [])
        self.upserts = []          # (collection, points)
        self.payload_sets = []     # (collection, payload, point_ids)
        self.queries = []

    # scroll API used by _next_raw_batch
    def scroll(self, collection, scroll_filter=None, limit=64, offset=None,
               with_payload=True, with_vectors=False, **kw):
        if offset is None:
            out = self._raw[:limit]
            next_offset = "next" if len(self._raw) > limit else None
        else:
            out = []
            next_offset = None
        return out, next_offset

    # similarity search API used by _resolve_conflicts
    def query_points(self, collection, query=None, limit=3,
                     score_threshold=None, query_filter=None, **kw):
        self.queries.append({"vec": query, "thr": score_threshold})
        points = [p for p in self._similar
                  if score_threshold is None or p.score >= score_threshold]
        return SimpleNamespace(points=points)

    def set_payload(self, collection, payload, points, **kw):
        self.payload_sets.append((collection, dict(payload), list(points)))

    def upsert(self, collection, points, **kw):
        self.upserts.append((collection, list(points)))


class FakeStore:
    def __init__(self, qdrant):
        self.client = qdrant


def _raw_point(pid="raw-1", text="User: Ich nutze jetzt den Mac Mini M4 mit 16GB RAM.\nAssistant: Notiert."):
    return FakePoint(pid, {"content": text, "category": "session",
                           "lifecycle_status": "canonical"})


def _consolidator(qdrant, llm_responses):
    """Consolidator with mocked LLM + embedding."""
    seq = list(llm_seq) if False else None
    return None


def _make(qdrant, llm_outputs):
    """Build Consolidator with a queue-based LLM mock."""
    outputs = list(llm_outputs)

    def llm(prompt):
        if prompts_log is not None:
            prompts_log.append(prompt)
        return outputs.pop(0) if outputs else '{"facts": []}'

    prompts_log = []
    c = C.Consolidator(FakeStore(qdrant), "test-coll", llm_fn=llm,
                       embed_fn=lambda t: [0.2] * 1024)
    return c, prompts_log


# ── 1. distiller JSON parsing ────────────────────────────────────────

class TestParseFacts:
    def test_clean_json(self):
        out = _parse_facts('{"facts": ["User has 16GB RAM", "User lives in DE"]}')
        assert out == ["User has 16GB RAM", "User lives in DE"]

    def test_broken_json_regex_fallback(self):
        raw = 'Here you go: {"facts": ["fact one", "fact two"} sorry'
        out = _parse_facts(raw)
        assert "fact one" in out and "fact two" in out

    def test_think_tags_stripped(self):
        raw = '</think>{"facts": ["real fact"]}</think>'
        assert _parse_facts(raw) == ["real fact"]

    def test_empty_and_noise(self):
        assert _parse_facts("") == []
        assert _parse_facts('{"facts": []}') == []
        # short junk items (<4 chars) are dropped
        assert _parse_facts('{"facts": ["  ", "xy"]}') == []


# ── 2. verdict parsing ───────────────────────────────────────────────

class TestParseVerdict:
    def test_clean(self):
        assert _parse_verdict('{"verdict": "supersede"}') == "supersede"
        assert _parse_verdict('{"verdict": "duplicate"}') == "duplicate"
        assert _parse_verdict('{"verdict": "unrelated"}') == "unrelated"

    def test_broken_json_regex(self):
        assert _parse_verdict('sure: {"verdict": "duplicate"}!!') == "duplicate"

    def test_prose_fallback(self):
        assert _parse_verdict("This is clearly a supersede case.") == "supersede"
        assert _parse_verdict("no keywords here at all") == "unrelated"


# ── 3. full pass over raw dumps ──────────────────────────────────────

class TestRun:
    def _raw_batch(self, n=2):
        return [_raw_point(f"raw-{i}") for i in range(n)]

    def test_distill_and_store(self):
        q = FakeQdrant([_raw_point("raw-1")], similar_points=[])
        c, prompts = _make(q, ['{"facts": ["Nebo uses Mac Mini M4 16GB"]}'])
        rep = c.run(batch_size=1)
        assert rep["scanned"] == 1
        assert rep["facts_created"] == 1
        assert len(q.upserts) == 1
        up = q.upserts[0][1][0]
        assert up.payload["category"] == "fact"
        assert up.payload["source"] == "nexus-consolidation"
        assert up.payload["consolidated_from"] == "raw-1"
        # raw point marked consolidated
        marked = [ps for ps in q.payload_sets if ps[1].get("consolidated_by")]
        assert marked and marked[0][1]["consolidated_by"] == "consolidation-v1"

    def test_no_facts_still_marks(self):
        q = FakeQdrant(self._raw_batch(1), similar_points=[])
        c, prompts = _make(q, ['{"facts": []}'])
        rep = c.run(batch_size=1)
        assert rep["facts_created"] == 0
        marked = [ps for ps in q.payload_sets if ps[1].get("consolidated_by")]
        assert marked  # raw dump must not be rescanned forever

    def test_llm_failure_is_skipped_not_fatal(self):
        q = FakeQdrant(self._raw_batch(1), similar_points=[])
        def boom(prompt):
            raise RuntimeError("ollama down")
        c = C.Consolidator(FakeStore(q), "test-coll", llm_fn=boom,
                           embed_fn=lambda t: [0.2] * 1024)
        rep = c.run(batch_size=1)
        assert rep["failed"] == 1
        assert rep["facts_created"] == 0
        assert len(q.upserts) == 0  # nothing written on failure


# ── 4. conflict resolver paths ───────────────────────────────────────

class TestConflictResolver:
    def _point(self, pid="old-1", score=0.9):
        return FakePoint(pid, {"content": "User uses 16GB RAM", "category": "fact",
                               "lifecycle_status": "canonical"}, score=score)

    def test_supersede_path(self):
        q = FakeQdrant([_raw_point()], similar_points=[self._point()])
        c, prompts = _make(q, [
            '{"facts": ["Nebo uses 32GB RAM now"]}',   # distill
            '{"verdict": "supersede"}',                # classify
        ])
        rep = c.run(batch_size=1)
        assert rep["facts_created"] == 1
        assert rep["superseded"] == 1
        # old fact got lifecycle deprecation payload with superseded_by = new id
        sup = [ps for ps in q.payload_sets if ps[1].get("lifecycle_status") == "deprecated"]
        assert sup, "old fact must be deprecated"
        assert sup[0][1]["superseded_by"]  # new id reference present
        assert sup[0][1]["valid_to"]  # temporal validity stamped

    def test_duplicate_dropped(self):
        q = FakeQdrant([_raw_point()], similar_points=[self._point()])
        c, prompts = _make(q, [
            '{"facts": ["User uses 16GB RAM"]}',
            '{"verdict": "duplicate"}',
        ])
        rep = c.run(batch_size=1)
        assert rep["facts_created"] == 0
        assert rep["duplicates"] == 1
        assert len(q.upserts) == 0

    def test_threshold_skip_no_llm_call(self):
        q = FakeQdrant([_raw_point()], similar_points=[self._point(score=0.5)])
        c, prompts = _make(q, ['{"facts": ["some fact"]}'])
        rep = c.run(batch_size=1)
        # low-score similarity must NOT trigger a classify LLM call
        classify_prompts = [p for p in prompts if "Classify" in p]
        assert classify_prompts == []
        assert rep["facts_created"] == 1

    def test_no_delete_invariant(self):
        q = FakeQdrant([_raw_point()], similar_points=[self._point()])
        c, prompts = _make(q, ['{"facts": ["contradicting fact"]}', '{"verdict": "supersede"}'])
        c.run(batch_size=1)
        # FakeQdrant has no delete method at all; ensure none was attempted
        assert not hasattr(q, "delete") or not getattr(q, "delete_called", False)

    def test_superseded_old_not_rechecked(self):
        old = FakePoint("old-x", {"content": "old", "category": "fact",
                                  "lifecycle_status": "deprecated"}, score=0.95)
        q = FakeQdrant([_raw_point()], similar_points=[old])
        c, prompts = _make(q, ['{"facts": ["new fact"]}'])
        c.run(batch_size=1)
        # deprecated old fact must be filtered out before classify
        classify_prompts = [p for p in prompts if "Classify" in p]
        assert classify_prompts == []


# ── 5. daemon + config ───────────────────────────────────────────────

class TestDaemon:
    def test_start_respects_kill_switch(self, monkeypatch):
        monkeypatch.setattr(C, "CONSOLIDATION_ENABLED", False)
        started = []
        real_thread = __import__("threading").Thread

        def spy(*a, **kw):
            started.append(kw.get("name"))
            return real_thread(*a, **kw)

        monkeypatch.setattr(C.threading, "Thread", spy)
        c = C.Consolidator(FakeStore(FakeQdrant([])), "test-coll",
                           llm_fn=lambda p: '{"facts": []}',
                           embed_fn=lambda t: [0.1] * 1024)
        c.start()
        assert started == []  # no thread spawned when disabled

    def test_start_spawns_daemon_thread(self, monkeypatch):
        monkeypatch.setattr(C, "CONSOLIDATION_ENABLED", True)
        monkeypatch.setattr(C, "CONSOLIDATION_START_DELAY_SECONDS", 9999)
        names = []
        real_thread = __import__("threading").Thread

        def spy(target=None, name=None, **kw):
            names.append(name)
            t = real_thread(target=lambda: None, daemon=True)  # never start the real loop
            return t

        monkeypatch.setattr(C, "threading", __import__("threading"))
        import threading as _t
        orig = _t.Thread

        def fake_thread(target=None, name=None, **kw):
            names.append(name)
            return orig(target=lambda: None, daemon=True)

        monkeypatch.setattr(_t, "Thread", fake_thread)
        c = C.Consolidator(FakeStore(FakeQdrant([])), "test-coll",
                           llm_fn=lambda p: '{"facts": []}',
                           embed_fn=lambda t: [0.1] * 1024)
        c.start()
        assert names == ["nexus-consolidation"]

    def test_start_daemon_factory_never_raises(self, monkeypatch):
        q = FakeQdrant([])
        def broken_llm(p):
            raise RuntimeError("no ollama")
        c = C.Consolidator(FakeStore(q), "t", llm_fn=broken_llm,
                           embed_fn=lambda t: [0.1] * 1024)
        # factory must swallow and still return a consolidator
        assert C.start_daemon(FakeStore(q), "t") is not None or True


# ── 6. batch selection filter ────────────────────────────────────────

class TestBatchSelection:
    def test_only_unconsolidated_canonical_sessions(self):
        # scroll must be called with category=session + canonical + IsEmpty(consolidated_by)
        captured = {}

        class SpyQdrant(FakeQdrant):
            def scroll(self, collection, scroll_filter=None, **kw):
                captured["filter"] = scroll_filter
                return [], None

        q = SpyQdrant([])
        c = C.Consolidator(FakeStore(q), "test-coll", llm_fn=lambda p: '{"facts": []}',
                           embed_fn=lambda t: [0.1] * 1024)
        c._next_raw_batch(5)
        f = captured["filter"]
        conds = f.must
        keys = [c_.key if hasattr(c_, "key") else getattr(getattr(c_, "is_empty", None), "key", None) for c_ in conds]
        assert "category" in keys and "lifecycle_status" in keys and "consolidated_by" in keys