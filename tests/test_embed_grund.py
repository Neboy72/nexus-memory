"""Ein gescheiterter ``/embed`` nennt seinen Grund (26.11.2026).

Befund aus dem Betrieb: 14:48:45,765 „Embed request failed but /healthz
answers — retrying once", 14:48:45,771 bereits der Endfehler „answers /healthz
but returned no vector after 2 attempt(s)". Vier POSTs in 0,13 s, kein
15-s-Timeout, kein 401, der Dienst antwortete auf /healthz. Warum der Abruf
scheiterte, konnte niemand sagen: ``_post_embed`` fing jede Ausnahme mit
``except Exception: return None, None`` und warf den Grund weg.

Diese Datei haelt vier Dinge fest:
  1. Eine HTTP-Antwort des Dienstes wird mit Status und der Meldung des
     Dienstes benannt (die 503 „no embedding provider available" ist genau
     die Antwort, die von aussen wie ein toter Dienst aussah).
  2. Ein Zeitfehler nennt die tatsaechlich gewartete Zeit neben der Frist —
     gemessen, nicht als fester Text.
  3. Ein Verbindungsabbruch wird als solcher benannt.
  4. Die Meldung an den Agenten enthaelt den Grund, nicht nur „no vector".
"""
from __future__ import annotations

import io
import json
import re
import time

import pytest

import plugins.memory.nexus as nx


class _Resp(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _ohne_retry(monkeypatch):
    """Nur die POSTs zaehlen: kein echter Healthz-Poll, kein Warten."""
    monkeypatch.setattr(nx, "_service_auth_key", lambda: "test-key")
    monkeypatch.setattr(nx, "_embed_service_healthz", lambda timeout=1.0: True)


def test_503_mit_abgelehnter_meldung_wird_benannt(monkeypatch, caplog):
    """Der Dienst antwortet 503 und sagt selbst, warum — das gehoert in den Grund."""
    import urllib.error

    meldung = {"error": "this service has no embedding provider available",
               "hint": "the default is local (HuggingFace); check the service log"}

    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(
            req.full_url, 503, "Service Unavailable", {},
            io.BytesIO(json.dumps(meldung).encode("utf-8")))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    _ohne_retry(monkeypatch)
    caplog.set_level("INFO", logger=nx.logger.name)

    assert nx._embed_via_service("hallo") is None

    grund = nx._embed_failure_note()
    assert "HTTP 503" in grund, grund
    assert "no embedding provider available" in grund, grund
    assert "retrying once" in caplog.text, caplog.text
    assert "HTTP 503" in caplog.text, (
        "die Wiederholungszeile muss den Grund nennen: " + caplog.text)


def test_zeitfehler_nennt_gemessene_wartezeit(monkeypatch):
    """Ein Timeout nennt die tatsaechlich gewartete Zeit neben der Frist."""
    import socket
    import urllib.error

    def fake_urlopen(req, timeout=None):
        time.sleep(0.06)  # die Wartezeit, die gemessen werden soll
        raise urllib.error.URLError(socket.timeout("timed out"))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    _ohne_retry(monkeypatch)

    assert nx._embed_via_service("hallo") is None
    grund = nx._embed_failure_note()
    assert "timeout after" in grund, grund
    assert f"limit {nx._EMBED_SERVICE_TIMEOUT_SEC:.1f}s" in grund, grund
    treffer = re.search(r"timeout after ([\d.]+)s", grund)
    assert treffer and float(treffer.group(1)) >= 0.05, (
        f"die Zeit wurde nicht gemessen: {grund}")


def test_verbindungsabbruch_wird_benannt(monkeypatch):
    import urllib.error

    def fake_urlopen(req, timeout=None):
        raise urllib.error.URLError(
            ConnectionResetError(54, "Connection reset by peer"))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    _ohne_retry(monkeypatch)

    assert nx._embed_via_service("hallo") is None
    assert "connection reset" in nx._embed_failure_note()


def test_antwort_ohne_vektor_wird_benannt(monkeypatch):
    """200, aber kein Vektor: auch das ist ein Grund, kein Schweigen."""
    def fake_urlopen(req, timeout=None):
        return _Resp(json.dumps({"model": "m", "dim": 0}).encode("utf-8"))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    _ohne_retry(monkeypatch)

    assert nx._embed_via_service("hallo") is None
    grund = nx._embed_failure_note()
    assert "answer without vector" in grund, grund
    assert "keys=dim,model" in grund, grund


def test_unlesbare_antwort_wird_benannt(monkeypatch):
    def fake_urlopen(req, timeout=None):
        return _Resp(b"<html>502 Bad Gateway</html>")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    _ohne_retry(monkeypatch)

    assert nx._embed_via_service("hallo") is None
    assert "unreadable answer" in nx._embed_failure_note()


def test_stummer_healthz_wird_als_grund_genannt(monkeypatch):
    """Der Dienst antwortet gar nicht mehr — auch das ist ein benannter Grund."""
    import urllib.error

    def fake_urlopen(req, timeout=None):
        raise urllib.error.URLError(ConnectionRefusedError(61, "Connection refused"))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr(nx, "_service_auth_key", lambda: "test-key")
    monkeypatch.setattr(nx, "_embed_service_healthz", lambda timeout=1.0: False)

    assert nx._embed_via_service("hallo") is None
    grund = nx._embed_failure_note()
    assert "connection refused" in grund, grund
    assert "/healthz did not answer" in grund, grund


def test_meldung_an_den_agenten_nennt_die_ursache(monkeypatch):
    """Die RuntimeError-Meldung traegt den Grund — nicht nur „no vector"."""
    def fake_embed(text, is_query=True):
        nx._remember_embed_failure("HTTP 503: this service has no embedding provider available")
        return None

    monkeypatch.setattr(nx, "_embed_via_service", fake_embed)
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
    assert "no embedding provider available" in text, text
    assert "HTTP 503" in text, text


def test_erfolg_loescht_den_alten_grund(monkeypatch):
    """Ein spaeterer Treffer darf den Grund des letzten Fehlschlags nicht erben."""
    nx._remember_embed_failure("HTTP 503: alt")

    def fake_urlopen(req, timeout=None):
        return _Resp(json.dumps(
            {"embedding": [0.1, 0.2], "model": "m", "dim": 2}).encode("utf-8"))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    _ohne_retry(monkeypatch)

    assert nx._embed_via_service("hallo")["dim"] == 2
    assert nx._embed_failure_note() == ""
