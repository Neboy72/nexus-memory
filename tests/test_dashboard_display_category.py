"""Wache: das Dashboard zeigt kein "uncategorized" mehr an.

Rund 4.297 von 48.753 Eintraegen haben kein category-Feld. Das sind keine
unfertigen Eintraege, sondern zwei klar unterscheidbare Gruppen: gescannte
Dokumente (Paperless: doc_id/title/correspondent) und alte Freitext-Notizen
aus der frueheren Sammlung openclaw-memory (type=turn/memory/index). Das Etikett
wird nur fuer die Anzeige abgeleitet — die Rohdaten in Qdrant bleiben unberuehrt.

Kein Qdrant noetig: die Tests rufen die Hilfsfunktion direkt bzw. setzen den
Scroll-Helfer des Dashboards aus.
"""

import os
import sys

import pytest

# dashboard/server.py beendet beim Import den Prozess, wenn FastAPI fehlt.
pytest.importorskip("fastapi", reason="dashboard-Server braucht fastapi")
pytest.importorskip("uvicorn", reason="dashboard-Server braucht uvicorn")

DASH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dashboard"
)
if DASH not in sys.path:
    sys.path.insert(0, DASH)

import server  # noqa: E402

PAPERLESS = {
    "source": "paperless",
    "doc_id": 4711,
    "title": "Rechnung 2026-08",
    "document_type": "Rechnung",
    "correspondent": "Stadtwerke",
    "url": "http://paperless.local/documents/4711",
}
OPENCLAW_NOTE = {
    "type": "turn",
    "_source_collection": "openclaw-memory",
    "content": "freitext notiz",
}


def test_explicit_category_is_passed_through_unchanged():
    assert server._display_category({"category": "fact", "text": "x"}) == "fact"
    assert server._display_category({"category": "rule"}) == "rule"


def test_document_payload_becomes_document():
    assert server._display_category(PAPERLESS) == "document"
    # Einzeln reicht ebenfalls: Kennung, source oder url+source.
    assert server._display_category({"doc_id": 1}) == "document"
    assert server._display_category({"document_id": "abc"}) == "document"
    assert server._display_category({"document_type": "Vertrag"}) == "document"
    assert server._display_category({"source": "paperless"}) == "document"
    assert server._display_category({"url": "http://x", "source": "wiki"}) == "document"
    # url ohne source ist KEINE Dokumentkennung.
    assert server._display_category({"url": "http://x"}) == "other"


def test_note_payload_becomes_note():
    assert server._display_category(OPENCLAW_NOTE) == "note"
    assert server._display_category({"type": "memory"}) == "note"
    assert server._display_category({"type": "index"}) == "note"
    assert server._display_category({"_source_collection": "openclaw-memory"}) == "note"


def test_empty_payload_becomes_other():
    assert server._display_category({}) == "other"
    assert server._display_category({"text": "ohne alles"}) == "other"
    # Leere Kategorie zaehlt wie keine Kategorie.
    assert server._display_category({"category": "", "text": "x"}) == "other"
    assert server._display_category({"category": "   "}) == "other"


def test_extract_memory_fields_uses_the_same_rule():
    """Die Anzeige nimmt die Felder aus _extract_memory_fields — dasselbe Ergebnis."""
    cases = [
        ({"category": "fact", "text": "x"}, "fact"),
        (PAPERLESS, "document"),
        (OPENCLAW_NOTE, "note"),
        ({}, "other"),
    ]
    for payload, expected in cases:
        assert server._extract_memory_fields(payload)["category"] == expected


def _point(idx, payload):
    return {"id": f"probe-{idx}", "payload": payload}


def test_both_display_paths_agree_and_show_no_uncategorized(monkeypatch):
    """/api/graph und /api/memories sind die zwei Anzeigestellen — beide muessen
    document/note zeigen, nicht uncategorized."""
    points = [
        _point(0, {"category": "fact", "text": "fakt"}),
        _point(1, dict(PAPERLESS)),
        _point(2, dict(OPENCLAW_NOTE)),
    ]
    monkeypatch.setattr(server, "_scroll_all_memories", lambda *a, **k: points)

    from starlette.testclient import TestClient

    client = TestClient(server.app)

    graph = client.get("/api/graph").json()
    assert "uncategorized" not in graph["categories"]
    assert graph["categories"] == {"fact": 1, "document": 1, "note": 1}

    memories = client.get("/api/memories").json()
    seen = {m["category"] for m in memories["memories"]}
    assert seen == {"fact", "document", "note"}
    assert "uncategorized" not in seen
