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
# How long ``/healthz`` may hold up the single retry (bounded wait).
_HEALTHZ_WAIT_SEC = 1.0


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


def test_cold_case_fits_the_deadline_even_with_one_retry(monkeypatch):
    """The bound is derived from the deadline, not chosen by feel.

    Red at f848deb by construction: the full turn needs ~29 s, the deadline is
    7.5 s. Green: the window costs ~2.5 s, and even two attempts plus the
    bounded ``/healthz`` wait stay below the coldstart deadline and below the
    core's own 8.0 s prefetch deadline.
    """
    monkeypatch.delenv("NEXUS_PREFETCH_EMBED_CHARS", raising=False)
    monkeypatch.delenv("NEXUS_COLDSTART_TIMEOUT", raising=False)
    deadline = nx._coldstart_deadline_sec()

    full = _LONG_TURN_CHARS * _SEC_PER_CHAR
    assert full > deadline, "der Ausgangsfall ist gar nicht ueber der Frist"

    window = nx._embed_window("x" * _LONG_TURN_CHARS)
    windowed = len(window) * _SEC_PER_CHAR
    worst_case = 2 * windowed + _HEALTHZ_WAIT_SEC

    assert windowed < deadline / 2, f"ein Versuch kostet {windowed:.2f}s"
    assert worst_case < deadline, (
        f"zwei Versuche plus healthz brauchen {worst_case:.2f}s, "
        f"Frist {deadline:.1f}s")
    assert worst_case < nx._CORE_PREFETCH_DEADLINE_SEC, (
        f"der Kern bricht bei {nx._CORE_PREFETCH_DEADLINE_SEC:.1f}s ab")


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
