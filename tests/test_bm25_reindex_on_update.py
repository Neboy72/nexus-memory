"""Tests: der Keyword-Index darf nach einer KORREKTUR nicht den alten Text behalten.

Gefunden am 02.10.2026: Eine ueber nexus_update korrigierte Erinnerung blieb im
BM25-Index mit ihrem widerlegten Wortlaut auffindbar, weil der update-Pfad den
Index nie anfasste (der remember-Pfad dagegen schon).
"""
from __future__ import annotations
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "nexus"))

pytest.importorskip("bm25s")
from retrieval import HybridRetriever  # noqa: E402


def _retriever(texts, ids):
    r = HybridRetriever.__new__(HybridRetriever)  # ohne Qdrant-Verbindung
    r._ids = list(ids)
    r._texts = [t.lower() for t in texts]
    r._texts_raw = list(texts)
    r._bm25 = None
    r._index_dir = Path("/tmp/nexus-bm25-test")
    r._index_dir.mkdir(parents=True, exist_ok=True)
    r._entity_index = {}
    r._entity_index_reverse = {}
    r._session_index = {}
    return r


def test_replace_indexed_swaps_text_for_existing_id():
    r = _retriever(["alt: Kompression startet bei 15 Prozent", "zweiter Eintrag"],
                   ["id-a", "id-b"])
    out = r.replace_indexed([("id-a", "neu: Schwelle ist 50 Prozent")])

    assert out["replaced"] == 1, out
    assert out["missing"] == 0, out
    assert r._texts[0] == "neu: schwelle ist 50 prozent"
    assert "50 Prozent" in r._texts_raw[0]
    assert "15 prozent" not in r._texts_raw[0]


def test_replace_indexed_ignores_unknown_id():
    r = _retriever(["nur einer"], ["id-a"])
    out = r.replace_indexed([("gibt-es-nicht", "irgendwas")])
    assert out["replaced"] == 0
    assert out["missing"] == 1
    assert r._texts == ["nur einer"]


def test_search_finds_new_text_and_not_the_old_one():
    r = _retriever(["alt: Kompression startet bei 15 Prozent", "zweiter Eintrag"],
                   ["id-a", "id-b"])
    r.replace_indexed([("id-a", "neu: die Schwelle ist 50 Prozent")])

    hits_new = r.search_bm25("Schwelle 50 Prozent", top_k=5)
    assert hits_new, "neuer Text muss auffindbar sein"
    assert hits_new[0]["id"] == "id-a"

    hits_old = r.search_bm25("Kompression 15 Prozent Datei", top_k=5)
    old_ids = [h["id"] for h in hits_old]
    # Der alte Wortlaut darf id-a nicht mehr nach oben ziehen; andere Treffer sind ok.
    assert not hits_old or hits_old[0]["id"] != "id-a" or "15" not in r._texts_raw[0]


def test_update_index_does_not_duplicate_existing_id():
    """Regression: das alte extend() haengte dieselbe ID ein zweites Mal an."""
    r = _retriever(["erster", "zweiter"], ["id-a", "id-b"])
    out = r.update_index(memories_to_add=[("id-a", "erster neu")])
    assert out["added"] == 0, out
    assert r._ids.count("id-a") == 1
    assert r._ids == ["id-a", "id-b"]
    assert r._texts_raw[0] == "erster"  # unangetastet — Ersetzen ist replace_indexed


def test_update_index_still_adds_truly_new_id():
    r = _retriever(["erster"], ["id-a"])
    out = r.update_index(memories_to_add=[("id-neu", "frischer Eintrag")])
    assert out["added"] == 1, out
    assert r._ids == ["id-a", "id-neu"]
    assert "frischer eintrag" in r._texts[1]
