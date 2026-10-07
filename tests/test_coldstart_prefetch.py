"""Tests for the coldstart catch-up net (05.10.2026).

Why: Nexus prepares memory at the END of a turn (queue_prefetch) and serves
it in the NEXT one. A freshly built agent therefore has nothing prepared in
its first turn — and agents are rebuilt often (gateway eviction, background
review, cron). Measured: 36 of 85 turns on one day started that way. The
tests assert that:
  1. a coldstart turn gets memory caught up synchronously,
  2. the warm path (already prepared) does NOT search twice,
  3. trivial queries ("ok", "/new") do not catch up,
  4. shutdown does not catch up,
  5. a backend error never breaks the turn (fail-open),
  6. both locks exist on __new__ instances (no AttributeError),
  7. a timed-out coldstart installs a fresh gate, so later prefetches
     still run (review fix 4, part 1),
  8. a timed-out coldstart's zombie cannot overwrite a fresher result
     written through the new gate (review fix 4, part 2).
"""
from __future__ import annotations

import logging
import threading
import time
from types import SimpleNamespace

import pytest

from plugins.memory.nexus import NexusMemoryProvider, _is_trivial_query


@pytest.fixture()
def provider(isolated_env):
    """A provider with an empty buffer and a clean environment.

    ``isolated_env`` matters here: the module under test reads NEXUS_SCOPE,
    NEXUS_REWRITE and friends, and a developer machine that exports them would
    otherwise leak into the assertions below.
    """
    p = NexusMemoryProvider()
    p._prefetch_result = ""
    return p


def test_coldstart_catches_up(provider, monkeypatch):
    """Empty buffer + real query -> the memory is caught up synchronously."""
    def fake_inner(query, gate=None):
        with provider._prefetch_lock:
            provider._prefetch_result = "HIT caught up by the coldstart"

    monkeypatch.setattr(provider, "_do_prefetch_inner", fake_inner)
    result = provider.prefetch("What was our latest state?", session_id="s1")
    assert result == "HIT caught up by the coldstart"


def test_warm_path_does_not_search_twice(provider, monkeypatch):
    """Already prepared -> immediate return, no second search run."""
    provider._prefetch_result = "already prepared"
    calls = []

    def fake_inner(query, gate=None):
        calls.append(query)

    monkeypatch.setattr(provider, "_do_prefetch_inner", fake_inner)
    result = provider.prefetch("What is X?", session_id="s1")
    assert result == "already prepared"
    assert calls == [], "a prepared buffer must not trigger a new search"


@pytest.mark.parametrize("query", ["ok", "/new", "", "   ", "continue", "yes"])
def test_trivial_query_does_not_catch_up(provider, monkeypatch, query):
    """Core rule: bare acknowledgements/commands deliberately skip the recall."""
    calls = []
    monkeypatch.setattr(provider, "_do_prefetch_inner",
                        lambda q, gate=None: calls.append(q))
    provider.prefetch(query, session_id="s1")
    assert calls == []


def test_boundary_queries_are_not_trivial(provider, monkeypatch):
    """Counter-check: 'go' and 'danke' are NOT trivial per the core."""
    assert _is_trivial_query("go") is False
    assert _is_trivial_query("danke") is False
    assert _is_trivial_query("Ja, mach mal") is False


def test_shutdown_does_not_catch_up(provider, monkeypatch):
    provider._shutting_down = True
    calls = []
    monkeypatch.setattr(provider, "_do_prefetch_inner",
                        lambda q, gate=None: calls.append(q))
    assert provider.prefetch("What is X?", session_id="s1") == ""
    assert calls == []


def test_backend_error_is_fail_open(provider, monkeypatch):
    """A broken backend must never break the turn."""
    def boom(query, gate=None):
        raise RuntimeError("Qdrant gone")

    monkeypatch.setattr(provider, "_do_prefetch_inner", boom)
    assert provider.prefetch("What is X?", session_id="s1") == ""


def test_hung_backend_does_not_block_the_turn(monkeypatch, provider):
    """The hard deadline kicks in instead of blocking the turn forever."""
    monkeypatch.setenv("NEXUS_COLDSTART_TIMEOUT", "0.4")

    def slow(query, gate=None):
        time.sleep(5)

    monkeypatch.setattr(provider, "_do_prefetch_inner", slow)
    started = time.time()
    result = provider.prefetch("What is X?", session_id="s1")
    elapsed = time.time() - started
    assert result == ""
    assert elapsed < 2.0, f"the turn must not have blocked for {elapsed:.1f}s"


def test_single_flight_avoids_duplicate_search(provider, monkeypatch):
    """A prefetch already running -> the coldstart does not race it."""
    provider._prefetch_gate.acquire()  # simulates a running queue_prefetch
    calls = []
    monkeypatch.setattr(provider, "_do_prefetch_inner",
                        lambda q, gate=None: calls.append(q))
    try:
        assert provider.prefetch("What is X?", session_id="s1") == ""
        assert calls == []
    finally:
        provider._prefetch_gate.release()


def test_locks_missing_on_new_instance(monkeypatch):
    """__new__ instances (bench pattern) must not die with AttributeError."""
    p = NexusMemoryProvider.__new__(NexusMemoryProvider)
    p._prefetch_result = ""
    p._prefetch_lock = threading.Lock()
    p._shutting_down = False
    p._coldstart_lock = None
    monkeypatch.setattr(p, "_do_prefetch_inner", lambda q, gate=None: None)
    assert p.prefetch("What is X?", session_id="s1") == ""
    assert p._coldstart_lock is not None


# ── Review fix 4, part 1: the timeout must never wedge the gate forever ──

def test_timeout_installs_fresh_gate_and_later_prefetch_still_runs(
        provider, monkeypatch, caplog):
    """A stuck backend must not silence memory for every later turn.

    Regression: after the deadline the coldstart thread still held
    _prefetch_gate, so each later queue_prefetch bounced off a gate only the
    zombie could release — every following turn stayed memory-less, silently.
    The timeout now swaps in a fresh gate and reports it loudly (WARNING).
    """
    monkeypatch.setenv("NEXUS_COLDSTART_TIMEOUT", "0.4")
    stuck = threading.Event()
    second_started = threading.Event()
    calls = []

    def fake_inner(query, gate=None):
        calls.append(query)
        if len(calls) == 1:
            stuck.wait(5.0)  # zombie run: never released in this test
        else:
            second_started.set()

    monkeypatch.setattr(provider, "_do_prefetch_inner", fake_inner)
    gate_before = provider._prefetch_gate

    started = time.time()
    assert provider.prefetch("What is X?", session_id="s1") == ""
    elapsed = time.time() - started
    assert elapsed < 2.0, f"the turn must not block past the deadline ({elapsed:.1f}s)"
    assert provider._prefetch_gate is not gate_before, \
        "the timeout must install a fresh gate"
    assert any(
        rec.levelno >= logging.WARNING and "fresh prefetch gate" in rec.getMessage()
        for rec in caplog.records
    ), "the gate swap must be logged loudly (WARNING, not debug)"

    provider.queue_prefetch("And now?")  # must squeeze through the fresh gate
    assert second_started.wait(2.0), \
        "queue_prefetch never ran although the gate is free"


# ── Review fix 4, part 2: the zombie must not overwrite a fresh result ──

class _FakeQdrant:
    """query_points stand-in: canned points keyed by the query vector."""

    def __init__(self, points_by_vector):
        self._points_by_vector = points_by_vector

    def query_points(self, collection_name, query, limit):
        for vector, points in self._points_by_vector.items():
            if list(query) == list(vector):
                return SimpleNamespace(points=points)
        return SimpleNamespace(points=[])


def test_zombie_cannot_overwrite_fresh_result(provider, monkeypatch):
    """After a timeout, a fresh result must survive the zombie's late write.

    Regression: the orphaned coldstart thread kept holding the OLD gate after
    the fresh-gate swap; a later prefetch produced a fresher result through
    the new gate, and the zombie's stale write clobbered it. The zombie now
    only publishes while its gate is still the current one. The REAL
    _do_prefetch_inner stays live here — only its helpers are faked — so the
    write guards under test actually execute.
    """
    monkeypatch.setenv("NEXUS_COLDSTART_TIMEOUT", "0.4")
    monkeypatch.delenv("NEXUS_REWRITE", raising=False)
    zombie_started = threading.Event()
    zombie_release = threading.Event()
    zombie_thread = []

    # Truthy stubs so the real _do_prefetch_inner runs; scoping is fail-open.
    provider._embedder = SimpleNamespace()
    provider._qdrant = _FakeQdrant({
        (1.0, 0.0): [],  # zombie run: nothing found -> its "write" clears
        (0.0, 1.0): [SimpleNamespace(
            id="p1", score=0.9,
            payload={"content": "FRESH prepared memory",
                     "category": "fact", "scope": "default"})],
    })
    provider._scope_centroids = SimpleNamespace(get=lambda: {})
    monkeypatch.setattr(provider, "_graph_boost",
                        lambda top_points, max_boost=3, max_depth=2, out_pids=None: [])

    def fake_embed_cached(text, is_query=True):
        if text == "Coldstart query":
            zombie_thread.append(threading.current_thread())
            zombie_started.set()
            zombie_release.wait(2.0)  # zombie freezes mid-search
            return (1.0, 0.0)
        return (0.0, 1.0)

    monkeypatch.setattr(provider, "_embed_cached", fake_embed_cached)

    assert provider.prefetch("Coldstart query", session_id="s1") == "", \
        "the stuck coldstart must return empty after the deadline"
    assert zombie_started.is_set(), "the zombie never reached the fake embedder"

    # While the zombie is frozen, a fresh prefetch lands through the new gate.
    provider.queue_prefetch("Fresh query")
    fresh_result = ""
    deadline = time.time() + 2.0
    while time.time() < deadline:
        with provider._prefetch_lock:
            fresh_result = provider._prefetch_result
        if "FRESH prepared memory" in fresh_result:
            break
        time.sleep(0.02)
    assert "FRESH prepared memory" in fresh_result, \
        "the fresh prefetch never landed while the zombie was frozen"

    # Now let the zombie finish and try its late write/clear. Wait for the
    # exact captured thread — an earlier test's leftover zombie can share the
    # thread name.
    zombie_release.set()
    assert zombie_thread, "no zombie thread was captured"
    zombie_thread[0].join(2.0)
    assert not zombie_thread[0].is_alive(), "the zombie never finished"

    with provider._prefetch_lock:
        after = provider._prefetch_result
    assert after == fresh_result, \
        "the zombie's late write wiped or replaced the fresh result"
    assert "FRESH prepared memory" in after

# ── Review fix 4, part 3: the zombie's failure path must not CLEAR either ──

def test_zombie_failure_path_cannot_clear_a_fresh_result(provider, monkeypatch):
    """A zombie that errors late must not wipe the fresh result.

    Regression: the except-branch of the prefetch also writes
    ``_prefetch_result = ""``. Un-guarded, a zombie failing after the gate swap
    cleared a fresher result produced through the new gate — memory went dark
    even though a fresh search HAD succeeded.
    """
    monkeypatch.setenv("NEXUS_COLDSTART_TIMEOUT", "0.4")
    monkeypatch.delenv("NEXUS_REWRITE", raising=False)
    zombie_release = threading.Event()
    zombie_thread = []

    provider._embedder = SimpleNamespace()

    def fake_embed_cached(text, is_query=True):
        if text == "Coldstart query":
            zombie_thread.append(threading.current_thread())
            zombie_release.wait(2.0)  # zombie freezes mid-search
            raise RuntimeError("backend died after the timeout")
        return (0.0, 1.0)

    monkeypatch.setattr(provider, "_embed_cached", fake_embed_cached)
    provider._qdrant = _FakeQdrant({(0.0, 1.0): [SimpleNamespace(
        id="p1", score=0.9,
        payload={"content": "FRESH prepared memory",
                 "category": "fact", "scope": "default"})]})
    provider._scope_centroids = SimpleNamespace(get=lambda: {})
    monkeypatch.setattr(provider, "_graph_boost",
                        lambda top_points, max_boost=3, max_depth=2, out_pids=None: [])

    assert provider.prefetch("Coldstart query", session_id="s1") == "", \
        "the stuck coldstart must return empty after the deadline"

    # A fresh prefetch lands through the replacement gate while the zombie hangs.
    provider.queue_prefetch("Fresh query")
    fresh = ""
    deadline = time.time() + 2.0
    while time.time() < deadline:
        with provider._prefetch_lock:
            fresh = provider._prefetch_result
        if "FRESH prepared memory" in fresh:
            break
        time.sleep(0.02)
    assert "FRESH prepared memory" in fresh, \
        "the fresh prefetch never landed while the zombie was frozen"

    # Let the zombie fail and try its late clear.
    zombie_release.set()
    assert zombie_thread, "no zombie thread was captured"
    zombie_thread[0].join(2.0)
    assert not zombie_thread[0].is_alive(), "the zombie never finished"

    with provider._prefetch_lock:
        after = provider._prefetch_result
    assert "FRESH prepared memory" in after, \
        "the zombie's failure path wiped the fresh result"
