"""The prefetch/coldstart ``/embed`` request is bounded, and a late run may publish.

Why (27.11.2026): the coldstart catch-up sent the WHOLE trigger turn to
``/embed``. Measured case 08:00: a 13,750-character turn; at the measured
8.47 s per 4,000 characters a cold run needs ~29 s — two 15 s attempts
against a 7.5 s background deadline (8.0 s in the core). The run failed,
published nothing, and the turn lost its memory block:
"Coldstart prefetch still running after 7.5s" -> "returned no vector after
2 attempt(s)".

What this file pins:
  1. An over-long turn text reaches the embedder BOUNDED and keeps both ends
     (the opening names the subject, the end carries the decision).
  2. A short text is embedded unchanged — warm runs (~0.86 s) and ordinary
     recall keep their exact behaviour.
  3. The bound also fits the deadline with one retry: the arithmetic that
     makes (1) a guarantee instead of a hope.
  4. A late but successful run still PUBLISHES (generation rule); only a run
     that started later and published first may displace it. That is what the
     corrected docstrings in the plugin now say.
  5. Every ``/embed`` attempt leaves exactly one log line with the character
     count, the measured duration, the status and the thread that asked.
"""
from __future__ import annotations

import io
import json
import threading
from types import SimpleNamespace

import pytest

import plugins.memory.nexus as nx
from plugins.memory.nexus import NexusMemoryProvider


# Cost per character, from the two measurements behind this fix. The LARGER
# one is what the bound has to fit — a bound that only fits the kinder
# measurement is not a bound.
#   4,000 chars -> 8.47 s  (Kiosha, cold service, 27.11.2026)
#   1,000 chars -> 2.51 s  (this plugin, live service, warm + busy, 27.11.2026)
_COLD_SEC_PER_CHAR = 8.47 / 4000.0
_LIVE_SEC_PER_CHAR = 2.51 / 1000.0
_SEC_PER_CHAR = max(_COLD_SEC_PER_CHAR, _LIVE_SEC_PER_CHAR)
# The turn from the incident report.
_LONG_TURN_CHARS = 13750
# Worst-case ``/healthz`` window, DERIVED from the implementation constants —
# the old test wrote 1.0 here by hand and so proved a guarantee the code did
# not give (four probes at 1.0 s plus the 0.25 s pauses = 5.0 s).
_HEALTHZ_WINDOW_SEC = nx._EMBED_SERVICE_RETRY_STEPS * (
    nx._EMBED_SERVICE_HEALTHZ_TIMEOUT_SEC + nx._EMBED_SERVICE_RETRY_INTERVAL)


class _Resp(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeQdrant:
    """``query_points`` stand-in: always the same canned points."""

    def __init__(self, points=()):
        self._points = list(points)

    def query_points(self, collection_name, query, limit, **kw):
        return SimpleNamespace(points=list(self._points))


@pytest.fixture()
def provider(isolated_env):
    p = NexusMemoryProvider()
    p._prefetch_result = ""
    return p


def _fake_search(provider, monkeypatch, captured=None, payload=None):
    """Truthy stubs so the REAL ``_do_prefetch_inner`` runs.

    Only the backend is faked: the embedder call is captured (that is what
    the bound is about), Qdrant returns at most one ready-made point.
    """
    provider._embedder = SimpleNamespace()
    points = [] if payload is None else [
        SimpleNamespace(id="p1", score=0.9, payload=payload)]
    provider._qdrant = _FakeQdrant(points)
    provider._scope_centroids = SimpleNamespace(get=lambda: {})
    monkeypatch.setattr(provider, "_graph_boost",
                        lambda *a, **k: [])

    def fake_embed_cached(text, is_query=True):
        if captured is not None:
            captured.append(text)
        return (1.0, 0.0)

    monkeypatch.setattr(provider, "_embed_cached", fake_embed_cached)


def test_long_turn_text_reaches_the_embedder_bounded_and_keeps_both_ends(
        provider, monkeypatch):
    """The 13,750-character turn must not go to ``/embed`` in full.

    Red at f848deb: the full text arrived at the embedder, so a cold run paid
    ~29 s against a 7.5 s deadline. Green now: at most the limit, and the
    beginning and the end of the turn are both still in there.
    """
    monkeypatch.delenv("NEXUS_PREFETCH_EMBED_CHARS", raising=False)
    long_turn = ("ANFANG der Turn beschreibt das Thema. " * 400
                 + "MITTE " * 400
                 + "ENTSCHEIDUNG am Ende des Turns.")
    assert len(long_turn) > _LONG_TURN_CHARS
    captured = []
    _fake_search(provider, monkeypatch, captured=captured,
                 payload={"content": "eine vorbereitete Erinnerung",
                          "category": "fact", "scope": "default"})

    provider._do_prefetch_inner(long_turn)

    assert captured, "the embedder was never asked"
    sent = captured[0]
    assert len(sent) <= nx._prefetch_embed_chars(), (
        f"{len(sent)} Zeichen gingen an den Einbetter, Grenze "
        f"{nx._prefetch_embed_chars()}")
    assert sent.startswith("ANFANG der Turn beschreibt das Thema."), (
        "der Anfang des Turns fehlt: " + sent[:80])
    assert sent.endswith("ENTSCHEIDUNG am Ende des Turns."), (
        "das Ende des Turns fehlt: " + sent[-80:])
    assert nx._PREFETCH_EMBED_WINDOW_MARKER in sent


def test_short_text_is_embedded_unchanged(provider, monkeypatch):
    """Warm path and ordinary queries: the text is NOT touched.

    This is the counter-check to the bound — a fix that shortens every query
    would change recall behaviour that works today.
    """
    monkeypatch.delenv("NEXUS_PREFETCH_EMBED_CHARS", raising=False)
    short = "Wie ist der Stand beim Prefetch-Gate?"
    captured = []
    _fake_search(provider, monkeypatch, captured=captured)

    provider._do_prefetch_inner(short)

    assert captured == [short], captured


def test_window_is_exactly_the_limit_for_an_over_long_text(monkeypatch):
    """The window ends at the limit, not below it (no text thrown away twice)."""
    monkeypatch.delenv("NEXUS_PREFETCH_EMBED_CHARS", raising=False)
    window = nx._embed_window("x" * 50000)
    assert len(window) == nx._prefetch_embed_chars()


def test_limit_can_be_set_and_is_clamped(monkeypatch):
    monkeypatch.setenv("NEXUS_PREFETCH_EMBED_CHARS", "900")
    assert nx._prefetch_embed_chars() == 900
    monkeypatch.setenv("NEXUS_PREFETCH_EMBED_CHARS", "1")
    assert nx._prefetch_embed_chars() == 200
    monkeypatch.setenv("NEXUS_PREFETCH_EMBED_CHARS", "999999")
    assert nx._prefetch_embed_chars() == 8000
    monkeypatch.setenv("NEXUS_PREFETCH_EMBED_CHARS", "keine Zahl")
    assert nx._prefetch_embed_chars() == nx._PREFETCH_EMBED_CHARS_DEFAULT


def test_budget_and_window_are_derived_from_the_implementation(monkeypatch):
    """The bound is derived from the deadline, not chosen by feel.

    Red at f848deb by construction: the full turn needs ~29 s, the deadline is
    7.5 s. Green: the window costs ~2.5 s, two measured attempts plus the
    bounded ``/healthz`` window fit the ONE budget, and that budget stays
    below the coldstart deadline and below the core's own 8.0 s deadline.
    """
    monkeypatch.delenv("NEXUS_PREFETCH_EMBED_CHARS", raising=False)
    monkeypatch.delenv("NEXUS_COLDSTART_TIMEOUT", raising=False)
    deadline = nx._coldstart_deadline_sec()
    budget = nx._EmbedBudget.default()

    full = _LONG_TURN_CHARS * _SEC_PER_CHAR
    assert full > budget.remaining(), (
        "der Ausgangsfall ist gar nicht ueber dem Budget")

    window = nx._embed_window("x" * _LONG_TURN_CHARS)
    windowed = len(window) * _SEC_PER_CHAR

    # The health window alone used to be the blind spot; the budget covers it.
    assert _HEALTHZ_WINDOW_SEC < budget.remaining(), (
        f"das /healthz-Fenster allein kostet {_HEALTHZ_WINDOW_SEC:.2f}s, "
        f"Budget {budget.remaining():.2f}s")
    # When /healthz answers at once (the normal path), both measured attempts
    # still fit the shared budget.
    assert 2 * windowed <= budget.remaining(), (
        f"zwei Versuche kosten {2 * windowed:.2f}s, "
        f"Budget {budget.remaining():.2f}s")
    assert budget.remaining() <= deadline, (
        f"Budget {budget.remaining():.2f}s liegt ueber der Frist {deadline:.2f}s")
    assert budget.remaining() < nx._CORE_PREFETCH_DEADLINE_SEC, (
        f"der Kern bricht bei {nx._CORE_PREFETCH_DEADLINE_SEC:.1f}s ab")


def _fake_clock(monkeypatch):
    """Fake ``time.monotonic``/``time.sleep`` so a hang costs no real second."""
    ticks = {"t": 1000.0}
    monkeypatch.setattr(nx.time, "monotonic", lambda: ticks["t"])

    def _sleep(seconds):
        ticks["t"] += max(0.0, seconds)

    monkeypatch.setattr(nx.time, "sleep", _sleep)
    return ticks


def _hanging_service(monkeypatch, ticks):
    """A service whose ``/embed`` always hangs; ``/healthz`` state is switchable.

    Returns ``(calls, phase)``. While ``phase["up"]`` is false every
    ``/healthz`` probe hangs for its whole window and fails (the prefetch
    pre-check case); set it true to model a service that answers ``/healthz``
    within milliseconds while the POSTs themselves still time out — exactly
    the 26.11.2026 case the single retry exists for.
    """
    calls = {"healthz": 0, "embed": 0}
    phase = {"up": False}

    def fake_healthz(timeout=2.0):
        calls["healthz"] += 1
        if not phase["up"]:
            ticks["t"] += max(0.0, timeout)   # hangs for its whole window
            return False
        return True                           # answers at once

    def fake_urlopen(req, timeout=None):
        calls["embed"] += 1
        ticks["t"] += max(0.0, timeout or 0.0)   # hangs for its whole window
        raise TimeoutError("service hung")

    monkeypatch.setattr(nx, "_embed_service_healthz", fake_healthz)
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr(nx, "_service_auth_key", lambda: "test-key")
    return calls, phase


def test_whole_chain_shares_one_budget_and_stays_under_the_ceiling(monkeypatch):
    """The arithmetic the deadline guarantee rests on, measured on a fake clock.

    Scenario: the prefetch pre-check never gets an answer (5.0 s of old
    window), then BOTH embed rounds hang while ``/healthz`` answers between
    them. Red on the old code: each step opened its own window, so the chain
    cost ~5.0 s + 2 × 2.5 s and ran past both deadlines. Green: every wait and
    every timeout is clipped to the ONE budget, and each step gets only its
    slice, so the retry still starts and the chain ends early.
    """
    ticks = _fake_clock(monkeypatch)
    calls, phase = _hanging_service(monkeypatch, ticks)
    monkeypatch.delenv("NEXUS_PREFETCH_EMBED_CHARS", raising=False)
    start = ticks["t"]

    budget = nx._EmbedBudget.default()
    assert nx._wait_for_embed_service(
        budget.slice(nx._EMBED_SERVICE_RETRY_ROUNDS)) is False, (
        "ein nie antwortender /healthz darf hier nicht als Erfolg gelten")
    phase["up"] = True   # reachable, but a POST still runs into its timeout
    assert nx._embed_via_service(
        "x" * nx._prefetch_embed_chars(), budget=budget) is None

    elapsed = ticks["t"] - start
    assert calls["embed"] == nx._EMBED_SERVICE_RETRY_ROUNDS, (
        f"beide Embed-Runden muessen versucht werden: {calls}")
    assert elapsed <= nx._EMBED_BUDGET_DEFAULT_SEC + 1e-9, (
        f"die Kette braucht {elapsed:.2f}s, Budget "
        f"{nx._EMBED_BUDGET_DEFAULT_SEC:.2f}s")
    assert elapsed < nx._COLDSTART_CEILING_SEC, (
        f"die Kette braucht {elapsed:.2f}s, Grenze "
        f"{nx._COLDSTART_CEILING_SEC:.1f}s")
    assert elapsed < nx._CORE_PREFETCH_DEADLINE_SEC, (
        f"der Kern bricht bei {nx._CORE_PREFETCH_DEADLINE_SEC:.1f}s ab")


def test_exhausted_budget_starts_no_further_probe_or_attempt(monkeypatch):
    """An exhausted budget must do NOTHING: no probe, no POST."""
    ticks = _fake_clock(monkeypatch)
    calls, _phase = _hanging_service(monkeypatch, ticks)
    budget = nx._EmbedBudget(0.0)

    assert nx._wait_for_embed_service(budget) is False
    assert nx._embed_via_service("x", budget=budget) is None
    assert calls == {"healthz": 0, "embed": 0}, calls


def test_budget_below_the_minimum_attempt_starts_nothing(monkeypatch):
    """Not only zero: too little for a meaningful attempt is also "stop"."""
    ticks = _fake_clock(monkeypatch)
    calls, _phase = _hanging_service(monkeypatch, ticks)
    budget = nx._EmbedBudget(nx._EMBED_MIN_ATTEMPT_SEC / 2.0)

    assert nx._wait_for_embed_service(budget) is False
    assert nx._embed_via_service("x", budget=budget) is None
    assert calls == {"healthz": 0, "embed": 0}, calls


def test_prefetch_runs_the_embed_call_under_one_budget(provider, monkeypatch):
    """The budget is installed at the prefetch top and reaches the attempt."""
    seen = {}

    def fake_embed_cached(text, is_query=True):
        seen["budget"] = nx._current_embed_budget()
        return (1.0, 0.0)

    _fake_search(provider, monkeypatch)
    monkeypatch.setattr(provider, "_embed_cached", fake_embed_cached)

    provider._do_prefetch_inner("kurz")

    budget = seen.get("budget")
    assert budget is not None, "der Vorlauf hat kein Budget gesetzt"
    assert 0.0 < budget.remaining() <= nx._EMBED_BUDGET_DEFAULT_SEC
    assert nx._current_embed_budget() is None, "Budget-Leck nach dem Lauf"


def test_late_but_successful_run_still_publishes(provider, monkeypatch):
    """Generation rule: the gate is NOT the publication rule.

    A run that outlives the watchdog swap holds a gate that is no longer the
    current one. Until 27.11. the docstring claimed such a run "publishes only
    while that gate is still the current one" — the code never did that, and
    it must not: a late result that arrived is worth more than no memory.
    Only a run that STARTED LATER and published first may displace it.
    """
    _fake_search(provider, monkeypatch,
                 payload={"content": "LATE memory kept",
                          "category": "fact", "scope": "default"})
    stale_gate = provider._prefetch_gate
    assert stale_gate.acquire(blocking=False)
    # The watchdog installed a fresh gate while this run was out.
    provider._prefetch_gate = threading.Lock()
    provider._prefetch_prepared = False

    provider._do_prefetch_inner("Late query", gate=stale_gate)

    with provider._prefetch_lock:
        assert "LATE memory kept" in provider._prefetch_result, (
            "ein verspaeteter, aber erfolgreicher Lauf darf weiter "
            "veroeffentlichen: " + repr(provider._prefetch_result))
        assert provider._prefetch_prepared is True


def test_later_publication_displaces_the_earlier_run(provider):
    """The other half of the rule: a later run's result wins."""
    stale_gate = threading.Lock()
    provider._prefetch_gate = threading.Lock()
    with provider._prefetch_lock:
        provider._prefetch_pub_gen = -1
        assert provider._may_prefetch_write(stale_gate, 1) is True
        provider._prefetch_pub_gen = 2  # a run that started later published
        assert provider._may_prefetch_write(stale_gate, 1) is False


def test_every_embed_attempt_leaves_one_log_line(monkeypatch, caplog):
    """Task 2: who asked, with how many characters, how long, what came back.

    The service logs nothing about ``/embed``, so an outlier used to be
    undiagnosable from both sides. One line per attempt, always.
    """
    def fake_urlopen(req, timeout=None):
        return _Resp(json.dumps(
            {"embedding": [0.1, 0.2], "model": "m", "dim": 2}).encode("utf-8"))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr(nx, "_service_auth_key", lambda: "test-key")
    caplog.set_level("INFO", logger=nx.logger.name)

    assert nx._embed_via_service("hallo welt") is not None

    lines = [r.getMessage() for r in caplog.records
             if "/embed attempt:" in r.getMessage()]
    assert len(lines) == 1, f"genau eine Zeile je Versuch: {lines}"
    line = lines[0]
    assert "chars=10" in line, line
    assert "duration=" in line, line
    assert "status=-" in line, line
    assert "thread=" in line, line
    assert "reason=ok" in line, line


def test_failed_attempt_logs_status_and_reason(monkeypatch, caplog):
    """A refusal is logged with its status and the service's own reason."""
    import urllib.error

    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(
            req.full_url, 503, "Service Unavailable", {},
            io.BytesIO(json.dumps(
                {"error": "this service has no embedding provider available"}
            ).encode("utf-8")))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr(nx, "_service_auth_key", lambda: "test-key")
    monkeypatch.setattr(nx, "_embed_service_healthz", lambda timeout=1.0: True)
    caplog.set_level("INFO", logger=nx.logger.name)

    assert nx._embed_via_service("kurz") is None

    lines = [r.getMessage() for r in caplog.records
             if "/embed attempt:" in r.getMessage()]
    assert len(lines) == 2, f"zwei Versuche, zwei Zeilen: {lines}"
    for line in lines:
        assert "status=503" in line, line
        assert "no embedding provider available" in line, line
        assert "chars=4" in line, line


# ── The budget covers the WHOLE chain, not just the embed (28.11.2026) ──────
# `_EmbedBudget` ended with the embed chain, so everything that runs after it
# read no clock. The rerank sends up to `rerank_pool` candidates to a model and
# costs seconds (measured 3 s for the 40-candidate pool on this machine), so an
# embed of 6 s plus that rerank reaches 9 s — past both the 7.5 s coldstart
# ceiling and the core's 8.0 s deadline. The rerank call lives in `_recall`
# (the prefetch itself does not rerank, see the search test below); the gate
# belongs at that call site and reads the SAME thread-local budget the prefetch
# installs, so no new clock and no second budget.
def _recall_provider(provider, monkeypatch, points):
    """Make the REAL ``_recall`` run against a counting rerank double."""
    provider._embedder = SimpleNamespace()
    provider._qdrant = _FakeQdrant(points)
    provider._scope_centroids = SimpleNamespace(get=lambda: {})
    provider._rerank_cfg = {"enabled": True, "reranker": "cross-encoder",
                            "pool_k": 40}
    monkeypatch.setattr(provider, "_embed_cached",
                        lambda text, is_query=True: (1.0, 0.0))
    monkeypatch.setattr(provider, "_graph_boost", lambda *a, **k: [])
    monkeypatch.setattr(provider, "_flywheel_bump", lambda *a, **k: None)
    monkeypatch.setattr(provider, "_apply_dynamics_tiebreak",
                        lambda pts, eff: pts)
    calls = {"rerank": 0}

    import nexus_memory.reranker as rr

    def fake_rerank(query, pts, *, reranker, pool_k=rr.DEFAULT_POOL_K):
        calls["rerank"] += 1
        return list(reversed(pts))  # visible difference from the vector order

    monkeypatch.setattr(rr, "rerank_points", fake_rerank)
    return calls


def _two_points():
    return [
        SimpleNamespace(id="p1", score=0.9,
                        payload={"id": "p1", "content": "erste Erinnerung",
                                 "category": "fact", "scope": "default"}),
        SimpleNamespace(id="p2", score=0.8,
                        payload={"id": "p2", "content": "zweite Erinnerung",
                                 "category": "fact", "scope": "default"}),
    ]


def test_rerank_is_skipped_when_the_chain_budget_is_spent(
        provider, monkeypatch, caplog):
    """Nearly exhausted budget: no model call, similarity order stands, one line.

    Red before this retrofit: `_recall` had no budget and always reranked, so a
    spent chain still paid the model and could overrun the deadline.
    """
    calls = _recall_provider(provider, monkeypatch, _two_points())
    caplog.set_level("INFO", logger=nx.logger.name)
    budget = nx._EmbedBudget(nx._RERANK_MIN_BUDGET_SEC / 2.0)

    results = provider._recall("Frage", budget=budget)

    assert calls["rerank"] == 0, "der Rerank lief trotz erschoepftem Budget"
    assert [r["id"] for r in results] == ["p1", "p2"], (
        "die Aehnlichkeits-/RRF-Reihenfolge muss unveraendert bleiben: "
        + repr([r["id"] for r in results]))
    lines = [r.getMessage() for r in caplog.records
             if "rerank skipped: prefetch budget" in r.getMessage()]
    assert len(lines) == 1, f"genau eine Log-Zeile mit dem Grund: {lines}"
    assert "needs" in lines[0], lines[0]


def test_rerank_runs_once_when_the_budget_allows_it(
        provider, monkeypatch, caplog):
    """Enough budget: the rerank runs exactly once, as before."""
    calls = _recall_provider(provider, monkeypatch, _two_points())
    caplog.set_level("INFO", logger=nx.logger.name)
    budget = nx._EmbedBudget.default()
    assert budget.remaining() >= nx._RERANK_MIN_BUDGET_SEC

    results = provider._recall("Frage", budget=budget)

    assert calls["rerank"] == 1, calls
    assert [r["id"] for r in results] == ["p2", "p1"], (
        "die Rerank-Reihenfolge wird zurueckgegeben: "
        + repr([r["id"] for r in results]))
    assert not [r for r in caplog.records
                if "rerank skipped" in r.getMessage()], "kein Skip-Grund noetig"


def test_recall_without_budget_is_unchanged(provider, monkeypatch):
    """No budget (the ordinary tool path): rerank still runs exactly once."""
    calls = _recall_provider(provider, monkeypatch, _two_points())

    results = provider._recall("Frage")

    assert calls["rerank"] == 1, calls
    assert [r["id"] for r in results] == ["p2", "p1"], (
        "ohne Budget muss sich der Abruf exakt wie vorher verhalten")


def test_exhausted_embed_budget_keeps_its_reason(monkeypatch):
    """A spent clock is distinguishable from "no vector"."""
    ticks = _fake_clock(monkeypatch)
    _hanging_service(monkeypatch, ticks)
    nx._remember_embed_failure("")

    assert nx._embed_via_service("x", budget=nx._EmbedBudget(0.0)) is None

    reason = nx._embed_failure_note()
    assert "embed budget exhausted" in reason, reason
    assert "returned no vector" not in reason, reason


def test_budget_exhausted_mid_attempt_keeps_its_reason(monkeypatch):
    """The second site: the first POST eats the rest, the next one never starts.

    ``_embed_via_service`` used to end either with a timeout text or nothing at
    all. "No time left" must be written as its own fact, not as "no vector".
    """
    ticks = _fake_clock(monkeypatch)
    _hanging_service(monkeypatch, ticks)
    nx._remember_embed_failure("")
    budget = nx._EmbedBudget(nx._EMBED_MIN_ATTEMPT_SEC * 1.6)

    assert nx._embed_via_service("x", budget=budget) is None

    reason = nx._embed_failure_note()
    assert "embed budget exhausted" in reason, reason
    assert "returned no vector" not in reason, reason


def test_prefetch_search_always_runs_even_when_the_budget_is_spent(
        provider, monkeypatch):
    """The search is local and cheap; a spent clock must not drop the memories.

    The budget exists to bound the EXPENSIVE steps. Skipping the Qdrant search
    would cost the turn its memories for a limit that never applied to it.
    """
    ticks = _fake_clock(monkeypatch)
    seen = {"queries": 0}

    class _CountingQdrant(_FakeQdrant):
        def query_points(self, collection_name, query, limit, **kw):
            seen["queries"] += 1
            return SimpleNamespace(points=list(self._points))

    provider._embedder = SimpleNamespace()
    provider._qdrant = _CountingQdrant([
        SimpleNamespace(id="p1", score=0.9,
                        payload={"content": "Erinnerung bleibt",
                                 "category": "fact", "scope": "default"})])
    provider._scope_centroids = SimpleNamespace(get=lambda: {})
    monkeypatch.setattr(provider, "_graph_boost", lambda *a, **k: [])

    def slow_embed(text, is_query=True):
        ticks["t"] += nx._EMBED_BUDGET_DEFAULT_SEC + 1.0  # budget is gone after
        return (1.0, 0.0)

    monkeypatch.setattr(provider, "_embed_cached", slow_embed)

    provider._do_prefetch_inner("Frage")

    assert seen["queries"] == 1, "die Qdrant-Suche wurde wegen des Budgets uebersprungen"
    assert "Erinnerung bleibt" in provider._prefetch_result
