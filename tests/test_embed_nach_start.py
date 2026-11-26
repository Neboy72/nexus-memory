"""Der erste Abruf nach einem Prozessstart darf nicht "unavailable" melden (26.11.2026).

Befund aus dem Betrieb (0.22.74, `~/.hermes/logs/agent.log`):
``Prefetch failed: ⚠ local embedder unavailable: local embedder service
answers on http://127.0.0.1:9122`` — der Dienst antwortete also, waehrend der
Plugin-Pfad ihn fuer tot erklaerte. Zeitlich: Start 12:00:30, Waechter
12:00:38, Fehler 12:00:45. Das sind die 15 s von
``_EMBED_SERVICE_TIMEOUT_SEC``: ein einzelner ``/embed``-Aufruf lief in den
Timeout, wurde nicht wiederholt, und ``_Embedder.embed`` uebersetzte das in
"unavailable" — mit dem Healthz-Text im selben Satz.

Drei Zusagen, die diese Datei festhaelt:
  1. Ein fehlgeschlagener ``/embed`` wird nach kurzem, healthz-bestaetigtem
     Warten EINMAL wiederholt; kommt der Vektor, ist es ein normaler Treffer.
  2. Antwortet ``/healthz``, wird nie "local embedder unavailable" gemeldet —
     sondern ein fehlgeschlagener Abruf.
  3. Ein Lauf, der die Frist ueberschreitet, darf sein Ergebnis nachliefern,
     solange kein spaeter gestarteter Lauf inzwischen veroeffentlicht hat.
"""
from __future__ import annotations

import io
import json
import threading
import time
from types import SimpleNamespace

import pytest

import plugins.memory.nexus as nx
from plugins.memory.nexus import NexusMemoryProvider


class _FakeQdrant:
    def __init__(self, points):
        self._points = points
        self.queries = 0

    def query_points(self, collection_name, query, limit, **kw):
        self.queries += 1
        return SimpleNamespace(points=list(self._points))


def _hit():
    return SimpleNamespace(
        id="p1", score=0.9,
        payload={"content": "FRISCHER Treffer aus dem Dienst",
                 "category": "fact", "scope": "default"})


@pytest.fixture()
def provider(isolated_env):
    p = NexusMemoryProvider()
    p._qdrant = _FakeQdrant([_hit()])
    p._scope_centroids = SimpleNamespace(get=lambda: {})
    p._embedder = SimpleNamespace()          # "verfuegbar", Vektor kommt unten
    return p


def _wire(monkeypatch, p, *, service_ok):
    """Echter Prefetch-Pfad, nur die teuren Helfer ersetzt."""
    monkeypatch.delenv("NEXUS_REWRITE", raising=False)
    monkeypatch.setattr(p, "_embed_cached", lambda text, is_query=True: [0.1, 0.2])
    monkeypatch.setattr(
        p, "_graph_boost",
        lambda top_points, max_boost=3, max_depth=2, out_pids=None: [])
    monkeypatch.setattr(nx, "_keyword_candidates_from_index", lambda *a, **k: [])


# ── 1. Erst der Retry: ein Timeout ist kein "unavailable" ───────────────────

class _Resp(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_embed_wird_nach_fehler_einmal_wiederholt(monkeypatch):
    """Erster POST scheitert (Timeout), der zweite liefert den Vektor."""
    import urllib.error

    calls = {"n": 0}

    def fake_urlopen(req, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise urllib.error.URLError("timed out")
        body = json.dumps({"embedding": [0.3, 0.4], "model": "m", "dim": 2})
        return _Resp(body.encode())

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr(nx, "_service_auth_key", lambda: "k")
    monkeypatch.setattr(nx, "_embed_service_healthz", lambda timeout=1.0: True)

    resp = nx._embed_via_service("hallo")
    assert resp is not None, "der fehlgeschlagene Abruf wurde nicht wiederholt"
    assert resp["dim"] == 2 and calls["n"] == 2


def test_healthz_antwortet_kein_unavailable_text(monkeypatch):
    """Antwortet der Dienst auf /healthz, ist "unavailable" die falsche Meldung."""
    monkeypatch.setattr(nx, "_embed_via_service", lambda text, is_query=True: None)
    monkeypatch.setattr(
        nx, "embedder_status",
        lambda: {"status": "ok", "source": "service",
                 "detail": "local embedder service answers on http://127.0.0.1:9122",
                 "repair": "/pfad/bin/python3 -m nexus_memory.mcp_server serve",
                 "endpoint": "http://127.0.0.1:9122"})

    emb = object.__new__(nx._Embedder)
    emb._impl = None
    emb._service_ok = True
    emb._service_model = ""
    emb._service_dim = 0
    emb._service_process = None
    emb._local_error = ""

    with pytest.raises(RuntimeError) as excinfo:
        emb.embed("hallo")
    text = str(excinfo.value)
    assert "local embedder unavailable" not in text, (
        f"falsche Meldung trotz antwortendem Dienst: {text}")
    assert "embed request failed" in text.lower()


# ── 2. Der veraltete Verfuegbarkeits-Vermerk ───────────────────────────────

def test_erster_abruf_nach_start_liefert_treffer_statt_notiz(provider, monkeypatch):
    """_service_ok ist vom Start her False, der Dienst laeuft aber.

    Am alten Stand veroeffentlichte der Vorlauf sofort die "unavailable"-Notiz,
    ohne den Dienst ein einziges Mal zu fragen.
    """
    _wire(monkeypatch, provider, service_ok=False)

    # Ein echter _Embedder, dessen Flag vom Prozessstart noch False ist.
    emb = object.__new__(nx._Embedder)
    emb._impl = None
    emb._service_ok = False            # veraltet: Dienst war beim Start noch nicht da
    emb._service_model = ""
    emb._service_dim = 0
    emb._service_process = None
    emb._local_error = ""
    calls = {"embed": 0}

    def fake_embed(text, is_query=True):
        calls["embed"] += 1
        return [0.1, 0.2]

    emb.embed = fake_embed
    provider._embedder = emb

    probed = {"n": 0}

    def fake_healthz(timeout=1.0):
        probed["n"] += 1
        return True                    # /healthz antwortet

    monkeypatch.setattr(nx, "_embed_service_healthz", fake_healthz)
    provider._do_prefetch_inner("Was war der letzte Stand?", gate=provider._prefetch_gate)

    assert probed["n"] >= 1, "der veraltete Vermerk wurde nicht nachgeprueft"
    assert provider._qdrant.queries == 1, \
        "der Vorlauf hat gar nicht gesucht, sondern sofort die Notiz gesetzt"
    assert "unavailable" not in provider._prefetch_result, \
        f"statt des Treffers kam die Notiz: {provider._prefetch_result[:120]}"
    assert "FRISCHER Treffer" in provider._prefetch_result


# ── 3. Ein verspaeteter Lauf darf nachliefern ──────────────────────────────

def test_verspaeteter_lauf_veroeffentlicht_wenn_niemand_frischeres_hat(provider, monkeypatch):
    """Nach dem Gate-Tausch darf der alte Lauf liefern — nichts Frischeres da."""
    _wire(monkeypatch, provider, service_ok=True)
    stale_gate = provider._prefetch_gate
    provider._prefetch_gate = threading.Lock()     # Waechter hat getauscht
    with provider._prefetch_lock:
        provider._prefetch_result = ""
        provider._prefetch_prepared = False

    provider._do_prefetch_inner("Was war der letzte Stand?", gate=stale_gate)

    assert "FRISCHER Treffer" in provider._prefetch_result, (
        "der verspaetete Lauf hat sein Ergebnis verworfen, obwohl niemand ein "
        "frischeres veroeffentlicht hat")
    assert provider._prefetch_prepared is True


def test_verspaeteter_lauf_ueberschreibt_keinen_frischeren_treffer(provider, monkeypatch):
    """Gegenprobe: hat ein spaeter gestarteter Lauf geliefert, bleibt er stehen."""
    _wire(monkeypatch, provider, service_ok=True)
    stale_gate = provider._prefetch_gate
    provider._prefetch_gate = threading.Lock()
    with provider._prefetch_lock:
        provider._prefetch_result = "FRISCHER Treffer aus dem Dienst"
        provider._prefetch_prepared = True
        provider._prefetch_pub_gen = 99          # spaeterer Lauf hat geliefert

    provider._do_prefetch_inner("Was war der letzte Stand?", gate=stale_gate)

    assert provider._prefetch_result == "FRISCHER Treffer aus dem Dienst", \
        "der verspaetete Lauf hat den frischeren Treffer ueberschrieben"
