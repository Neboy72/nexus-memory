"""Alte numerische Punkt-Kennungen im Filter: als Zahl, nicht als Text.

Befund 25.11.2026: Im Bestand liegen Punkte mit rein numerischer Kennung
(``"642541819651052"``). Qdrant nimmt im ``HasIdCondition`` eine UUID als Text
oder eine Ganzzahl — eine Ziffernfolge als TEXT quittiert der Server mit
``400 Format error in JSON body``. Davon betroffen waren 11 von 20
Graph-Abrufen: der ganze Stapel galt als unsichtbar, der Graph-Zusatz fiel aus.

Geprueft:
1. Der Helfer wandelt nur reine Ziffernfolgen um (und nur, wenn sie in 64 Bit
   passen); UUIDs und Zahlen bleiben, wie sie sind.
2. ``GraphTraversal._fetch_payloads`` gibt dem Client die Zahl, nicht den Text.
3. ``EdgeStore._scroll_point`` (dieselbe Bauart, eine Kennung) tut dasselbe.

Kein Qdrant, kein Netz: die Clients sind Attrappen, die den Filter mitschreiben.
"""
from __future__ import annotations

from types import SimpleNamespace

from nexus.graph.store import EdgeStore, point_id_for_filter
from nexus.graph.traversal import GraphTraversal

ZAHL = "642541819651052"
UUID = "1eb7ca53-a603-5e40-8c88-fef5b1d2a6ac"


class _Client:
    """Schreibt mit, was im Filter landet, und liefert nichts zurueck."""

    def __init__(self):
        self.gesehen = None

    def scroll(self, **kwargs):
        flt = kwargs.get("scroll_filter")
        bedingung = flt.must[0] if flt is not None and getattr(flt, "must", None) else None
        self.gesehen = list(getattr(bedingung, "has_id", []) or [])
        return [], None


def test_helfer_wandelt_nur_ziffernfolgen():
    assert point_id_for_filter(ZAHL) == 642541819651052
    assert isinstance(point_id_for_filter(ZAHL), int)
    assert point_id_for_filter(UUID) == UUID
    assert point_id_for_filter("  " + ZAHL + " ") == 642541819651052
    assert point_id_for_filter(42) == 42
    # zu gross fuer 64 Bit: bleibt Text, damit nichts still umgedeutet wird
    assert point_id_for_filter("9" * 25) == "9" * 25


def test_fetch_payloads_uebergibt_die_zahl():
    client = _Client()
    graph = SimpleNamespace(store=SimpleNamespace(client=client, _collection="c"))
    gt = GraphTraversal(graph, access_level="private")
    gt._fetch_payloads([ZAHL, UUID])
    assert client.gesehen == [642541819651052, UUID], (
        "numerische Kennung muss als Zahl in den Filter; alte Fassung schickte "
        "den Text und bekam HTTP 400"
    )


def test_scroll_point_uebergibt_die_zahl():
    client = _Client()
    store = EdgeStore.__new__(EdgeStore)
    store.client = client
    store._collection = "c"
    store._scroll_point(ZAHL)
    assert client.gesehen == [642541819651052], (
        "auch die Einzelabfrage darf die Ziffernfolge nicht als Text schicken"
    )
