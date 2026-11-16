"""Die Ausgabegrenze steuert die Menge, nicht die Trefferquote (16.11.2026).

Gemessen am laufenden Dienst: Ein Eintrag, dessen Text woertlich abgefragt
wurde, war bei Grenze 3, 5 und 6 ueberhaupt nicht dabei (null Treffer), bei
Grenze 8 einmal, bei 20 schliesslich auf Platz 1. Die Grenze hat also nicht
die Ausgabemenge bestimmt, sondern ob man den richtigen Eintrag ueberhaupt
findet.

Ursache, gemessen und nicht vermutet: Der Kandidatenpool wurde aus der
Ausgabegrenze berechnet (limit*2 im Dienst, top_k*2 im Retriever). Bei kleiner
Grenze war das Fenster mit Eintraegen gefuellt, die der Aufrufer nicht sehen
darf — von 40 Kandidaten einer Probe trugen 31 den Geltungsbereich einer
fremden Sitzung. Der gesuchte Eintrag stand auf Rang 25, also hinter lauter
unsichtbaren, und fiel aus dem Fenster. Nach dem Aussieben blieb nichts
uebrig — und ein leerer Abruf sieht aus wie "gibt es nicht".

Was hier festgehalten wird:

1. Der Pool hat eine Untergrenze, die nicht von der Ausgabegrenze abhaengt —
   an beiden Stellen (Retriever und Dienst).
2. Die Praefix-Invariante: Das Ergebnis fuer eine kleine Grenze ist der Anfang
   des Ergebnisses fuer eine grosse. Formal die Fassung von "die Grenze
   steuert nur die Ausgabemenge".
"""

import pytest

import nexus.retrieval as retrieval
from nexus.retrieval import POOL_FLOOR, HybridRetriever


def _retriever(bm25_hits, vector_hits):
    """Instanz ohne Qdrant; die beiden Suchen werden ersetzt."""
    r = object.__new__(HybridRetriever)
    r._ids = []
    r._texts = []
    r._texts_raw = []
    r._vector_name = None
    r._chunk_graph = {}
    r._chunk_text_lookup = {}
    r._entity_index = {}
    r._skillgraph = None
    # Abdeckungsregel still halten: ohne Bestandswissen feuert sie nicht.
    r._df = None
    r._doc_count = 0
    r._suppressed_ids = set()
    r.search_bm25 = lambda query, top_k=10: list(bm25_hits)[:top_k]
    r.search_vector = lambda query_vector, top_k=10: list(vector_hits)[:top_k]
    r._time_decay = lambda ranked: ranked
    r._tier_boost = lambda ranked: ranked
    return r


def _bm25(n, offset=0):
    return [{"id": f"b{i:03d}", "rank": i + 1, "method": "bm25",
             "bm25_score": 20.0 - i * 0.1, "text": f"b{i}"} for i in range(offset, offset + n)]


def _vec(n, offset=0):
    return [{"id": f"v{i:03d}", "rank": i + 1, "method": "vector",
             "score": 0.9 - i * 0.001, "text": f"v{i}"} for i in range(offset, offset + n)]


# ---------- Pool-Untergrenze ----------

def test_pool_floor_is_a_real_floor():
    """Die Zahl ist eine Untergrenze, keine Verhaeltniszahl."""
    assert POOL_FLOOR >= 50


def test_retriever_pool_does_not_shrink_with_small_limit():
    """Bei kleiner Ausgabegrenze muss der Retriever trotzdem den ganzen Pool sehen.

    Ohne Boden bekaeme search_bm25 bei top_k=3 nur 6 Kandidaten zu sehen — und
    damit eine andere Grundgesamtheit als bei top_k=20.
    """
    gesehen = {}

    def fake_bm25(query, top_k=10):
        gesehen.setdefault("bm25", []).append(top_k)
        return _bm25(top_k)

    def fake_vec(query_vector, top_k=10):
        gesehen.setdefault("vec", []).append(top_k)
        return _vec(top_k)

    r = _retriever([], [])
    r.search_bm25 = fake_bm25
    r.search_vector = fake_vec

    r.search_hybrid("irgendeine frage", query_vector=[0.1] * 4, top_k=3)
    r.search_hybrid("irgendeine frage", query_vector=[0.1] * 4, top_k=20)

    assert gesehen["bm25"] == [POOL_FLOOR, POOL_FLOOR], gesehen
    assert gesehen["vec"] == [POOL_FLOOR, POOL_FLOOR], gesehen


# ---------- Praefix-Invariante ----------

@pytest.mark.parametrize("klein", [1, 2, 3, 5, 6, 8, 10])
def test_small_limit_is_prefix_of_large_limit(klein):
    """Das Ergebnis fuer eine kleine Grenze ist der Anfang des grossen.

    Das ist die formale Fassung von "die Grenze steuert nur die Ausgabemenge":
    Wer drei Treffer anfordert, bekommt dieselben drei, die auch an erster
    Stelle stehen, wenn er zwanzig anfordert. Vorher verschob sich mit der
    Grenze auch die Reihenfolge.
    """
    r = _retriever(_bm25(60), _vec(60))
    gross = r.search_hybrid("frage", query_vector=[0.1] * 4, top_k=20)
    klein_res = r.search_hybrid("frage", query_vector=[0.1] * 4, top_k=klein)

    assert [h["id"] for h in klein_res] == [h["id"] for h in gross[:klein]]


def test_limit_never_yields_more_than_asked():
    r = _retriever(_bm25(60), _vec(60))
    for k in (1, 3, 5, 20):
        assert len(r.search_hybrid("frage", query_vector=[0.1] * 4, top_k=k)) == k


def test_large_limit_does_not_invent_results():
    """Mehr anfordern als da ist, liefert nicht mehr als da ist."""
    r = _retriever(_bm25(4), _vec(3))
    res = r.search_hybrid("frage", query_vector=[0.1] * 4, top_k=50)
    assert 0 < len(res) <= 7


def test_identifier_query_still_skips_vectors():
    """Die Kennungs-Regel aus 0.22.52 bleibt unberuehrt."""
    r = _retriever(_bm25(20), _vec(20))
    ruf = []
    r.search_vector = lambda query_vector, top_k=10: (ruf.append(top_k), _vec(20))[1]
    r.search_hybrid("9122", query_vector=[0.1] * 4, top_k=5)
    assert ruf == [], "Bei einer Kennung darf die Vektorsuche nicht laufen"


# ---------- Der Dienst reicht den groesseren Pool weiter ----------

def test_service_pool_floor_constant():
    """Der Dienst benutzt denselben Boden wie der Retriever (eigene Konstante)."""
    import nexus_memory.mcp_server as ms

    assert ms.HYBRID_POOL_FLOOR >= 50


def test_service_pool_beats_the_old_limit_times_two():
    """Die alte Regel war limit*2. Bei Grenze 3 waren das 6 Kandidaten."""
    import nexus_memory.mcp_server as ms

    for limit in (3, 5, 8, 10, 20):
        assert max(ms.HYBRID_POOL_FLOOR, limit * 4) > limit * 2
