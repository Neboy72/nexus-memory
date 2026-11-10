"""Tests fuer das gemeinsame Schreibtor (``nexus/write_gate.py``).

Jede Regel einzeln, dazu ein Durchlauf durch den echten Schreibweg des
Hermes-Plugins (``_upsert``): leerer Inhalt und Dokumenttext als Regel muessen
dort abgewiesen werden, ohne dass ein Punkt in die Sammlung geht. Zum Schluss
der Stichwortindex: ein nachgezogener Punkt muss auffindbar sein, und ein
Indexfehler darf das Schreiben nicht verhindern.

Die Laengengrenze fuer ``fact`` ist in ``tests/conftest.py`` fuer die uebrige
Suite abgeschaltet (kurze Beispieltexte); hier wird sie ausdruecklich
eingeschaltet.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from nexus.write_gate import MIN_FACT_CHARS, last_reason, prepare

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


@pytest.fixture
def strenge_grenze(monkeypatch):
    """Die Vorgabegrenze fuer ``fact`` (conftest schaltet sie fuer andere Tests ab)."""
    monkeypatch.setenv("NEXUS_WRITE_GATE_MIN_FACT_CHARS", str(MIN_FACT_CHARS))
    return MIN_FACT_CHARS


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


# ── 2. Etikett statt Fakt ────────────────────────────────────────────


class TestLaengengrenze:
    def test_kurzer_fakt_wird_abgewiesen(self, strenge_grenze):
        assert prepare("zu kurz", category="fact") is None
        assert "Zeichen" in last_reason()

    def test_genau_an_der_grenze_wird_angenommen(self, strenge_grenze):
        text = "x" * strenge_grenze
        assert prepare(text, category="fact") is not None

    def test_ein_zeichen_zu_kurz_wird_abgewiesen(self, strenge_grenze):
        assert prepare("x" * (strenge_grenze - 1), category="fact") is None

    @pytest.mark.parametrize("kategorie", ["temp", "session", "belief", "rule", "entity"])
    def test_kurzer_text_in_anderer_kategorie_bleibt_erlaubt(self, strenge_grenze, kategorie):
        """Etiketten sind nur bei ``fact`` ein Problem; Entities heissen so."""
        assert prepare("kurz", category=kategorie, source_type="chat") is not None

    def test_vorgabe_gilt_ohne_umgebungsvariable(self, monkeypatch):
        monkeypatch.delenv("NEXUS_WRITE_GATE_MIN_FACT_CHARS", raising=False)
        assert prepare("x" * (MIN_FACT_CHARS - 1), category="fact") is None
        assert prepare("x" * MIN_FACT_CHARS, category="fact") is not None

    def test_null_schaltet_die_grenze_ab(self, monkeypatch):
        monkeypatch.setenv("NEXUS_WRITE_GATE_MIN_FACT_CHARS", "0")
        assert prepare("x", category="fact") is not None


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


# ── 5. Regel nur aus Gespraech oder Handeingabe ──────────────────────


class TestRegelherkunft:
    REGEL = "Der Verteiler darf nur nach Ruecksprache abgeschaltet werden"

    @pytest.mark.parametrize("quelle,quellentyp", [
        ("paperless", "paperless"),
        ("wiki", "wiki"),
        ("https://example.com/seite", ""),
        ("mail", "email"),
        ("", "cron"),
        ("", "document"),
    ])
    def test_dokument_und_webtext_werden_keine_regel(self, quelle, quellentyp):
        assert prepare(self.REGEL, category="rule", source=quelle,
                       source_type=quellentyp) is None
        assert "Regel" in last_reason()

    @pytest.mark.parametrize("quellentyp", ["chat", "manual", "conversation"])
    def test_gespraech_und_handeingabe_duerfen_regel_werden(self, quellentyp):
        assert prepare(self.REGEL, category="rule", source_type=quellentyp) is not None

    def test_regel_ohne_herkunft_wird_abgewiesen(self):
        assert prepare(self.REGEL, category="rule") is None


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
    def test_leerer_inhalt_wird_abgewiesen(self, monkeypatch, strenge_grenze):
        provider, gespeichert = _provider(monkeypatch)
        ergebnis = provider._upsert(text="   ")

        assert ergebnis["status"] == "rejected"
        assert ergebnis["reason"]
        assert gespeichert == [], "ein abgewiesener Eintrag darf nicht geschrieben werden"

    def test_dokumenttext_als_regel_wird_abgewiesen(self, monkeypatch):
        provider, gespeichert = _provider(monkeypatch)
        ergebnis = provider._upsert(
            text="Laut Paperless-Anleitung gilt: Der Verteiler bleibt dauerhaft aus",
            category="rule",
            source="paperless",
        )

        assert ergebnis["status"] == "rejected"
        assert "Regel" in ergebnis["reason"]
        assert gespeichert == []

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
