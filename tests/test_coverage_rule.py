"""Woertliches Zitat gewinnt gegen einen mittelmaessigen Doppeltreffer (16.11.2026).

Gemessen am laufenden Dienst: Bei einer Abfrage, die den Text eines Eintrags
woertlich zitiert, standen zwei Eintraege ueber die Ollama-Ueberwachung ganz
oben — sie haben inhaltlich nichts mit der Abfrage zu tun. Sie stehen in BEIDEN
Trefferlisten (Wortlaut und Bedeutung), der zitierte Eintrag nur in einer. Die
Rangfusion belohnt das doppelt: Wer in beiden Listen mittelmaessig steht, schlaegt
den einseitigen Spitzenreiter.

Die Rangfusion selbst bleibt unangetastet. Stattdessen ein AUSLOESER: Deckt genau
ein sichtbarer Eintrag die Abfrage vollstaendig ab, gehoert er auf Platz 1. Sonst
aendert sich nichts — deshalb bleiben umformulierte Fragen unberuehrt.

Zwei Feinheiten, die beim Bauen aufgefallen sind und hier festgehalten werden:

1. Schreibvarianten werden vorher geglaettet (Eszett, Umlaute, Gross-/
   Kleinschreibung, Satzzeichen). "scheiss" und "scheiß" sind derselbe Term.
   Das ist keine Tippfehler-Toleranz: ein echter Tippfehler deckt nicht ab.
2. Ein zurueckgezogener Zwilling mit demselben Text liesse die Regel nie
   feuern, weil sie dann zwei Volltreffer findet. Zurueckgezogene Eintraege
   bleiben im Index (die Zeitpunkt-Abfrage braucht sie), zaehlen hier aber
   nicht als sichtbar.
"""

import pytest

from nexus.retrieval import (
    COVERAGE_MIN_RARE_TERMS,
    COVERAGE_RARE_DF_RATIO,
    HybridRetriever,
    _coverage_normalize,
    _coverage_terms,
)


# ---------- Normalisierung ----------

def test_scheiss_and_scheiss_are_the_same_term():
    """Der Fall, der die ganze Arbeit ausgeloest hat."""
    assert _coverage_terms("scheiss") == _coverage_terms("scheiß")


def test_umlauts_are_folded():
    assert _coverage_terms("überwacht") == _coverage_terms("ueberwacht")
    assert _coverage_terms("läuft") == _coverage_terms("laeuft")


def test_case_and_punctuation_do_not_matter():
    a = _coverage_terms("Du hast den Kessel drin, oder?")
    b = _coverage_terms("du hast den kessel drin oder")
    assert a == b


def test_a_real_typo_is_not_covered():
    """Tippfehler-Toleranz ist ausdruecklich NICHT Teil der Regel."""
    assert _coverage_terms("kessi") != _coverage_terms("kessel")


def test_normalisation_is_deterministic_and_idempotent():
    einmal = _coverage_normalize("Grüße, Straße!")
    zweimal = _coverage_normalize(einmal)
    assert einmal == zweimal


# ---------- Die Regel ----------

ZITAT = "du hast den scheiss kessel immer noch drin aber immerhin ist es jetzt ordentlich"


def _retriever(df, doc_count, suppressed=None):
    r = object.__new__(HybridRetriever)
    r._ids = []
    r._texts = []
    r._texts_raw = []
    r._df = df
    r._doc_count = doc_count
    r._suppressed_ids = suppressed or set()
    return r


def _hit(doc_id, text):
    return {"id": doc_id, "text": text, "rrf_score": 0.01, "method": "bm25"}


def _seltener_bestand():
    """Bestand, in dem die Fuellwoerter haeufig und die Inhaltswoerter selten sind."""
    df = {"du": 800, "hast": 700, "den": 900, "aber": 850, "ist": 950, "es": 900, "jetzt": 600,
          "noch": 500, "scheiss": 3, "kessel": 2, "immer": 120, "drin": 40, "immerhin": 9,
          "ordentlich": 7}
    return df, 1000


def test_unique_full_cover_wins():
    df, n = _seltener_bestand()
    r = _retriever(df, n)
    treffer = [
        _hit("konkurrent-a", "der agent ueberwacht still die ollama nutzung fuer nebo"),
        _hit("ziel", "Du hast den scheiß Kessel immer noch drin, aber immerhin ist es jetzt ordentlich"),
        _hit("konkurrent-b", "der agent laeuft als kiosha und ueberwacht die ollama nutzung"),
    ]
    res = r._apply_coverage(treffer, "Du hast den scheiss Kessel immer noch drin, aber immerhin ist es jetzt ordentlich")
    assert res[0]["id"] == "ziel"
    assert len(res) == 3


def test_two_full_covers_change_nothing():
    """Zwei Volltreffer (z. B. Original und Kopie) — die Regel entscheidet nicht."""
    df, n = _seltener_bestand()
    r = _retriever(df, n)
    text = "Du hast den scheiß Kessel immer noch drin, aber immerhin ist es jetzt ordentlich"
    treffer = [_hit("a", "etwas anderes"), _hit("b", text), _hit("c", text)]
    res = r._apply_coverage(treffer, "Du hast den scheiss Kessel immer noch drin, aber immerhin ist es jetzt ordentlich")
    assert [h["id"] for h in res] == ["a", "b", "c"]


def test_suppressed_twin_does_not_count():
    """Der zurueckgezogene Zwilling zaehlt nicht als sichtbarer Volltreffer."""
    df, n = _seltener_bestand()
    text = "Du hast den scheiß Kessel immer noch drin, aber immerhin ist es jetzt ordentlich"
    r = _retriever(df, n, suppressed={"b"})
    treffer = [_hit("a", "etwas anderes"), _hit("b", text), _hit("c", text)]
    res = r._apply_coverage(treffer, "Du hast den scheiss Kessel immer noch drin, aber immerhin ist es jetzt ordentlich")
    assert res[0]["id"] == "c"


def test_paraphrase_is_untouched():
    """Eine umformulierte Frage deckt nichts vollstaendig ab — Reihenfolge bleibt."""
    df, n = _seltener_bestand()
    r = _retriever(df, n)
    treffer = [_hit("a", "der agent ueberwacht die ollama nutzung"),
               _hit("b", "ollama nutzung wird ueberwacht"),
               _hit("c", "ganz anderer text")]
    res = r._apply_coverage(treffer, "wie ueberwacht der agent die ollama nutzung")
    assert [h["id"] for h in res] == ["a", "b", "c"]


def test_query_of_filler_words_does_not_fire():
    """Abfrage ohne aussagekraeftige Terme: kein Ausloeser, auch wenn einer deckt."""
    df = {"du": 800, "hast": 700, "es": 900, "jetzt": 600}
    r = _retriever(df, 1000)
    treffer = [_hit("a", "irgendwas"), _hit("b", "du hast es jetzt")]
    res = r._apply_coverage(treffer, "du hast es jetzt")
    assert [h["id"] for h in res] == ["a", "b"]


def test_without_index_the_rule_stays_silent():
    """Ohne Dokumenthaeufigkeit (kein Index) feuert nichts — kein Fehler."""
    r = object.__new__(HybridRetriever)
    r._suppressed_ids = set()
    r._df = None
    r._doc_count = 0
    treffer = [_hit("a", "x"), _hit("b", ZITAT)]
    res = r._apply_coverage(treffer, ZITAT)
    assert [h["id"] for h in res] == ["a", "b"]


def test_single_result_is_left_alone():
    r = _retriever(*_seltener_bestand())
    assert r._apply_coverage([_hit("a", ZITAT)], ZITAT)[0]["id"] == "a"


def test_thresholds_are_sane():
    assert 0 < COVERAGE_RARE_DF_RATIO < 0.2
    assert COVERAGE_MIN_RARE_TERMS >= 2


def test_rare_terms_excludes_common_words():
    df, n = _seltener_bestand()
    r = _retriever(df, n)
    selten = r._rare_query_terms("du hast den kessel scheiss ordentlich")
    assert "du" not in selten and "hast" not in selten
    assert "kessel" in selten and "scheiss" in selten
