"""Wache: der Graph bekommt einen QUERSCHNITT, nicht die erste Seite.

Die Graph-Seite holt ihre Knoten ueber /api/memories (nicht ueber /api/graph).
Ohne Filter las der Endpunkt nur die erste Seite — bei ~75 % Fakten im Bestand
enthielt die praktisch nur Fakten, und der Graph malte zwei Farben statt
fuenfzehn. Zusaetzlich spiegelte die Legende die gekuerzte Auswahl statt der
echten Verteilung.
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


def _point(idx, category):
    return {
        "id": f"probe-{idx}",
        "payload": {"text": f"probe {idx}", "category": category, "access_level": "private"},
    }


def test_first_page_is_not_used_as_the_whole_store(monkeypatch):
    """Es muss die volle Sammlung gelesen werden — sonst fehlen seltene Kategorien."""
    seen = {}

    def _fake_scroll(cap=5000):
        seen["cap"] = cap
        return [_point(i, "fact") for i in range(1000)]

    monkeypatch.setattr(server, "_scroll_all_memories", _fake_scroll)
    from starlette.testclient import TestClient

    TestClient(server.app).get("/api/memories", params={"limit": 50})
    assert seen.get("cap", 0) >= 500_000, (
        "nur die erste Seite zu lesen ist kein Querschnitt — rare Kategorien fehlen dann"
    )


def test_a_rare_category_reaches_the_graph(monkeypatch):
    points = [_point(i, "fact") for i in range(1000)]
    points += [_point(10_000 + i, "session") for i in range(50)]
    monkeypatch.setattr(server, "_scroll_all_memories", lambda *a, **k: points)
    from starlette.testclient import TestClient

    body = TestClient(server.app).get("/api/memories", params={"limit": 100}).json()
    cats = {m["category"] for m in body["memories"]}
    assert "session" in cats, "die seltene Kategorie muss im Querschnitt auftauchen"


def test_legend_counts_the_whole_store_not_the_slice(monkeypatch):
    points = [_point(i, "fact") for i in range(1000)]
    points += [_point(10_000 + i, "session") for i in range(50)]
    monkeypatch.setattr(server, "_scroll_all_memories", lambda *a, **k: points)
    from starlette.testclient import TestClient

    body = TestClient(server.app).get("/api/memories", params={"limit": 20}).json()
    assert body["category_counts"]["fact"] == 1000
    assert body["category_counts"]["session"] == 50
    assert len(body["memories"]) == 20
