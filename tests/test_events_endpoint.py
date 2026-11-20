"""``POST /events`` — die neutrale Vorfall-Meldung (0.22.61).

Der Punkt der Sache: das Träumen liest keine fremde Sitzungsdatenbank mehr.
Jeder Agent meldet seine gescheiterten Aufrufe hier; Nexus klassifiziert
selbst. Diese Tests fahren den echten ASGI-App in-process, ohne Port und
ohne Qdrant.
"""
from __future__ import annotations

import json

import pytest

import nexus_memory.mcp_server as mcp

pytestmark = pytest.mark.usefixtures("admin_caller")


@pytest.fixture(autouse=True)
def _isolierte_ablage(monkeypatch, tmp_path):
    monkeypatch.setenv("NEXUS_SERVICE_AUTH_KEY", "test-service-key")
    monkeypatch.setenv("NEXUS_EVENT_DIR", str(tmp_path / "events"))


def _auth_headers():
    from nexus_memory import service_auth
    return {service_auth.AUTH_HEADER: service_auth.issue_key("hermes", "Hermes")}


def _client():
    from starlette.testclient import TestClient
    return TestClient(mcp.build_serve_app(), base_url="http://127.0.0.1:9122")


def _journal_zeilen():
    from nexus_memory import events
    pfad = events.journal_path()
    if not pfad.exists():
        return []
    return [json.loads(z) for z in pfad.read_text(encoding="utf-8").splitlines() if z.strip()]


def test_ein_vorfall_wird_angenommen():
    with _client() as client:
        antwort = client.post("/events", headers=_auth_headers(), json={
            "tool": "terminal", "ok": False,
            "error": "ModuleNotFoundError: No module named 'requests'",
            "hint": "Falscher Interpreter: venv nutzen.",
            "session": "s1"})
    assert antwort.status_code == 200, antwort.text
    assert antwort.json() == {"stored": 1, "received": 1}
    zeilen = _journal_zeilen()
    assert len(zeilen) == 1
    assert zeilen[0]["tool"] == "terminal" and zeilen[0]["ok"] is False
    # Ohne eigenen agent-Namen gilt der Schlüssel, mit dem geschrieben wurde.
    assert zeilen[0]["agent"] == "hermes"


def test_liste_und_eigener_agent_name():
    with _client() as client:
        antwort = client.post("/events", headers=_auth_headers(), json=[
            {"tool": "terminal", "ok": False, "error": "Exit 124: timed out after 300s",
             "session": "a", "agent": "openclaw"},
            {"tool": "read_file", "ok": True, "session": "b"},
        ])
    assert antwort.json() == {"stored": 2, "received": 2}
    zeilen = _journal_zeilen()
    assert [z["agent"] for z in zeilen] == ["openclaw", "hermes"]


def test_ohne_werkzeug_keine_zeile():
    with _client() as client:
        antwort = client.post("/events", headers=_auth_headers(),
                              json={"ok": False, "error": "irgendwas"})
    assert antwort.status_code == 200
    assert antwort.json()["stored"] == 0
    assert _journal_zeilen() == []


def test_zu_viele_auf_einmal_werden_abgewiesen():
    with _client() as client:
        antwort = client.post("/events", headers=_auth_headers(),
                              json=[{"tool": "t", "ok": False}] * 501)
    assert antwort.status_code == 413
    assert _journal_zeilen() == []


def test_kaputter_koerper_ist_ein_fehler():
    with _client() as client:
        antwort = client.post(
            "/events",
            headers={**_auth_headers(), "Content-Type": "application/json"},
            content=b"kein json")
    assert antwort.status_code == 400
    assert _journal_zeilen() == []


def test_ohne_schluessel_kein_zutritt():
    with _client() as client:
        antwort = client.post("/events", json={"tool": "terminal", "ok": False})
    assert antwort.status_code == 401
    assert _journal_zeilen() == []
