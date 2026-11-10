"""Tests fuer das gemeinsame Schreibtor (``nexus/write_gate.py``).

Jede Regel einzeln, dazu ein Durchlauf durch den echten Schreibweg des
Hermes-Plugins (``_upsert``): leerer Inhalt muss dort abgewiesen werden, ohne
dass ein Punkt in die Sammlung geht, und eine Regel ohne menschlichen Nachweis
kommt als ``fact`` an. Zum Schluss der Stichwortindex: ein nachgezogener Punkt
muss auffindbar sein, und ein Indexfehler darf das Schreiben nicht verhindern.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from nexus.write_gate import last_reason, prepare

REPO = Path(__file__).resolve().parents[1]
if str(REPO / "src") not in sys.path:
    sys.path.insert(0, str(REPO / "src"))

_PLUGIN_PATH = REPO / "plugins" / "memory" / "nexus" / "__init__.py"
_spec = importlib.util.spec_from_file_location("nexus_plugin_under_gate_test", _PLUGIN_PATH)
assert _spec is not None
plugin = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(plugin)

LANGE_QUELLE = "Eine Quelle mit ausreichend Text fuer das Schreibtor"
FAKT = "Der Verteiler liegt im Flur neben der Kellertuer"  # 47 Zeichen


# ── 1. Leerer Inhalt ─────────────────────────────────────────────────


class TestLeererInhalt:
    def test_leerer_text_wird_abgewiesen(self):
        assert prepare("") is None
        assert "leer" in last_reason()

    def test_nur_leerzeichen_werden_abgewiesen(self):
        assert prepare("   \n\t ") is None
        assert "leer" in last_reason()

    def test_none_wird_abgewiesen(self):
        assert prepare(None) is None


# ── 2. Etikett statt Fakt (Etikettenerkennung, keine Laengengrenze) ──


class TestEtikettenerkennung:
    """Nicht die Laenge entscheidet, sondern die Form: ``wort: wert`` ohne
    Satzzeichen und ohne Verb ist ein Etikett. Ein kurzer Fakt mit Zahl oder
    Kennung geht durch — die alte Laengengrenze hat echte Fakten verloren."""

    @pytest.mark.parametrize("text", [
        "Farbe: blau",
        "Kategorie: Werkzeug",
        "Standort: Keller",
        "concept: Heizoel",
        "software: Skill.md",
    ])
    def test_etikett_wird_abgewiesen(self, text):
        assert prepare(text, category="fact") is None
        assert "Etikett" in last_reason()

    @pytest.mark.parametrize("text", [
        "Tuer-Code: 4711",
        "Blutgruppe: A+",
        "PIN: 1234",
        "Seriennummer: AB-12",
    ])
    def test_kurzer_fakt_mit_zahl_oder_kennung_geht_durch(self, text):
        prepared = prepare(text, category="fact")
        assert prepared is not None, last_reason()
        assert prepared["content"] == text

    def test_satz_mit_doppelpunkt_ist_kein_etikett(self):
        assert prepare("Status: Der Verteiler laeuft wieder", category="fact") is not None

    def test_satz_ohne_doppelpunkt_ist_kein_etikett(self):
        assert prepare("Kurz", category="fact") is not None

    @pytest.mark.parametrize("kategorie", ["temp", "session", "belief", "entity"])
    def test_etikett_in_anderer_kategorie_bleibt_erlaubt(self, kategorie):
        """Etiketten sind nur bei ``fact`` ein Problem; Entities heissen so."""
        assert prepare("Farbe: blau", category=kategorie) is not None

    def test_die_grenze_haengt_nicht_an_der_umgebung(self, monkeypatch):
        """Der Schwellwert steht in der Bibliothek, nicht beim Aufrufer."""
        monkeypatch.setenv("NEXUS_WRITE_GATE_MIN_FACT_CHARS", "0")
        assert prepare("Farbe: blau", category="fact") is None


# ── 3. Herkunft nie leer ─────────────────────────────────────────────


class TestHerkunft:
    def test_ohne_angaben_wird_die_herkunft_gebaut(self):
        prepared = prepare(FAKT, source_type="chat")
        assert prepared["provenance"]["source_type"] == "chat"
        assert prepared["provenance"]["source"]

    def test_ganz_ohne_herkunft_bleibt_sie_nicht_leer(self):
        prepared = prepare(FAKT)
        assert prepared["provenance"]["source_type"] == "unbekannt"
        assert prepared["provenance"]["source"] == "unbekannt"

    def test_nur_quelle_ergibt_quellentyp(self):
        prepared = prepare(FAKT, source="paperless")
        assert prepared["provenance"]["source"] == "paperless"
        assert prepared["provenance"]["source_type"] == "paperless"

    def test_vorhandene_provenance_wird_ergaenzt_nicht_ersetzt(self):
        prepared = prepare(
            FAKT,
            provenance={
                "source_type": "hermes-plugin",
                "created_by": "nexus-memory-provider",
                "confidence": 0.9,
            },
            source_type="chat",
        )
        prov = prepared["provenance"]
        assert prov["source_type"] == "hermes-plugin"  # Schreiber bleibt Schreiber
        assert prov["created_by"] == "nexus-memory-provider"
        assert prov["confidence"] == 0.9
        assert prov["source"]  # fehlte und wurde gebaut


# ── 4. Zugriffsstufe: die strengste aus Quelle und Angabe ───────────


class TestZugriffsstufe:
    @pytest.mark.parametrize("quelle", ["paperless", "wiki", "Paperless-Import"])
    def test_dokumentquellen_sind_privat(self, quelle):
        assert prepare(FAKT, source=quelle)["access_level"] == "private"

    @pytest.mark.parametrize("quelle", [
        "mail", "email", "postfach", "kalender", "termin", "gesundheit",
        "arzt", "befund", "diagnose", "blutdruck", "medikament",
        "konto", "iban", "versicherung", "steuer", "rechnung",
    ])
    def test_post_kalender_und_gesundheitsquellen_sind_privat(self, quelle):
        """Diese Quellen sind immer ``private`` — egal, was der Aufrufer sagt."""
        assert prepare(FAKT, source=quelle)["access_level"] == "private"
        assert prepare(FAKT, access_level="public", source=quelle)["access_level"] == "private"

    def test_geld_und_gesundheit_aus_dem_gespraech_bleiben_privat(self):
        """Auch als Quellentyp statt Quelle (OpenClaw/MCP setzen source_type)."""
        assert prepare(FAKT, source_type="email")["access_level"] == "private"
        assert prepare(FAKT, source_type="gesundheit")["access_level"] == "private"

    @pytest.mark.parametrize("quelle", ["nicht-oeffentlich", "/srv/public/notizen", "/public/wiki"])
    def test_kein_teilstring_und_kein_pfad_macht_oeffentlich(self, quelle):
        """``nicht-oeffentlich`` verneint, ``/public/`` ist ein Verzeichnis."""
        assert prepare(FAKT, source=quelle)["access_level"] == "private"

    def test_ganzes_wort_oeffentlich_bleibt_oeffentlich(self):
        assert prepare(FAKT, source="public")["access_level"] == "public"
        assert prepare(FAKT, source="oeffentlich")["access_level"] == "public"

    @pytest.mark.parametrize("quelle", [
        "hermes-plugin-session-end", "sitzung", "transcript", "protokoll",
    ])
    def test_sitzungsquelle_ist_vertraut(self, quelle):
        assert prepare(FAKT, source=quelle)["access_level"] == "trusted"

    def test_kategorie_session_allein_senkt_die_stufe_nicht(self):
        """Eine Kategorie sagt nicht, wer lesen darf — die Quelle sagt es.

        ``sync_turn`` schreibt Kategorie ``session``; ein pauschales
        ``trusted`` dort wuerde den Raum-Deckel (geteilter Raum -> public)
        wieder aufheben.
        """
        assert prepare(FAKT, category="session", access_level="public")["access_level"] == "public"

    def test_ausdrueckliche_stufe_bleibt(self):
        assert prepare(FAKT, access_level="private")["access_level"] == "private"
        assert prepare(FAKT, access_level="trusted")["access_level"] == "trusted"

    def test_quelle_ist_niemals_lockerer_als_die_angabe(self):
        """Public aus einem Paperless-Dokument wird nicht oeffentlich."""
        assert prepare(FAKT, access_level="public", source="paperless")["access_level"] == "private"
        assert prepare(FAKT, access_level="trusted", source="wiki")["access_level"] == "private"
        assert prepare(FAKT, access_level="public", source="sitzung")["access_level"] == "trusted"

    def test_ausdrueckliches_public_bleibt_bei_neutraler_quelle(self):
        """Das Tor vergibt public nie selbst; eine Angabe bleibt eine Angabe.

        Sonst braeche der Raum-Deckel (13.11.2026): er setzt in einem geteilten
        Raum bewusst ``public``, weil der Inhalt fuer alle im Raum gedacht ist.
        """
        assert prepare(FAKT, access_level="public")["access_level"] == "public"

    def test_public_aus_ausdruecklich_oeffentlicher_quelle(self):
        assert prepare(FAKT, source="public")["access_level"] == "public"

    def test_unbekannte_stufe_faellt_auf_privat(self):
        assert prepare(FAKT, access_level="geheim")["access_level"] == "private"
        assert prepare(FAKT, access_level="PUBLIC")["access_level"] == "public"


# ── 5. Regel nur mit menschlichem Nachweis ───────────────────────────


class TestRegelherkunft:
    """``source_type`` und ``explicit`` sind Selbstauskuenfte des Modells.

    Seit 10.11.2026 entscheidet ein Nachweis, den ein Modell im Gespraech nicht
    erzeugen kann: das Token aus ``rule-confirm``, erzeugt von einem Menschen
    am Terminal, zehn Minuten gueltig, einmal, an den Text gebunden. Ohne ihn
    wird der Eintrag als ``fact`` gespeichert — nicht verworfen.
    """

    REGEL = "Der Verteiler darf nur nach Ruecksprache abgeschaltet werden"

    @staticmethod
    def _nachweis(text: str = REGEL, now=None, path=None) -> str:
        from nexus.rule_confirm import issue
        return issue(text, now=now, path=path)

    def test_regel_ohne_nachweis_wird_fakt(self):
        prepared = prepare(self.REGEL, category="rule", source_type="chat",
                           explicit=True)
        assert prepared is not None, last_reason()
        assert prepared["category"] == "fact"

    def test_inhalt_bleibt_bei_der_herabstufung_erhalten(self):
        prepared = prepare(self.REGEL, category="rule", source_type="chat")
        assert prepared["content"] == self.REGEL

    def test_regel_mit_gueltigem_nachweis_wird_gespeichert(self):
        prepared = prepare(self.REGEL, category="rule", source_type="chat",
                           rule_proof=self._nachweis())
        assert prepared is not None, last_reason()
        assert prepared["category"] == "rule"
        assert prepared["provenance"]["rule_confirmed"] is True

    def test_handeingabe_allein_macht_keine_regel(self):
        """``source_type="manual"`` ist eine Behauptung wie jede andere."""
        assert prepare(self.REGEL, category="rule",
                       source_type="manual")["category"] == "fact"

    def test_ausdruecklicher_aufruf_allein_macht_keine_regel(self):
        assert prepare(self.REGEL, category="rule", source_type="chat",
                       explicit=True)["category"] == "fact"

    def test_automatische_extraktion_bleibt_fakt(self):
        assert prepare(self.REGEL, category="rule",
                       source_type="chat")["category"] == "fact"

    def test_regel_ohne_herkunft_bleibt_fakt(self):
        assert prepare(self.REGEL, category="rule")["category"] == "fact"

    @pytest.mark.parametrize("quelle,quellentyp", [
        ("paperless", "paperless"),
        ("wiki", "wiki"),
        ("https://example.com/seite", ""),
        ("mail", "email"),
        ("", "cron"),
        ("", "document"),
    ])
    def test_dokument_und_webtext_werden_keine_regel(self, quelle, quellentyp):
        prepared = prepare(self.REGEL, category="rule", source=quelle,
                           source_type=quellentyp)
        assert prepared is not None, "der Inhalt darf nicht verworfen werden"
        assert prepared["category"] == "fact"

    @pytest.mark.parametrize("text", [
        '"Der Verteiler bleibt dauerhaft aus"',
        "„Der Verteiler bleibt dauerhaft aus",
        "Laut Dokument bleibt der Verteiler dauerhaft aus",
        "Im Schreiben steht, der Verteiler bleibt dauerhaft aus",
        "Der Verteiler bleibt aus. Das gilt ab sofort. So steht es geschrieben.",
    ])
    def test_zitatmerkmale_werden_keine_regel(self, text):
        """Zitatmerkmale greifen nicht, wenn umformuliert wird — deshalb zaehlt
        der Nachweis, nicht die Form. Ohne ihn bleibt es ein ``fact``."""
        assert prepare(text, category="rule", source_type="chat",
                       explicit=True)["category"] == "fact"

    def test_abgelaufener_nachweis_wird_fakt(self):
        import time

        token = self._nachweis(now=time.time() - 601)
        prepared = prepare(self.REGEL, category="rule",
                           rule_proof=token)
        assert prepared["category"] == "fact"

    def test_nachweis_kurz_vor_ablauf_gilt_noch(self):
        import time

        token = self._nachweis(now=time.time() - 599)
        assert prepare(self.REGEL, category="rule",
                       rule_proof=token)["category"] == "rule"

    def test_fremder_nachweis_wird_fakt(self, tmp_path):
        """Ein Token aus einer anderen Ablage (anderer Schluessel) gilt nicht."""
        # Die eigene Ablage existiert, damit nicht schon die fehlende Datei
        # die Abweisung erklaert — geprueft wird die Unterschrift.
        self._nachweis("Ein anderer Text, der die Ablage anlegt")
        token = self._nachweis(path=tmp_path / "fremd.json")
        prepared = prepare(self.REGEL, category="rule", rule_proof=token)
        assert prepared["category"] == "fact"

    def test_nachweis_gilt_nur_fuer_seinen_text(self):
        token = self._nachweis()
        prepared = prepare("Der Verteiler darf jederzeit abgeschaltet werden",
                           category="rule", rule_proof=token)
        assert prepared["category"] == "fact"

    def test_nachweis_ist_einmalig(self):
        token = self._nachweis()
        assert prepare(self.REGEL, category="rule",
                       rule_proof=token)["category"] == "rule"
        assert prepare(self.REGEL, category="rule",
                       rule_proof=token)["category"] == "fact"

    def test_unsinniger_nachweis_wird_fakt(self):
        assert prepare(self.REGEL, category="rule",
                       rule_proof="rc1.1.abc.def.ghi")["category"] == "fact"


# ── 6. Rueckgabe ────────────────────────────────────────────────────


class TestRueckgabe:
    def test_inhalt_wird_getrimmt(self):
        assert prepare(f"  {FAKT}  ")["content"] == FAKT

    def test_unbekannte_kategorie_wird_fakt(self):
        assert prepare(FAKT, category="quatsch")["category"] == "fact"

    def test_erfolg_loescht_den_alten_grund(self):
        assert prepare("") is None
        assert last_reason()
        assert prepare(FAKT) is not None
        assert last_reason() == ""

    def test_alle_felder_sind_gesetzt(self):
        prepared = prepare(FAKT, category="belief", access_level="trusted", source="notiz")
        assert set(prepared) == {"content", "category", "access_level", "source", "provenance"}


# ── 7. Der echte Schreibweg des Plugins ──────────────────────────────


class _Embedder:
    def embed(self, text, is_query=False):
        return [0.0] * 8


def _provider(monkeypatch):
    """Provider ohne Netz: Einbettung, Qdrant und Statistik sind Attrappen."""
    provider = object.__new__(plugin.NexusMemoryProvider)
    provider._embedder = _Embedder()
    provider._qdrant = object()
    provider._collection = "test-collection"
    provider.vector_name = None
    gespeichert: list = []

    import nexus_memory.collection_vectors as collection_vectors

    def _upsert_named(client, collection, points, vector_name):
        gespeichert.extend(points)

    monkeypatch.setattr(collection_vectors, "upsert_named", _upsert_named)
    monkeypatch.setattr(plugin.NexusMemoryProvider, "_bump_agent_stats",
                        lambda self, **kwargs: None)
    return provider, gespeichert


class TestPluginSchreibweg:
    def test_leerer_inhalt_wird_abgewiesen(self, monkeypatch):
        provider, gespeichert = _provider(monkeypatch)
        ergebnis = provider._upsert(text="   ")

        assert ergebnis["status"] == "rejected"
        assert ergebnis["reason"]
        assert gespeichert == [], "ein abgewiesener Eintrag darf nicht geschrieben werden"

    def test_dokumenttext_als_regel_wird_als_fakt_gespeichert(self, monkeypatch):
        """Ohne menschlichen Nachweis entsteht keine Regel — der Inhalt bleibt
        aber erhalten, statt verworfen zu werden."""
        provider, gespeichert = _provider(monkeypatch)
        monkeypatch.setattr(plugin, "_index_written_point", lambda *a, **k: None)
        ergebnis = provider._upsert(
            text="Laut Paperless-Anleitung gilt: Der Verteiler bleibt dauerhaft aus",
            category="rule",
            source="paperless",
        )

        assert ergebnis["status"] == "ok"
        assert len(gespeichert) == 1
        assert gespeichert[0].payload["category"] == "fact"

    def test_gueltiger_eintrag_traegt_herkunft_und_stufe(self, monkeypatch):
        provider, gespeichert = _provider(monkeypatch)
        monkeypatch.setattr(plugin, "_index_written_point", lambda *a, **k: None)

        ergebnis = provider._upsert(text=FAKT, category="fact", access_level="public",
                                    source="claude-code")

        assert ergebnis["status"] == "ok"
        payload = gespeichert[0].payload
        assert payload["content"] == FAKT
        assert payload["provenance"]["source_type"] == "hermes-plugin"
        assert payload["provenance"]["created_by"] == "nexus-memory-provider"

    def test_fehlendes_tor_weist_ab_statt_ungeprueft_zu_schreiben(self, monkeypatch):
        """Ein Fehler im Tor darf den alten, ungepruefte Zustand nicht
        wiederherstellen: ohne ladbares Tor wird abgewiesen und nichts
        geschrieben."""
        provider, gespeichert = _provider(monkeypatch)

        class _KaputterImport:
            def find_spec(self, name, path, target=None):
                if name == "nexus.write_gate":
                    raise ImportError("Tor absichtlich nicht ladbar")
                return None

        monkeypatch.setattr(sys, "meta_path", [_KaputterImport()] + list(sys.meta_path))
        monkeypatch.delitem(sys.modules, "nexus.write_gate", raising=False)

        ergebnis = provider._upsert(text=FAKT, category="fact")

        assert ergebnis["status"] == "rejected"
        assert "Schreibtor nicht ladbar" in ergebnis["reason"]
        assert gespeichert == [], "ohne Tor darf kein Punkt geschrieben werden"

    def test_fehler_im_stichwortindex_verhindern_das_schreiben_nicht(self, monkeypatch):
        provider, gespeichert = _provider(monkeypatch)

        class _KaputterIndex:
            def upsert_point(self, *args, **kwargs):
                raise RuntimeError("Index gesperrt")

        monkeypatch.setattr(plugin, "_keyword_index_mod", _KaputterIndex())

        ergebnis = provider._upsert(text=FAKT, category="fact")

        assert ergebnis["status"] == "ok"
        assert gespeichert, "der Punkt muss trotz Indexfehler geschrieben werden"


# ── 8. Stichwortindex nachziehen ─────────────────────────────────────


def _keyword_index_module():
    path = REPO / "plugins" / "memory" / "nexus" / "keyword_index.py"
    spec = importlib.util.spec_from_file_location("nexus_keyword_index_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestStichwortindexNachziehen:
    def test_nachgezogener_punkt_ist_auffindbar(self, tmp_path):
        ki = _keyword_index_module()
        db = tmp_path / "index.db"
        ki.build_index(
            [{"id": "alt", "payload": {"content": "Der Verteiler steht im Flur"}}],
            path=db, collection="nexus",
        )
        index = ki.KeywordIndex(db, "nexus")
        assert not index.search("Kellertuer")

        assert ki.upsert_point(
            {"id": "neu", "payload": {"content": "Die Kellertuer klemmt seit gestern"}},
            path=db, collection="nexus",
        )

        treffer = ki.KeywordIndex(db, "nexus").search("Kellertuer")
        assert [pid for pid, _ in treffer] == ["neu"]
        assert ki.KeywordIndex(db, "nexus").meta()["points"] == "2"

    def test_fremde_sammlung_wird_nicht_angefasst(self, tmp_path):
        ki = _keyword_index_module()
        db = tmp_path / "index.db"
        ki.build_index([{"id": "a", "payload": {"content": "Ein Punkt"}}],
                       path=db, collection="nexus")
        assert not ki.upsert_point({"id": "b", "payload": {"content": "Zweiter Punkt"}},
                                   path=db, collection="andere")
        assert ki.KeywordIndex(db, "nexus").count() == 1

    def test_fehlender_index_wird_nicht_angelegt(self, tmp_path):
        ki = _keyword_index_module()
        db = tmp_path / "gibt-es-nicht.db"
        assert not ki.upsert_point({"id": "a", "payload": {"content": "Inhalt"}},
                                   path=db, collection="nexus")
        assert not db.exists()

    def test_doppeltes_nachziehen_erzeugt_keinen_zweiten_eintrag(self, tmp_path):
        ki = _keyword_index_module()
        db = tmp_path / "index.db"
        ki.build_index([], path=db, collection="nexus")
        punkt = {"id": "a", "payload": {"content": "Die Kellertuer klemmt"}}
        assert ki.upsert_point(punkt, path=db, collection="nexus")
        assert ki.upsert_point(punkt, path=db, collection="nexus")
        index = ki.KeywordIndex(db, "nexus")
        assert index.count() == 1
        assert len(index.search("Kellertuer")) == 1


# ── 9. Bibliotheksweg ``nexus_remember`` ────────────────────────────


class TestBibliotheksweg:
    """``nexus.nexus_remember`` schrieb als einziger Weg ungeprueft direkt in
    die Sammlung. Er laeuft jetzt durch dasselbe Tor; eine Abweisung ist ein
    Fehler, kein stiller Erfolg."""

    @pytest.fixture
    def qdrant_put(self, monkeypatch):
        from unittest.mock import MagicMock

        antwort = MagicMock()
        antwort.status_code = 200
        antwort.json.return_value = {"result": {"status": "ok"}}
        antwort.text = '{"result": {"status": "ok"}}'
        put = MagicMock(return_value=antwort)
        monkeypatch.setattr("requests.put", put)
        return put

    def test_leerer_inhalt_wird_abgewiesen_und_nichts_geschrieben(self, qdrant_put):
        import nexus

        with pytest.raises(ValueError, match="abgewiesen"):
            nexus.nexus_remember("", collection_name="test-collection")
        assert not qdrant_put.called, "ein abgewiesener Eintrag darf Qdrant nicht erreichen"

    def test_etikett_wird_abgewiesen(self, qdrant_put):
        import nexus

        with pytest.raises(ValueError, match="abgewiesen"):
            nexus.nexus_remember("Farbe: blau", collection_name="test-collection")
        assert not qdrant_put.called

    def test_kurzer_fakt_mit_zahl_geht_durch(self, qdrant_put):
        import nexus

        nexus.nexus_remember("Tuer-Code: 4711", collection_name="test-collection")

        payload = qdrant_put.call_args.kwargs["json"]["points"][0]["payload"]
        assert payload["content"] == "Tuer-Code: 4711"
        assert payload["access_level"] == "private"

    def test_dokumentquelle_setzt_die_stufe_auf_privat(self, qdrant_put):
        import nexus

        nexus.nexus_remember(FAKT, source_type="paperless", access_level="public",
                             collection_name="test-collection")

        payload = qdrant_put.call_args.kwargs["json"]["points"][0]["payload"]
        assert payload["access_level"] == "private"

    def test_regel_aus_dem_cron_wird_als_fakt_gespeichert(self, qdrant_put):
        """Paperless- und Wiki-Cron-Jobs schreiben ueber diesen Weg.

        Ohne menschlichen Nachweis entsteht keine Regel; der Inhalt geht
        nicht verloren."""
        import nexus

        nexus.nexus_remember("Der Verteiler bleibt dauerhaft aus",
                             category="rule", source_type="cron",
                             collection_name="test-collection")

        payload = qdrant_put.call_args.kwargs["json"]["points"][0]["payload"]
        assert payload["category"] == "fact"
