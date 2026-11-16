"""Wachen fuer ``GET /api/service-log`` — den Logs-Knopf im Dashboard.

Drei Zusagen, die hier haengen:

1. Die Route liest ueber ``nexus_memory.service_log`` (dieselbe Quelle wie das
   CLI) und liefert die Zeilen des Tages, neueste zuerst — mit Kettenstatus.
2. Ein Tag ohne Datei ist ein LEERER Tag: HTTP 200 mit ``count: 0``. Ein 404
   waere eine Aussage ueber das Protokoll, die die Route nicht treffen kann.
3. Ein kaputtes Datum wird abgewiesen, statt still auf "heute" zu fallen —
   sonst zeigte die Antwort einen anderen Tag als angefragt.
"""

import os
import sys

import pytest

# dashboard/server.py beendet beim Import den Prozess, wenn FastAPI fehlt
# (``sys.exit(1)``) — derselbe Riegel wie in den anderen Dashboard-Tests.
pytest.importorskip("fastapi", reason="dashboard-Server braucht fastapi")
pytest.importorskip("uvicorn", reason="dashboard-Server braucht uvicorn")

DASH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dashboard"
)
if DASH not in sys.path:
    sys.path.insert(0, DASH)

import server  # noqa: E402

from nexus_memory import service_log  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402


def _client() -> TestClient:
    return TestClient(server.app)


def test_route_liest_das_protokoll_mit_kettenstatus():
    """Gefuellte Tagesdatei: die Zeilen kommen zurueck, neueste zuerst."""
    service_log.record("write", "ok", actor="ayosha", target_id="a",
                       text="Der Verteiler steht im Flur",
                       access_level="trusted")
    service_log.record("forget", "ok", actor="ayosha", target_id="b",
                       reason="veraltet", access_level="trusted")

    resp = _client().get("/api/service-log", params={"limit": 3})

    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 2
    assert body["chain"]["ok"] is True, "die Kette muss geprueft sein"
    assert body["chain"]["lines"] == 2
    assert body["chain"]["broken_at"] is None
    assert [event["target_id"] for event in body["events"]] == ["b", "a"], \
        "neueste Zeile zuerst"
    # Die Zeile kommt so, wie sie im Protokoll steht — kein Nachladen.
    assert body["events"][0] == service_log.read_events(limit=1)[0]


def test_route_filtert_nach_aktion_und_absender():
    service_log.record("write", "ok", actor="ayosha", target_id="a")
    service_log.record("denied", "refused", actor="hermes", target_id="b")
    service_log.record("denied", "refused", actor="ayosha", target_id="c")

    client = _client()
    nur_abgewiesen = client.get("/api/service-log",
                                params={"action": "denied"}).json()
    assert [event["target_id"] for event in nur_abgewiesen["events"]] == ["c", "b"]

    nur_ayosha = client.get("/api/service-log", params={"actor": "ayosha"}).json()
    assert [event["target_id"] for event in nur_ayosha["events"]] == ["c", "a"]


def test_tag_ohne_datei_ist_leer_und_kein_404():
    """Kein Protokoll fuer diesen Tag = HTTP 200, count 0."""
    resp = _client().get("/api/service-log", params={"date": "2000-01-01"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 0
    assert body["events"] == []
    assert body["chain"]["lines"] == 0


def test_kaputtes_datum_wird_abgewiesen():
    """Statt still auf heute zu fallen: 400."""
    assert _client().get("/api/service-log",
                         params={"date": "15.11.2026"}).status_code == 400
