"""Tests fuer den Kaltstart-Zustand des Vorlaufs (26.11.2026).

Vier Befunde aus dem Betrieb, jeder mit einer Gegenprobe am alten Stand:

1. Die Frist der Kaltstart-Nachholung (5,0 s) war genau so gross wie das
   Warten auf den Embedder-Dienst. Jede Arbeit, die darueber hinausging
   (Modellaufbau), lief damit in die Frist: der erste Turn nach jedem
   Provider-Neubau begann mit 5,0 s Warten und einem leeren Merkblock
   (12 WARNINGs an einem Tag). Jetzt wird die Vorgabe aus den Teilen des
   Kaltstarts abgeleitet, und ``initialize()`` waermt den Embedder vor.
2. Es gab keinen "vorbereitet"-Zustand: ein leeres Ergebnis wurde als "".
   veroeffentlicht und sah beim naechsten Turn aus wie "noch nichts gelaufen".
   Folge: nach jedem trefferlosen Turn suchte der Kaltstart synchron erneut.
3. Ein belegtes Gate liess den Turn ohne Wartezeit und ohne Log leer
   zurueck — stiller Verlust.
4. ``_clear_prefetch_result()`` schrieb ohne Gate-Pruefung, obwohl der
   Kommentar an der Schreibstelle das als Invariante behauptet.
"""
from __future__ import annotations

import logging
import threading
import time
from types import SimpleNamespace

import pytest

from plugins.memory.nexus import NexusMemoryProvider
from plugins.memory.nexus import __name__ as _plugin_name
import plugins.memory.nexus as _nexus_plugin


_WAIT = 5.0


def _provider() -> NexusMemoryProvider:
    """Provider ohne initialize(): nur die Felder, die diese Tests brauchen."""
    p = NexusMemoryProvider.__new__(NexusMemoryProvider)
    p._prefetch_result = ""
    p._prefetch_prepared = False
    p._prefetch_lock = threading.Lock()
    p._prefetch_gate = threading.Lock()
    p._prefetch_thread_lock = threading.Lock()
    p._coldstart_lock = threading.Lock()
    p._shutting_down = False
    return p


# ── 1. Die Frist ist nicht kleiner als ein Kaltstart ────────────────────────

def test_frist_deckt_einen_kaltstart_und_bleibt_unter_dem_wirt(monkeypatch):
    """Die Vorgabe deckt Dienst-Start plus Modellaufbau, bleibt unter 8,0 s.

    Gegenprobe am alten Stand: dort war die Vorgabe 5,0 s — genau das
    Dienst-Warten, ohne einen einzigen Sekundenbruchteil fuer den Modellaufbau.
    """
    monkeypatch.delenv("NEXUS_COLDSTART_TIMEOUT", raising=False)

    deadline = _nexus_plugin._coldstart_deadline_sec()
    kaltstart = (_nexus_plugin._EMBED_SERVICE_START_POLLS
                 * _nexus_plugin._EMBED_SERVICE_START_INTERVAL
                 + _nexus_plugin._EMBED_MODEL_LOAD_ALLOWANCE_SEC)

    assert deadline > 5.0, (
        "die Frist ist wieder genau das Dienst-Warten — jeder Kaltstart mit "
        "Modellaufbau laeuft dann in die Frist")
    assert deadline >= min(kaltstart, _nexus_plugin._COLDSTART_CEILING_SEC)
    assert deadline < _nexus_plugin._CORE_PREFETCH_DEADLINE_SEC, (
        "die Frist muss unter der Frist des Wirts bleiben, sonst bricht der "
        "Wirt den Abruf zuerst ab")


def test_erster_turn_wartet_den_kaltstart_nicht_ab(monkeypatch):
    """Der langsame erste Embed laeuft im Vorwaermen, nicht im Turn."""
    p = _provider()
    calls = []

    class ColdEmbedder:
        """Erster Aufruf traegt den Modellaufbau, jeder weitere ist warm."""

        def embed(self, text, is_query=True):
            calls.append(text)
            if len(calls) == 1:
                time.sleep(1.0)
            return [0.1, 0.2]

    p._embedder = ColdEmbedder()
    warm = p._start_embed_warmup(wait=_WAIT)   # genau das ruft initialize()
    assert warm is not None

    def inner(query, gate=None):
        p._embedder.embed(query)               # der Turn braucht den Vektor

    p._do_prefetch_inner = inner
    started = time.monotonic()
    p.prefetch("Was war der letzte Stand?")
    turn = time.monotonic() - started

    assert calls[0] == "nexus memory warmup", (
        "der Kaltstart lief nicht im Vorwaermen")
    assert turn < 0.2, f"der Turn wartete {turn:.2f}s auf den Kaltstart"


# ── 2. "vorbereitet, leer" ist ein Ergebnis ─────────────────────────────────

def test_vorbereitet_leer_loest_genau_einen_suchlauf_aus():
    """Zweimal derselbe Aufruf bei leerem, aber fertigem Vorlauf: ein Lauf."""
    p = _provider()
    runs = []

    def inner(query, gate=None):
        runs.append(query)
        with p._prefetch_lock:
            p._prefetch_result = ""       # gesucht und nichts gefunden
            p._prefetch_prepared = True

    p._do_prefetch_inner = inner
    assert p.prefetch("Was war der letzte Stand?") == ""
    assert p.prefetch("Was war der letzte Stand?") == ""
    assert len(runs) == 1, f"der zweite Turn suchte erneut ({len(runs)} Laeufe)"


def test_leeres_ergebnis_setzt_vorbereitet(monkeypatch):
    """Der echte Veroeffentlichungspfad setzt die Marke auch bei null Treffern."""
    p = NexusMemoryProvider()
    p._embedder = SimpleNamespace()
    p._qdrant = SimpleNamespace(
        query_points=lambda **kw: SimpleNamespace(points=[]),
        scroll=lambda **kw: ([], None),
    )
    p._scope_centroids = SimpleNamespace(get=lambda: {})
    monkeypatch.delenv("NEXUS_REWRITE", raising=False)
    monkeypatch.setattr(p, "_embed_cached", lambda text, is_query=True: (1.0, 0.0))
    monkeypatch.setattr(
        p, "_graph_boost",
        lambda top_points, max_boost=3, max_depth=2, out_pids=None: [])
    monkeypatch.setattr(_nexus_plugin, "_keyword_candidates_from_index",
                        lambda *a, **k: [])

    p._do_prefetch_inner("eine Frage ohne Treffer", gate=p._prefetch_gate)

    assert p._prefetch_result == ""
    assert p._prefetch_prepared is True, (
        "ein leeres Ergebnis wurde nicht als 'vorbereitet' markiert")


# ── 3. Belegtes Gate: warten, nehmen was landet, nie stumm ──────────────────

def test_belegtes_gate_wartet_und_nimmt_was_landet(monkeypatch):
    p = _provider()
    monkeypatch.setenv("NEXUS_PREFETCH_GATE_WAIT", "0.5")
    gate = p._prefetch_gate
    gate.acquire()

    def _land_later():
        time.sleep(0.2)
        with p._prefetch_lock:
            p._prefetch_result = "LANDED during the wait"
            p._prefetch_prepared = True
        gate.release()

    threading.Thread(target=_land_later, daemon=True).start()
    started = time.monotonic()
    result = p.prefetch("Was war der letzte Stand?")
    elapsed = time.monotonic() - started

    assert result == "LANDED during the wait"
    assert elapsed >= 0.15, f"es wurde nicht gewartet ({elapsed:.2f}s)"


def test_belegtes_gate_bleibt_nicht_stumm(monkeypatch, caplog):
    p = _provider()
    monkeypatch.setenv("NEXUS_PREFETCH_GATE_WAIT", "0.5")
    gate = p._prefetch_gate
    gate.acquire()
    try:
        with caplog.at_level(logging.DEBUG, logger=_plugin_name):
            started = time.monotonic()
            result = p.prefetch("Was war der letzte Stand?")
            elapsed = time.monotonic() - started
    finally:
        gate.release()

    assert result == ""
    assert elapsed >= 0.45, f"es wurde nicht gewartet ({elapsed:.2f}s)"
    assert any("Coldstart catch-up skipped" in r.getMessage()
               for r in caplog.records), [r.getMessage() for r in caplog.records]


def test_queue_prefetch_bei_belegtem_gate_ist_nicht_stumm(monkeypatch, caplog):
    p = _provider()
    monkeypatch.setenv("NEXUS_PREFETCH_GATE_WAIT", "0.5")
    gate = p._prefetch_gate
    gate.acquire()
    try:
        with caplog.at_level(logging.DEBUG, logger=_plugin_name):
            started = time.monotonic()
            p.queue_prefetch("Was war der letzte Stand?")
            elapsed = time.monotonic() - started
    finally:
        gate.release()

    assert elapsed >= 0.45, f"es wurde nicht gewartet ({elapsed:.2f}s)"
    assert any("queue_prefetch skipped" in r.getMessage()
               for r in caplog.records), [r.getMessage() for r in caplog.records]


# ── 4. Loeschen ist an das Gate gebunden ────────────────────────────────────

def test_clear_ist_an_das_gate_gebunden():
    p = _provider()
    stale = p._prefetch_gate
    p._prefetch_gate = threading.Lock()       # frisches Gate: der Alte ist Zombie
    p._prefetch_result = "frischer Treffer"
    p._prefetch_prepared = True

    p._clear_prefetch_result(stale)
    assert p._prefetch_result == "frischer Treffer", \
        "ein Zombie hat den frischen Treffer geloescht"
    assert p._prefetch_prepared is True

    p._clear_prefetch_result(p._prefetch_gate)
    assert p._prefetch_result == ""
    assert p._prefetch_prepared is False


def test_shutdown_checkpoint_wischt_keinen_frischeren_treffer():
    """Die Pruefpunkte in _do_prefetch reichen ihr Gate durch."""
    p = _provider()
    stale = p._prefetch_gate
    p._prefetch_gate = threading.Lock()       # inzwischen ersetzt
    p._prefetch_result = "frischer Treffer"
    p._prefetch_prepared = True
    p._shutting_down = True

    p._do_prefetch("egal", gate=stale)

    assert p._prefetch_result == "frischer Treffer", \
        "der Shutdown-Pruefpunkt hat einen frischen Treffer geloescht"
    assert p._prefetch_prepared is True
