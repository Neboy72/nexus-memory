"""Tests fuer nexus/variants.py — Suchvarianten beim Schreiben.

Der Fall, der das Modul ausgeloest hat, steht in ``test_marsolek_fall``:
Frage "Heizöl-Lieferant", gespeichert "organization: Marsolek (type=Oellieferant)"
— ohne "lieferant" im Text ist der Eintrag bei der Einspielung nicht zu finden.
"""

from nexus.variants import compound_parts, embed_text, fold, search_variants


class TestFold:
    def test_umlaute_und_eszett(self):
        assert fold("Heizöl") == "Heizoel"
        assert fold("Öllieferant") == "Oellieferant"
        assert fold("Straße") == "Strasse"

    def test_text_ohne_umlaute_bleibt(self):
        assert fold("Marsolek") == "Marsolek"

    def test_leerer_text(self):
        assert fold("") == ""


class TestCompoundParts:
    def test_drei_zeichen_vorn_zaehlt(self):
        # Der Fall aus der Messung: "Oel" ist zu kurz, "lieferant" traegt.
        assert compound_parts("Oellieferant") == ["lieferant"]

    def test_beide_teile_tragbar(self):
        assert compound_parts("heizoellieferant") == ["heizoel", "lieferant"]

    def test_unbekanntes_grundwort_wird_nicht_zerlegt(self):
        # "Hermesdatenbank" hat kein Grundwort aus der Liste — dann lieber nichts
        # als eine geratene Fuge.
        assert compound_parts("Hermesdatenbank") == []

    def test_praefix_am_schnitt_verwirft_den_linken_teil(self):
        # "datenschutzver|ordnung" ist die falsche Fuge; nur "ordnung" bleibt.
        assert compound_parts("Datenschutzverordnung") == ["ordnung"]

    def test_kurzes_wort(self):
        assert compound_parts("Haus") == []


class TestSearchVariants:
    def test_marsolek_fall(self):
        text = ("organization: Marsolek (type=Oellieferant, location=Eltville, "
                "customer_number=13258)")
        assert "lieferant" in search_variants(text)

    def test_umlautvariante_entsteht(self):
        terms = search_variants("device: Ölheizung (fuel=leichtes Heizöl)")
        assert "heizoel" in terms

    def test_nichts_zu_gewinnen(self):
        assert search_variants("software: Hermes-Datenbank (sessions=2261)") == ""

    def test_begrenzt_und_doppelfrei(self):
        terms = search_variants("Heizöllieferant " * 5, max_terms=3)
        liste = terms.split(", ")
        assert len(liste) <= 3
        assert len(liste) == len(set(liste))

    def test_mehrere_texte_werden_zusammengefasst(self):
        terms = search_variants("Öllieferant", "Brennstoffhändler")
        assert "oellieferant" in terms and "haendler" in terms


class TestEmbedText:
    def test_varianten_haengen_hinten_an(self):
        assert embed_text("Inhalt", "a, b") == "Inhalt a, b"

    def test_ohne_varianten_bleibt_der_inhalt(self):
        assert embed_text("Inhalt", "") == "Inhalt"
