"""Regression tests: prefetch thread vs. shutdown() client close.

Production symptom (2026-09-30, ~12x/day):::

    NexusMemoryProvider shut down
    Prefetch failed: 'NoneType' object has no attribute 'query_points'

``shutdown()`` joined the write/backup/update threads and then closed the
Qdrant client, but a prefetch thread spawned by ``queue_prefetch`` could
still be in flight — it then dereferenced the now-``None`` client and the
session silently got an empty memory block. These tests exercise the real
ordering: shutdown() must not close the client while a prefetch is running.

The shutdown grace period is bounded on purpose (a hung prefetch must never
stall exit). A prefetch that legitimately outlives the grace therefore cannot
be aborted retroactively while it is inside the client call; what must hold is
that (1) shutdown reports the survivor at ERROR instead of silently ignoring
the join result, (2) the thread aborts at its next checkpoint instead of
dereferencing a closed/``None`` client, and (3) a deliberate shutdown never
emits the alarming "Prefetch failed" warning that hid the original bug.

Two further guarantees are pinned here:

* the shutdown *flag* is not the same as the shutdown *cause* — a genuine
  Qdrant/network failure inside the (multi-second) shutdown window keeps its
  WARNING and its health re-probe (P2-A);
* an abort checkpoint publishes ``_prefetch_result = ""`` so a consumer during
  shutdown never reads a stale result from an earlier query (P2-C).
"""

from __future__ import annotations

import importlib.util
import logging
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "src"))

_PLUGIN_PATH = _REPO_ROOT / "plugins" / "memory" / "nexus" / "__init__.py"
_spec = importlib.util.spec_from_file_location(
    "nexus_hermes_plugin_prefetch_race", str(_PLUGIN_PATH))
_nexus_plugin = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_nexus_plugin)

NexusMemoryProvider = _nexus_plugin.NexusMemoryProvider
_JOIN_TIMEOUT = _nexus_plugin._PREFETCH_JOIN_TIMEOUT
_PLUGIN_LOGGER = _nexus_plugin.logger.name


class _FakePoint:
    def __init__(self, content: str, score: float = 0.9):
        self.id = "p1"
        self.payload = {"content": content, "category": "fact"}
        self.score = score


class _BlockingQdrant:
    """Fake client whose ``query_points`` can outlast the shutdown grace.

    ``queries_after_close`` counts calls that were *initiated* against an
    already-closed client — the guard's contract is that this stays 0.
    ``closed_while_in_flight`` records the (physically unavoidable) case where
    ``close()`` runs while a call that already started is still sleeping; the
    bounded grace cannot retroactively abort such a call. ``closed_at`` /
    ``finished_at`` let a test assert the close happened *after* a short
    in-flight call returned (impossible on the pre-fix code).

    ``release_event`` makes a call block deterministically until the test
    releases it (instead of racing a ``sleep``); ``fail_with`` raises a chosen
    exception after the call returns/block ends.
    """

    def __init__(self, points=None, block: float = 0.3, raise_after_close: bool = False,
                 release_event: threading.Event | None = None, fail_with=None):
        self._points = points or []
        self._block = block
        self._raise_after_close = raise_after_close
        self._release = release_event
        self._fail_with = fail_with
        self.started = threading.Event()
        self.finished = threading.Event()
        self.closed = False
        self.closed_while_in_flight = False
        self.queries_after_close = 0
        self.query_calls = 0
        self.started_at: float | None = None
        self.finished_at: float | None = None
        self.closed_at: float | None = None
        self._in_flight = False
        self._lock = threading.Lock()

    def query_points(self, **kwargs):
        with self._lock:
            self.query_calls += 1
            if self.closed:
                self.queries_after_close += 1  # dereferencing a closed client
            self._in_flight = True
        self.started_at = time.monotonic()
        self.started.set()
        if self._release is not None:
            self._release.wait(5.0)
        elif self._block:
            time.sleep(self._block)
        with self._lock:
            self._in_flight = False
        self.finished_at = time.monotonic()
        self.finished.set()
        if self._fail_with is not None:
            raise self._fail_with
        if self.closed and self._raise_after_close:
            # A real Qdrant client fails requests after close().
            raise RuntimeError("qdrant client has been closed")
        return SimpleNamespace(points=self._points)

    def close(self):
        with self._lock:
            self.closed = True
            self.closed_at = time.monotonic()
            if self._in_flight:
                self.closed_while_in_flight = True


def _make_provider(client, monkeypatch):
    """A real (non-``__new__``) provider with its network edges stubbed."""
    monkeypatch.setenv("NEXUS_REWRITE", "0")  # no fuel/LLM call in prefetch
    prov = NexusMemoryProvider()
    prov._collection = "nexus_test"
    prov._qdrant = client
    prov._embedder = SimpleNamespace(embed=lambda text, is_query=True: [0.0, 1.0])
    prov._embed_cache = SimpleNamespace(get=lambda *a: None, put=lambda *a: None)
    # scope_auto would otherwise build ScopeCentroids against the fake client.
    prov._scope_centroids = SimpleNamespace(get=lambda: {})
    # graph-boost is out of scope here and would touch a real SkillGraph.
    monkeypatch.setattr(prov, "_graph_boost", lambda *a, **k: [])
    return prov


def _records(caplog, level, phrase):
    """caplog records emitted by *this* module's logger at exactly ``level``."""
    return [r for r in caplog.records
            if r.name == _PLUGIN_LOGGER and r.levelno == level
            and phrase in r.getMessage()]


def _grace_errors(caplog):
    return _records(caplog, logging.ERROR, "grace period")


# ── the in-flight cases ──────────────────────────────────────────────────────

def test_shutdown_waits_for_in_flight_prefetch(monkeypatch, caplog):
    """A prefetch outlasting the grace: shutdown stays bounded, reports the
    survivor at ERROR, and never initiates a query against the closed client.

    Pre-fix code (no prefetch join) closed the client immediately *and* emitted
    no ERROR, so both the elapsed/ERROR assertions below fail there.
    """
    overrun = _JOIN_TIMEOUT + 0.3
    client = _BlockingQdrant(points=[], block=overrun)
    prov = _make_provider(client, monkeypatch)

    prov.queue_prefetch("was ist das rezept")
    assert client.started.wait(2.0), "prefetch thread never reached query_points"

    started_at = time.monotonic()
    caplog.set_level(logging.DEBUG)
    prov.shutdown()  # must return, not hang, not raise
    elapsed = time.monotonic() - started_at

    # Bounded: shutdown enforces the join deadline instead of waiting the
    # whole call out (and instead of closing immediately, pre-fix).
    assert elapsed >= _JOIN_TIMEOUT * 0.9, elapsed
    assert elapsed < overrun, elapsed
    # The ignored join result is now a loud, explicit ERROR naming the grace.
    errors = _grace_errors(caplog)
    assert errors, caplog.text
    assert f"{_JOIN_TIMEOUT:.1f}" in errors[0].getMessage()

    assert client.closed is True
    assert prov._qdrant is None
    # No query is *initiated* after close (the already-started call may still be
    # in flight when close() runs — the bounded grace cannot abort it).
    assert client.queries_after_close == 0
    survivor = prov._prefetch_thread
    assert survivor is not None
    survivor.join(5.0)
    assert not survivor.is_alive()
    assert client.query_calls == 1
    assert "Prefetch failed" not in caplog.text


def test_shutdown_closes_only_after_subgrace_prefetch_finishes(monkeypatch, caplog):
    """A prefetch that finishes *inside* the grace is joined, not raced.

    This is what the join exists for: ``close()`` must happen strictly after
    the in-flight call returned. Pre-fix code closed immediately, so
    ``closed_at >= finished_at`` and ``closed_while_in_flight is False`` both
    fail there.
    """
    client = _BlockingQdrant(points=[], block=0.3)
    prov = _make_provider(client, monkeypatch)

    prov.queue_prefetch("schnelle abfrage")
    assert client.started.wait(2.0), "prefetch thread never reached query_points"

    with caplog.at_level(logging.WARNING):
        started_at = time.monotonic()
        prov.shutdown()
        elapsed = time.monotonic() - started_at

    # It really waited for the join instead of racing ahead of the call.
    assert elapsed >= 0.25, elapsed
    assert client.finished.is_set()
    assert client.closed is True
    assert client.closed_at is not None and client.finished_at is not None
    assert client.closed_at >= client.finished_at
    assert client.closed_while_in_flight is False
    assert client.queries_after_close == 0
    assert client.query_calls == 1
    assert _grace_errors(caplog) == []
    assert "Prefetch failed" not in caplog.text


def test_slow_prefetch_survivor_is_loud_and_safe(monkeypatch, caplog):
    """Prefetch outliving the grace: bounded, reported, and non-fatal."""
    slow_query = _JOIN_TIMEOUT + 1.0
    client = _BlockingQdrant(points=[], block=slow_query, raise_after_close=True)
    prov = _make_provider(client, monkeypatch)

    prov.queue_prefetch("langsame abfrage")
    assert client.started.wait(2.0), "prefetch thread never reached query_points"

    started_at = time.monotonic()
    caplog.set_level(logging.DEBUG)
    prov.shutdown()  # must return, not hang, not raise
    elapsed = time.monotonic() - started_at

    # Bounded: it waited the whole grace and then gave up (rather than waiting
    # the full 3 s call out, or closing without waiting at all).
    assert elapsed >= _JOIN_TIMEOUT * 0.9, elapsed
    assert elapsed < slow_query, elapsed
    # The ignored join result is now a loud, explicit ERROR from this logger.
    errors = _grace_errors(caplog)
    assert errors, caplog.text
    assert f"{_JOIN_TIMEOUT:.1f}" in errors[0].getMessage()

    # shutdown still closed the client afterwards.
    assert client.closed is True
    assert prov._qdrant is None

    # The survivor finishes its already-started call, issues no new client
    # call, and never escapes as an unhandled AttributeError.
    survivor = prov._prefetch_thread
    assert survivor is not None
    survivor.join(5.0)
    assert not survivor.is_alive()
    assert client.query_calls == 1
    assert client.queries_after_close == 0
    # Requirement: a deliberate shutdown is not misreported as a failure.
    assert "Prefetch failed" not in caplog.text
    assert "aborted by shutdown" in caplog.text


def test_shutdown_race_aborts_before_touching_client(monkeypatch, caplog):
    """A prefetch still pre-checkpoint when shutdown starts aborts cleanly."""
    client = _BlockingQdrant(points=[], block=0.0)
    prov = _make_provider(client, monkeypatch)
    prov._prefetch_result = "stale result from an earlier query"

    embed_entered = threading.Event()
    proceed = threading.Event()

    def slow_embed(query):
        embed_entered.set()
        assert proceed.wait(5.0)
        return [0.0, 1.0]

    monkeypatch.setattr(prov, "_embed_cached", slow_embed)
    prov.queue_prefetch("race")
    assert embed_entered.wait(2.0), "prefetch never entered the embedder"

    with caplog.at_level(logging.DEBUG):
        shutdown_thread = threading.Thread(target=prov.shutdown, name="test-shutdown")
        shutdown_thread.start()
        deadline = time.monotonic() + 2.0
        while not prov._shutting_down and time.monotonic() < deadline:
            time.sleep(0.005)
        assert prov._shutting_down, "shutdown never marked the provider shutting down"
        proceed.set()
        shutdown_thread.join(5.0)
    assert not shutdown_thread.is_alive()

    # The checkpoint held: the client was never touched at all, and the stale
    # result from the earlier query was cleared under the lock (P2-C).
    assert client.query_calls == 0
    assert client.queries_after_close == 0
    assert client.closed_while_in_flight is False
    assert prov.prefetch("race") == ""
    assert "Prefetch failed" not in caplog.text


def test_prefetch_checkpoints_clear_stale_result(monkeypatch):
    """Both ``_shutting_down`` checkpoints clear ``_prefetch_result``."""
    client = _BlockingQdrant(points=[], block=0.0)
    prov = _make_provider(client, monkeypatch)
    prov._shutting_down = True

    prov._prefetch_result = "stale from earlier query"
    prov._do_prefetch("egal")  # outer checkpoint (spawn/turn race)
    assert prov.prefetch("egal") == ""
    assert client.query_calls == 0

    prov._prefetch_result = "stale again"
    prov._do_prefetch_inner("egal")  # inner checkpoint (after embedding)
    assert prov.prefetch("egal") == ""
    assert client.query_calls == 0


# ── cause vs. flag (P2-A) ─────────────────────────────────────────────────────

def test_predicate_matches_only_shutdown_caused_errors():
    pred = _nexus_plugin._prefetch_error_explained_by_shutdown
    # Explained by the shutdown: None client / client closed underneath us.
    assert pred(AttributeError("'NoneType' object has no attribute 'query_points'"))
    assert pred(RuntimeError("qdrant client has been closed"))
    assert pred(ValueError("client is closed"))
    # Genuine failures must NOT be excused by the shutdown flag.
    assert not pred(RuntimeError("Connection refused by Qdrant"))
    assert not pred(RuntimeError("Connection reset by peer"))
    assert not pred(OSError("No space left on device"))


def test_genuine_failure_during_shutdown_keeps_warning_and_selfcheck(monkeypatch, caplog):
    """A real error inside the shutdown window is not downgraded to DEBUG.

    The prefetch is already past its checkpoint (inside ``query_points``) when
    shutdown starts, and the call then fails for a real reason: the handler
    must branch on the CAUSE, warn, and still refresh the health self-check.
    """
    release = threading.Event()
    client = _BlockingQdrant(points=[], release_event=release,
                             fail_with=RuntimeError("Connection refused by Qdrant"))
    prov = _make_provider(client, monkeypatch)
    refreshed = []
    monkeypatch.setattr(_nexus_plugin, "_refresh_selfcheck_if_needed",
                        lambda: refreshed.append(True))

    prov.queue_prefetch("echter fehler")
    assert client.started.wait(2.0), "prefetch thread never reached query_points"

    caplog.set_level(logging.DEBUG)
    shutdown_thread = threading.Thread(target=prov.shutdown, name="test-shutdown")
    shutdown_thread.start()
    deadline = time.monotonic() + 2.0
    while not prov._shutting_down and time.monotonic() < deadline:
        time.sleep(0.002)
    assert prov._shutting_down
    release.set()
    shutdown_thread.join(5.0)
    assert not shutdown_thread.is_alive()
    thread = prov._prefetch_thread
    assert thread is not None
    thread.join(5.0)
    assert not thread.is_alive()

    warnings = _records(caplog, logging.WARNING, "during shutdown")
    assert warnings, caplog.text
    assert refreshed == [True], "health re-probe skipped for a genuine failure"
    # It must NOT have taken the shutdown-noise branch.
    assert _records(caplog, logging.DEBUG, "aborted by shutdown") == []


# ── refusal / stress ──────────────────────────────────────────────────────────

def test_queue_prefetch_after_shutdown_does_not_spawn(monkeypatch, caplog):
    client = _BlockingQdrant(points=[], block=0.0)
    prov = _make_provider(client, monkeypatch)
    prov.shutdown()

    with caplog.at_level(logging.INFO):
        prov.queue_prefetch("too late")

    assert client.query_calls == 0
    # The refusal is explicit, never a silent drop.
    assert "shutting down" in caplog.text
    # The single-flight gate must be free again (not leaked by the skip).
    assert prov._prefetch_gate.acquire(blocking=False) is True
    prov._prefetch_gate.release()


def test_fresh_instance_always_has_the_prefetch_thread_lock():
    """P2-D: ``__init__`` defines the lock, so ``queue_prefetch`` must fail
    fast on an instance that lacks it instead of silently substituting a
    second, unseen lock (which shutdown() would never read)."""
    prov = NexusMemoryProvider.__new__(NexusMemoryProvider)
    prov._prefetch_gate = threading.Lock()
    prov._shutting_down = False
    with pytest.raises(AttributeError):
        prov.queue_prefetch("ohne thread-lock")


def test_stress_queue_prefetch_then_shutdown_never_queries_after_close(monkeypatch, caplog):
    """Nets the original race across three deterministic round kinds.

    * fast rounds: the client returns at once (the common path);
    * survivor rounds: the client blocks LONGER than the join grace, so
      shutdown must time out, log the survivor, and close underneath it;
    * embed rounds: a blocked embedder holds the thread before its first client
      touch, so the checkpoint must abort without ever querying.

    The grace is monkeypatched small only to keep the loop fast; the ordering
    it exercises is the real one.
    """
    grace = 0.1
    fast_rounds, survivor_rounds, embed_rounds = 150, 4, 4

    total_after_close = 0
    survivor_query_calls = 0
    with caplog.at_level(logging.DEBUG):
        # Fast rounds — no prefetch outlives the grace.
        monkeypatch.setattr(_nexus_plugin, "_PREFETCH_JOIN_TIMEOUT", 1.0)
        for _ in range(fast_rounds):
            client = _BlockingQdrant(points=[], block=0.0)
            prov = _make_provider(client, monkeypatch)
            prov.queue_prefetch("stress runde")
            prov.shutdown()
            thread = prov._prefetch_thread
            if thread is not None:
                thread.join(2.0)
            total_after_close += client.queries_after_close

        # Survivor rounds — the call blocks past the grace deterministically.
        monkeypatch.setattr(_nexus_plugin, "_PREFETCH_JOIN_TIMEOUT", grace)
        for _ in range(survivor_rounds):
            release = threading.Event()
            client = _BlockingQdrant(points=[], release_event=release,
                                     raise_after_close=True)
            prov = _make_provider(client, monkeypatch)
            prov.queue_prefetch("survivor runde")
            assert client.started.wait(2.0)
            prov.shutdown()  # times out, ERRORs, closes under the survivor
            release.set()  # let the survivor observe the closed client
            thread = prov._prefetch_thread
            assert thread is not None
            thread.join(5.0)
            assert not thread.is_alive()
            total_after_close += client.queries_after_close
            survivor_query_calls += client.query_calls

        # Embed rounds — blocked before the first client touch.
        # A longer grace keeps the shutdown join deterministic here.
        monkeypatch.setattr(_nexus_plugin, "_PREFETCH_JOIN_TIMEOUT", 1.0)
        for _ in range(embed_rounds):
            client = _BlockingQdrant(points=[], block=0.0)
            prov = _make_provider(client, monkeypatch)
            entered = threading.Event()
            proceed = threading.Event()

            def slow_embed(query, _entered=entered, _proceed=proceed):
                _entered.set()
                assert _proceed.wait(5.0)
                return [0.0, 1.0]

            prov._embed_cached = slow_embed
            prov._prefetch_result = "stale"
            prov.queue_prefetch("embed runde")
            assert entered.wait(2.0)
            shutdown_thread = threading.Thread(target=prov.shutdown,
                                               name="stress-shutdown")
            shutdown_thread.start()
            deadline = time.monotonic() + 2.0
            while not prov._shutting_down and time.monotonic() < deadline:
                time.sleep(0.002)
            assert prov._shutting_down
            proceed.set()
            shutdown_thread.join(5.0)
            assert not shutdown_thread.is_alive()
            thread = prov._prefetch_thread
            assert thread is not None
            thread.join(5.0)
            # The checkpoint held: no client touch and no stale result.
            assert client.query_calls == 0
            assert client.queries_after_close == 0
            assert prov.prefetch("embed runde") == ""

    assert total_after_close == 0
    # Every survivor did reach the client exactly once before close.
    assert survivor_query_calls == survivor_rounds
    assert "Prefetch failed" not in caplog.text
    # Only the survivor rounds may report a grace-period overrun.
    assert len(_grace_errors(caplog)) == survivor_rounds
    assert len(_records(caplog, logging.DEBUG, "aborted by shutdown")) >= survivor_rounds


def test_prefetch_still_returns_memories(monkeypatch):
    client = _BlockingQdrant(points=[_FakePoint("the cake recipe")], block=0.0)
    prov = _make_provider(client, monkeypatch)

    prov.queue_prefetch("rezept")
    assert client.finished.wait(2.0), "prefetch never queried the live client"
    thread = prov._prefetch_thread
    if thread is not None:
        thread.join(2.0)

    text = prov.prefetch("rezept")
    assert "the cake recipe" in text
    assert client.query_calls == 1
    assert client.queries_after_close == 0
