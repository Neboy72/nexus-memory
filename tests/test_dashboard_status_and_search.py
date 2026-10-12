"""Wachen fuer die vier Dashboard-Defekte vom 12.10.2026.

1. Die Such-Route fehlte ganz — das Frontend rief einen Pfad, den es nicht gab.
2. "Qdrant Offline" hiess auch dann so, wenn nur die Sammlung fehlte.
3. Die Provider-Anzeige ueberging die Umgebungsvariable, die wirklich gilt.
4. Der Modellname wurde auf dem Event-Loop gebaut und blockierte alles.
"""

import os
import sys

import pytest

# dashboard/server.py beendet beim Import den Prozess, wenn FastAPI fehlt
# (`sys.exit(1)`), und die Testumgebung der CI bringt FastAPI nicht mit. Ohne
# diesen Riegel reisst der Import die GANZE Suite ab (INTERNALERROR statt Skip).
pytest.importorskip("fastapi", reason="dashboard-Server braucht fastapi")
pytest.importorskip("uvicorn", reason="dashboard-Server braucht uvicorn")

DASH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dashboard"
)
if DASH not in sys.path:
    sys.path.insert(0, DASH)

import server  # noqa: E402


# ── 1. Such-Route ────────────────────────────────────────────────────────────

def test_search_route_exists_and_returns_scored_hits(monkeypatch):
    """Der Pfad, den die WebUI ruft, muss existieren UND Treffer liefern."""
    monkeypatch.setattr(server, "_service_embed", lambda text: [0.1] * 1024)
    monkeypatch.setattr(server, "_named_vector", lambda: "probe__vector__1024")

    async def _fake_qdrant(path, body=None):
        assert "points/search" in path
        assert body["vector"]["name"] == "probe__vector__1024"
        return {"result": [{"id": "p1", "score": 0.91,
                            "payload": {"text": "Probe-Treffer", "category": "fact",
                                        "access_level": "private"}}]}

    monkeypatch.setattr(server, "_qdrant_request_async", _fake_qdrant)

    from starlette.testclient import TestClient

    resp = TestClient(server.app).get("/api/memories/search", params={"q": "probe", "limit": 3})
    assert resp.status_code == 200, "die Route muss es geben"
    body = resp.json()
    assert body["count"] == 1
    assert body["memories"][0]["text"] == "Probe-Treffer"
    assert body["memories"][0]["score"] == 0.91


def test_search_fails_loud_and_never_pretends_empty(monkeypatch):
    """Kein Embedding = gemeldeter Fehler, nicht 'nichts gefunden'."""
    def _boom(text):
        raise RuntimeError("Dienst nicht erreichbar")

    monkeypatch.setattr(server, "_service_embed", _boom)
    from starlette.testclient import TestClient

    body = TestClient(server.app).get("/api/memories/search", params={"q": "probe"}).json()
    assert body["count"] == 0
    assert "embedding unavailable" in body["error"]


# ── 2. Zwei Fehler, zwei Namen ───────────────────────────────────────────────

def test_store_state_separates_the_two_faults():
    assert server._store_state(True, True) == "ok"
    assert server._store_state(False, True) == "collection_missing"
    assert server._store_state(False, False) == "qdrant_unreachable"


# ── 3. Der Provider, der wirklich gilt ───────────────────────────────────────

def test_environment_variable_wins_over_a_stray_cloud_key(monkeypatch):
    """Genau der Defekt: eine gesetzte Env-Variable wurde ignoriert, ein
    herumliegender Schluessel gewann und die Karte nannte den falschen Dienst."""
    monkeypatch.setenv("NEXUS_EMBEDDING_PROVIDER", "sentence-transformers")
    monkeypatch.setenv("VOYAGE_API_KEY", "nur-ein-alter-schluessel")
    assert server._detect_embed_provider() == "sentence-transformers"


def test_empty_environment_variable_does_not_hijack_the_choice(monkeypatch):
    monkeypatch.setenv("NEXUS_EMBEDDING_PROVIDER", "   ")
    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
    provider = server._detect_embed_provider()
    assert provider != "   ", "Leerraum darf nicht als Provider durchgehen"
    assert provider != ""
