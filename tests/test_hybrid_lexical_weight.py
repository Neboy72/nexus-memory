"""Woertliche Treffer bei wortgleichen Fragen (16.11.2026).

Gemessen: Ein Eintrag, den ein Agent 20 Minuten zuvor geschrieben hatte, stand
bei wortgleicher Frage auf Platz 2 — hinter einem Eintrag ohne inhaltlichen
Bezug. Ursache war nicht die Suche selbst: woertlich war der gesuchte Eintrag
Platz 1 (36.4 gegen 12.5), im Vektor aber nicht unter den ersten zwanzig.
Beide bekamen dadurch denselben Fusionswert, und im Gleichstand entschied die
Reihenfolge der Listen.

Diese Tests halten fest, was jetzt gilt: ein klarer woertlicher Vorsprung
zaehlt mehr, und bei Gleichstand gewinnt der staerkere woertliche Beleg.
"""

import pytest

from nexus.retrieval import (
    LEXICAL_CLEAR_RATIO,
    LEXICAL_WEIGHT_STRONG,
    RRF_K,
    HybridRetriever,
)


def _retriever():
    """Instanz ohne Qdrant — _rrf und _lexical_weight brauchen nur _ids/_texts."""
    r = object.__new__(HybridRetriever)
    r._ids = []
    r._texts = []
    return r


def _bm25(doc_id, rank, score):
    return {"id": doc_id, "rank": rank, "method": "bm25", "bm25_score": score,
            "text": f"text {doc_id}"}


def _vec(doc_id, rank, score=0.9):
    return {"id": doc_id, "rank": rank, "method": "vector", "score": score,
            "text": f"text {doc_id}"}


# ---------- Gewicht der woertlichen Seite ----------

def test_clear_lexical_lead_raises_weight():
    r = _retriever()
    hits = [_bm25("a", 1, 36.4), _bm25("b", 2, 12.5)]
    assert 36.4 / 12.5 >= LEXICAL_CLEAR_RATIO
    assert r._lexical_weight(hits) == LEXICAL_WEIGHT_STRONG


def test_close_lexical_scores_keep_equal_weight():
    r = _retriever()
    hits = [_bm25("a", 1, 10.0), _bm25("b", 2, 9.0)]
    assert r._lexical_weight(hits) == 1.0


def test_no_lexical_hits_keep_equal_weight():
    r = _retriever()
    assert r._lexical_weight([]) == 1.0
    assert r._lexical_weight([_bm25("a", 1, 0.0)]) == 1.0


def test_single_lexical_hit_counts_as_clear():
    r = _retriever()
    assert r._lexical_weight([_bm25("a", 1, 5.0)]) == LEXICAL_WEIGHT_STRONG


# ---------- Fusion ----------

def test_clear_lexical_lead_wins_against_vector_leader():
    """Woertlich Platz 1 gegen Vektor Platz 1: bei klarem Vorsprung gewinnt der
    woertliche Treffer, auch wenn er im Vektor weiter hinten steht."""
    r = _retriever()
    bm25 = [_bm25("gesucht", 1, 36.4), _bm25("zwei", 2, 12.5)]
    vec = [_vec("fremd", 1), _vec("gesucht", 2)]
    fused = r._rrf(bm25, vec)
    assert fused[0]["id"] == "gesucht"


def test_exact_tie_prefers_the_lexical_hit():
    """Enger woertlicher Abstand (kein Bonus): beide bekommen denselben
    Fusionswert, dann entscheidet der hoehere BM25-Score."""
    r = _retriever()
    bm25 = [_bm25("gesucht", 1, 10.0), _bm25("zwei", 2, 9.5)]
    vec = [_vec("fremd", 1)]
    assert r._lexical_weight(bm25) == 1.0

    fused = r._rrf(bm25, vec)
    scores = {item["id"]: item["rrf_score"] for item in fused}
    assert scores["gesucht"] == pytest.approx(scores["fremd"], rel=1e-9)
    assert fused[0]["id"] == "gesucht"


def test_semantic_question_is_left_alone():
    """Ohne woertlichen Vorsprung bleibt die Fusion wie vorher: der Vektor-
    Erste und der woertliche Erste stehen gleichauf, nichts wird bevorzugt."""
    r = _retriever()
    bm25 = [_bm25("woertlich", 1, 10.0), _bm25("zwei", 2, 9.5)]
    vec = [_vec("vektor", 1)]
    assert r._lexical_weight(bm25) == 1.0

    fused = r._rrf(bm25, vec)
    scores = {item["id"]: item["rrf_score"] for item in fused}
    assert scores["woertlich"] == scores["vektor"]


def test_rrf_constant_is_the_documented_one():
    assert RRF_K == 60
