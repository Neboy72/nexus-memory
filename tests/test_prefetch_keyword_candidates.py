"""Tests fuer die Stichwortkandidaten und die Umsortierung der Einspielung.

Der Fall, der das ausgeloest hat (gemessen 10.11.2026): Frage "Heizöl-Lieferant"
holte den Fakt "Heizöl-Bestellung ... bei Marsolek Brennstoffe" nur auf Platz 58
von 64 Kandidaten. Reine Aehnlichkeit verliert gegen kurze Etiketten; die
Stichwortsuche neben der Aehnlichkeit holt die Antwort in den Kandidatenkreis.

Ausserdem: Das alte Wortmuster zerschnitt deutsche Woerter am Umlaut
("Heizöl" -> "heiz"), und "heiz" trifft dann alles, was mit Heizung zu tun hat.
"""

from types import SimpleNamespace

from plugins.memory.nexus import (
    _fold_umlauts,
    _keyword_boost_order,
    _keyword_candidate_points,
    _query_tokens,
)


class _FakeQdrant:
    def __init__(self, records):
        self.records = records
        self.calls = []

    def scroll(self, **kwargs):
        self.calls.append(kwargs)
        return self.records, None


class TestQueryTokens:
    def test_umlaut_bleibt_in_der_wortmarke(self):
        assert _query_tokens("Heizöl-Lieferant") == ["heizöl", "lieferant"]

    def test_stoppwoerter_fallen_weg(self):
        assert _query_tokens("Welche Farbe hat der Schalter") == ["farbe", "schalter"]

    def test_kurze_woerter_fallen_weg(self):
        assert _query_tokens("IP vom NAS") == []


class TestFoldUmlauts:
    def test_schreibweise_wird_gleich(self):
        assert _fold_umlauts("Heizöl") == _fold_umlauts("Heizoel")

    def test_kleinschreibung(self):
        assert _fold_umlauts("Marsolek") == "marsolek"


class TestKeywordCandidates:
    def test_holt_punkte_und_gibt_ihnen_den_sockel_score(self):
        rec = SimpleNamespace(id="abc", payload={"content": "Heizöl bestellt"})
        fake = _FakeQdrant([rec])
        pts = _keyword_candidate_points(fake, "nexus_hf", "Heizöl-Lieferant", 0.632)
        assert len(pts) == 1
        assert pts[0].id == "abc"
        assert pts[0].score == 0.632
        assert pts[0].payload["content"] == "Heizöl bestellt"

    def test_sucht_im_inhalt_des_punktes(self):
        fake = _FakeQdrant([])
        _keyword_candidate_points(fake, "nexus_hf", "Heizöl-Lieferant", 0.5)
        flt = fake.calls[0]["scroll_filter"]
        keys = {c.key for c in flt.should}
        assert keys == {"content"}

    def test_ohne_tragbares_wort_keine_abfrage(self):
        fake = _FakeQdrant([])
        assert _keyword_candidate_points(fake, "nexus_hf", "ok und so", 0.5) == []
        assert fake.calls == []


class TestKeywordBoost:
    @staticmethod
    def _point(content, score, terms=""):
        return SimpleNamespace(payload={"content": content, "search_terms": terms}, score=score)

    def test_worttreffer_vor_hoeherem_score(self):
        aehnlich = self._point("concept: LPG (gas) und weitere Angaben", 0.80)
        treffer = self._point("Heizöl-Bestellung bei Marsolek Brennstoffe", 0.55)
        assert _keyword_boost_order([aehnlich, treffer], "Heizöl-Lieferant")[0] is treffer

    def test_suchvarianten_zaehlen_als_treffer(self):
        # Der Text traegt das Wort nicht, das Feld search_terms schon.
        punkt = self._point("organization: Marsolek (type=Oellieferant, Eltville)", 0.55,
                            terms="lieferant")
        ohne = self._point("concept: LPG (gas) und weitere Angaben", 0.80)
        assert _keyword_boost_order([ohne, punkt], "Heizöl-Lieferant")[0] is punkt

    def test_umlautschreibweise_trifft(self):
        punkt = self._point("Heizoel Lieferung 2000 Liter", 0.50)
        ohne = self._point("concept: LPG (gas) und weitere Angaben", 0.80)
        assert _keyword_boost_order([ohne, punkt], "Heizöl")[0] is punkt
