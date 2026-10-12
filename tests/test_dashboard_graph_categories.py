"""Wache: die Graph-Ansicht zaehlt JEDE Kategorie, nicht nur die erste Seite.

Eine einzelne Scroll-Seite ist ein beliebiger Ausschnitt, keine Stichprobe. Bei
~75 % Fakten im Bestand tauchen die seltenen Kategorien (session, belief,
preference) darin nie auf — der Graph sah dann aus, als gaebe es nur Fakten.
Diese Wache schiebt eine bekannte Verteilung durch den Scroll-Helfer und
verlangt, dass jede Kategorie bis in die Antwort durchkommt.
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
        "payload": {
            "text": f"probe {idx}",
            "category": category,
            "access_level": "private",
        },
    }


def _distribution():
    # 500 Fakten fuellen eine einzelne erste Seite komplett aus und verdraengen
    # jede andere Kategorie — genau der gemeldete Defekt.
    points = [_point(i, "fact") for i in range(500)]
    points += [_point(500 + i, "session") for i in range(7)]
    points += [_point(600 + i, "belief") for i in range(3)]
    return points


def test_graph_counts_every_category_from_the_full_scroll(monkeypatch):
    monkeypatch.setattr(server, "_scroll_all_memories", lambda *a, **k: _distribution())

    from starlette.testclient import TestClient

    resp = TestClient(server.app).get("/api/graph")
    assert resp.status_code == 200
    body = resp.json()

    assert body["categories"]["fact"] == 500
    assert body["categories"]["session"] == 7
    assert body["categories"]["belief"] == 3
    assert sum(body["categories"].values()) == 510

    labels = {n["label"] for n in body["nodes"] if n.get("category") == "meta"}
    assert {"fact", "session", "belief"} <= labels


def test_a_tiny_category_is_never_crowded_out(monkeypatch):
    points = [_point(i, "fact") for i in range(500)]
    points += [_point(1000 + i, "belief") for i in range(2)]
    monkeypatch.setattr(server, "_scroll_all_memories", lambda *a, **k: points)

    from starlette.testclient import TestClient

    body = TestClient(server.app).get("/api/graph").json()
    belief_nodes = [n for n in body["nodes"] if n.get("category") == "belief"]
    assert len(belief_nodes) == 2
