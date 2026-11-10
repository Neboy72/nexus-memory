"""Der HTTP-Schreibweg ``POST /write`` — das Tor fuer die TypeScript-Plugins.

Warum es diesen Test gibt: das OpenClaw-Plugin sprach bisher direkt mit
Qdrant und umging damit jede Pruefung. Der Endpunkt nimmt den Schreibwunsch
entgegen und schickt ihn durch ``nexus.write_gate`` (ueber
``MemoryStore.remember``). Geprueft wird hier, dass ein abgewiesener Eintrag
die Sammlung nicht erreicht — nicht nur, dass eine Fehlermeldung zurueckkommt.

Bewusst am echten ``remember``: ein Fake, der selbst prueft, wuerde nur den
Fake testen.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

FAKT = "Der Verteiler liegt im Flur neben der Kellertuer"  # 47 Zeichen
REGEL = "Laut Paperless-Anleitung bleibt der Verteiler dauerhaft ausgeschaltet"


class _FakeQdrant:
    """Nur da, damit ``remember`` ein Attribut vorfindet; Schreibwege sind Attrappen."""

    def query_points(self, **_kwargs):
        raise AssertionError("ein abgewiesener Eintrag darf keine Aehnlichkeitssuche starten")


@pytest.fixture(autouse=True)
def _service_key(monkeypatch):
    monkeypatch.setenv("NEXUS_SERVICE_AUTH_KEY", "test-service-key")


@pytest.fixture
def written(monkeypatch):
    """Fängt den echten Schreibvorgang ab und zaehlt die Punkte."""
    import nexus_memory.mcp_server as mcp
    import nexus_memory.collection_vectors as collection_vectors

    points: list = []
    monkeypatch.setattr(
        collection_vectors,
        "upsert_named",
        lambda _client, _collection, pts, _vector_name: points.extend(pts),
    )
    # Der Stichwortindex ist eine Zugabe und hat mit dieser Frage nichts zu tun.
    monkeypatch.setattr(mcp, "_index_written_point", lambda _payload: None)

    store = object.__new__(mcp.MemoryStore)
    store.vector_name = None
    store._hybrid_retriever = None
    store._skill_graph = None
    store._scope_centroids = None
    store.client = _FakeQdrant()

    async def _embed(_text, is_query=True):
        return [0.1] * 4

    store._embed = _embed
    monkeypatch.setattr(mcp, "get_store", lambda: store)
    return points


def _client(monkeypatch):
    from starlette.testclient import TestClient
    import nexus_memory.mcp_server as mcp

    return TestClient(mcp.build_serve_app(), base_url="http://127.0.0.1:9122")


def _headers():
    from nexus_memory import service_auth

    return {service_auth.AUTH_HEADER: "test-service-key"}


def _post(monkeypatch, payload):
    return _client(monkeypatch).post("/write", json=payload, headers=_headers())


def test_leerer_inhalt_wird_abgewiesen_und_nicht_geschrieben(monkeypatch, written):
    r = _post(monkeypatch, {
        "content": "   ",
        "category": "fact",
        "source": "openclaw_tool",
        "source_type": "chat",
    })

    assert r.status_code == 422
    body = r.json()
    assert body["status"] == "rejected"
    assert "leer" in body["reason"]
    assert written == [], "ein abgewiesener Eintrag darf die Sammlung nicht erreichen"


def test_dokumenttext_als_regel_wird_abgewiesen_und_nicht_geschrieben(monkeypatch, written):
    r = _post(monkeypatch, {
        "content": REGEL,
        "category": "rule",
        "access_level": "private",
        "source": "paperless",
        "source_type": "paperless",
    })

    assert r.status_code == 422
    body = r.json()
    assert body["status"] == "rejected"
    assert "Regel" in body["reason"]
    assert written == [], "Dokumenttext darf nie als Regel in der Sammlung landen"


def test_regel_aus_dem_gespraech_wird_geschrieben(monkeypatch, written):
    """Gegenprobe: dieselbe Laenge, aber ausdruecklich aus dem Gespraech.

    ``explicit`` setzt nur das Werkzeug (Store-Tool), nie die automatische
    Erfassung eines Turns — deshalb steht es im Antrag.
    """
    r = _post(monkeypatch, {
        "content": REGEL,
        "category": "rule",
        "access_level": "private",
        "source": "openclaw_tool",
        "source_type": "chat",
        "scope": "work",
        "explicit": True,
    })

    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert len(written) == 1
    payload = written[0].payload
    assert payload["content"] == REGEL
    assert payload["category"] == "rule"
    assert payload["provenance"]["source"]


def test_regel_aus_automatischer_erfassung_wird_abgewiesen(monkeypatch, written):
    """Die automatische Erfassung eines Turns laesst ``explicit`` weg.

    Sie laeuft bei jedem Turn; eine so entstandene Regel wuerde dauerhaft
    eingespielt. Deshalb: 422 und nichts in der Sammlung.
    """
    r = _post(monkeypatch, {
        "content": REGEL,
        "category": "rule",
        "access_level": "private",
        "source": "openclaw",
        "source_type": "chat",
        "scope": "work",
    })

    assert r.status_code == 422
    assert "Regel" in r.json()["reason"]
    assert written == [], "automatische Erfassung darf nie eine Regel schreiben"


def test_sitzungsquelle_hebt_die_stufe_mindestens_auf_trusted(monkeypatch, written):
    r = _post(monkeypatch, {
        "content": FAKT,
        "category": "session",
        "access_level": "public",
        "source": "openclaw",
        "source_type": "session",
        "scope": "work",
    })

    assert r.status_code == 200
    assert written[0].payload["access_level"] == "trusted"


def test_gelieferte_kennung_wird_uebernommen(monkeypatch, written):
    """Die Retry-Queue des Plugins braucht dieselbe Kennung, sonst verdoppelt
    ein Wiederholungsversuch den Eintrag statt ihn zu ersetzen."""
    kid = "3f8f5c2e-6a1b-4c7d-9e2f-1a2b3c4d5e6f"
    r = _post(monkeypatch, {
        "id": kid,
        "content": FAKT,
        "category": "session",
        "access_level": "private",
        "source": "openclaw",
        "source_type": "session",
        "scope": "work",
    })

    assert r.status_code == 200
    assert written[0].id == kid
    assert written[0].payload["id"] == kid


@pytest.mark.parametrize("payload", [
    {},
    {"content": 42},
    {"content": ["a", "b"]},
])
def test_ohne_brauchbaren_inhalt_ist_es_ein_anfragefehler(monkeypatch, written, payload):
    r = _post(monkeypatch, payload)

    assert r.status_code == 400
    assert "content" in r.json()["error"]
    assert written == []


def test_ohne_dienstschluessel_kein_schreibweg(monkeypatch, written):
    client = _client(monkeypatch)
    r = client.post("/write", json={"content": FAKT, "category": "session"})

    assert r.status_code == 401
    assert written == []
