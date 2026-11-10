"""Tests fuer den Belegsatz der Entity-Extraktion.

Gemessen 10.11.2026: Von 2.000 Entity-Punkten waren 1.943 reine Etiketten
("concept: Heizöl", "service: Error Scanner (zeitplan=*/30)"). Ursache war die
Eingabe, nicht die Suche: die Extraktion kannte nur Name, Typ und Attribute und
warf den Satz weg, in dem der Name stand. Ein Etikett beantwortet keine Frage.
"""

from nexus_memory.entity_extractor import (
    Entity,
    ExtractionResult,
    attach_evidence,
    evidence_sentence,
    extract_entities,
)


class TestEvidenceSentence:
    def test_satz_mit_dem_namen(self):
        text = ("Der Vorrat reichte eine Woche. Marsolek Brennstoffe liefert das "
                "Heizöl nach Eltville. Preis noch offen.")
        assert evidence_sentence(text, "Marsolek") == ("Marsolek Brennstoffe liefert das "
                                                       "Heizöl nach Eltville.")

    def test_zeilenumbrueche_zaehlen_als_grenze(self):
        text = "Erste Zeile ohne Treffer\nQdrant laeuft auf Port 6333\nDritte Zeile"
        assert evidence_sentence(text, "Qdrant") == "Qdrant laeuft auf Port 6333"

    def test_langer_satz_wird_gefenstert(self):
        text = "A" * 200 + " Marsolek " + "B" * 400
        got = evidence_sentence(text, "Marsolek", max_chars=100)
        assert "Marsolek" in got
        assert len(got) <= 102          # zwei Auslassungszeichen dazu
        assert got.startswith("…") and got.endswith("…")

    def test_name_nicht_im_text(self):
        assert evidence_sentence("Nur ein Satz ohne den Namen.", "Marsolek") == ""

    def test_leere_eingaben(self):
        assert evidence_sentence("", "Qdrant") == ""
        assert evidence_sentence("Text", "") == ""


class TestAttachEvidence:
    def test_fuellt_leere_belege(self):
        res = ExtractionResult([Entity("Qdrant", "service")], [])
        attach_evidence(res, "Qdrant laeuft auf Port 6333. Sonst nichts.")
        assert res.entities[0].evidence == "Qdrant laeuft auf Port 6333."

    def test_laesst_vorhandene_belege_stehen(self):
        ent = Entity("Qdrant", "service", evidence="schon da")
        attach_evidence(ExtractionResult([ent], []), "Qdrant laeuft auf Port 6333.")
        assert ent.evidence == "schon da"


class TestExtractEntities:
    def test_heuristik_haengt_den_belegsatz_an(self):
        text = ("Wir haben Paperless neu aufgesetzt. Der Server laeuft seit heute "
                "wieder stabil.")
        res = extract_entities(text)          # kein hermes_home -> Heuristik
        paperless = [e for e in res.entities if e.name.lower() == "paperless"]
        assert paperless, "Heuristik sollte Paperless finden"
        assert paperless[0].evidence == "Wir haben Paperless neu aufgesetzt."

    def test_beleg_landet_im_dict(self):
        ent = Entity("Qdrant", "service", evidence="Qdrant laeuft auf Port 6333.")
        assert ent.to_dict()["evidence"] == "Qdrant laeuft auf Port 6333."
