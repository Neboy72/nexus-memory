"""Tests fuer den Stichwortindex (FTS5), die RRF-Fusion und den Ausfallpfad.

Der Index loest das gemessene Problem vom 10.11.2026: Ein Fakt, der ein seltenes
Wort der Frage woertlich traegt, landet bei reiner Aehnlichkeitssuche oft nicht
im Kandidatenkreis. BM25 gewichtet das seltene Wort hoch; RRF verschmilzt die
Stichwortliste mit der Aehnlichkeitsliste, ohne einen der beiden Wege zu
bevorzugen.
"""

import importlib.util
import os
import sys
import threading

from plugins.memory.nexus import keyword_index as fts
from plugins.memory.nexus.keyword_index import (
    KeywordIndex,
    build_index,
    ensure_index,
    index_body,
    query_expression,
    rrf_fuse,
    tokenize,
)

STOPP = frozenset({"welche", "welcher", "welches", "hat", "der", "die", "das",
                   "im", "ist", "ein", "eine"})


class TestTokenisierung:
    def test_umlaute_werden_gefaltet(self):
        assert tokenize("Heizöl-Lieferant") == ["heizoel", "lieferant"]

    def test_version_bleibt_ein_token(self):
        assert tokenize("Fassung 0.23.1") == ["fassung", "0_23_1"]

    def test_ip_adresse_bleibt_ein_token(self):
        assert tokenize("192.168.31.206") == ["192_168_31_206"]

    def test_punktierte_zahl_bleibt_ein_token(self):
        assert tokenize("5.800 Liter") == ["5_800", "liter"]

    def test_stoppwoerter_fallen_weg(self):
        assert tokenize("Welcher Kessel steht im Keller", stopwords=STOPP) == \
            ["kessel", "steht", "keller"]

    def test_mindestlaenge(self):
        assert tokenize("IP vom NAS", min_len=3) == ["vom", "nas"]

    def test_eszet_wird_ss(self):
        assert tokenize("Straße") == ["strasse"]

    def test_index_text_enthaelt_suchvarianten(self):
        body = index_body("organization: Marsolek", "lieferant")
        assert "marsolek" in body and "lieferant" in body

    def test_abfrageausdruck_oder_verknuepft(self):
        assert query_expression("Heizöl-Lieferant", stopwords=STOPP) == \
            '"heizoel" OR "lieferant"'

    def test_leere_abfrage(self):
        assert query_expression("welche im", stopwords=STOPP) is None


class TestIndexAufbau:
    @staticmethod
    def _punkte():
        return [
            {"id": "a", "payload": {"content": "Die Wärmepumpe ist eine Vitocal 250",
                                    "category": "fact", "access_level": "public"}},
            {"id": "b", "payload": {"content": "Das Notebook ist ein Ideapad",
                                    "category": "fact", "access_level": "private"}},
            {"id": "c", "payload": {"content": "Der Kessel ist ein Buderus",
                                    "category": "fact", "scope": "haus"}},
        ]

    def test_baut_index_und_findet_seltenes_wort(self, tmp_path):
        pfad = tmp_path / "fts.db"
        assert build_index(self._punkte(), pfad, "testcoll") == 3
        index = KeywordIndex(pfad, "testcoll")
        assert index.count() == 3
        assert index.is_current(3)
        treffer = index.search("Welcher Kessel steht im Keller", stopwords=STOPP)
        assert [pid for pid, _ in treffer][0] == "c"

    def test_payload_wird_schlank_gespeichert(self, tmp_path):
        pfad = tmp_path / "fts.db"
        build_index(self._punkte(), pfad, "testcoll")
        treffer = KeywordIndex(pfad, "testcoll").search("Ideapad", stopwords=STOPP)
        _pid, payload = treffer[0]
        assert payload["access_level"] == "private"
        assert "scope" not in payload  # nur gesetzte Felder

    def test_seltenes_wort_schlaegt_haeufiges(self, tmp_path):
        punkte = [
            {"id": "haeufig", "payload": {"content": "Kessel Kessel Kessel Kessel"}},
            {"id": "selten", "payload": {"content": "Kessel Vitocal"}},
        ]
        pfad = tmp_path / "fts.db"
        build_index(punkte, pfad, "testcoll")
        treffer = KeywordIndex(pfad, "testcoll").search(
            "Kessel Vitocal", stopwords=STOPP)
        # BM25 gewichtet das seltene Wort staerker: der Punkt mit Vitocal vorn.
        assert [pid for pid, _ in treffer][0] == "selten"

    def test_ohne_tragbares_wort_keine_treffer(self, tmp_path):
        pfad = tmp_path / "fts.db"
        build_index(self._punkte(), pfad, "testcoll")
        assert KeywordIndex(pfad, "testcoll").search("welche im", stopwords=STOPP) == []

    def test_meta_traegt_sammlung_und_zeilenzahl(self, tmp_path):
        pfad = tmp_path / "fts.db"
        build_index(self._punkte(), pfad, "testcoll")
        index = KeywordIndex(pfad, "testcoll")
        assert index.meta() == {"collection": "testcoll", "points": "3"}
        assert not KeywordIndex(pfad, "andere").is_current(3)


class TestRrfFusion:
    def test_punkt_in_beiden_listen_gewinnt(self):
        assert rrf_fuse([["a", "b", "c"], ["c", "d"]], k=60) == ["c", "a", "b", "d"]

    def test_gleichstand_bleibt_stabil(self):
        # a und d teilen den Beitrag (jeweils Rang 0); a wurde zuerst gesehen.
        assert rrf_fuse([["a", "b"], ["d"]], k=60) == ["a", "d", "b"]

    def test_k_daempft_den_rang(self):
        # Bei kleinem k zaehlt der Spitzenplatz deutlich mehr.
        klein = rrf_fuse([["a", "b"], ["b"]], k=1)
        gross = rrf_fuse([["a", "b"], ["b"]], k=1000)
        assert klein[0] == "b" and gross[0] == "b"
        assert klein.index("a") == 1 and gross.index("a") == 1


class _FakeQdrant:
    def __init__(self, count=None, records=None, fail=False):
        self._count = count
        self._records = records or []
        self._fail = fail

    def count(self, collection_name, exact=True):
        if self._fail:
            raise RuntimeError("Qdrant nicht erreichbar")
        if self._count is None:
            raise AttributeError("count")
        return type("R", (), {"count": self._count})()

    def scroll(self, **_kwargs):
        if self._fail:
            raise RuntimeError("Qdrant nicht erreichbar")
        return self._records, None


class TestAusfall:
    def test_fehlende_datei_ist_kein_index(self, tmp_path):
        index = KeywordIndex(tmp_path / "fehlt.db", "testcoll")
        assert index.meta() == {}
        assert index.count() == -1
        assert not index.is_current(5)
        assert index.search("Kessel", stopwords=STOPP) == []

    def test_kaputte_datei_wirft_nicht(self, tmp_path):
        pfad = tmp_path / "kaputt.db"
        pfad.write_bytes(b"das ist keine datenbank")
        index = KeywordIndex(pfad, "testcoll")
        assert index.count() == -1
        # count unbekannt -> kein Hintergrundaufbau, kein Fehler nach aussen.
        assert ensure_index(_FakeQdrant(count=None), "testcoll", pfad) is None

    def test_ohne_sammlungszahl_wird_nicht_gebaut(self, tmp_path):
        pfad = tmp_path / "fehlt.db"
        assert ensure_index(_FakeQdrant(count=None), "testcoll", pfad) is None
        assert not pfad.exists()

    def test_leere_sammlung_baut_nichts(self, tmp_path):
        pfad = tmp_path / "fehlt.db"
        assert ensure_index(_FakeQdrant(count=0), "testcoll", pfad) is None
        assert not pfad.exists()

    def test_qdrant_ausfall_wirft_nicht(self, tmp_path):
        assert ensure_index(_FakeQdrant(fail=True), "testcoll",
                            tmp_path / "fehlt.db") is None

    def test_veralteter_index_wird_weiter_gelesen(self, tmp_path, monkeypatch):
        pfad = tmp_path / "fts.db"
        punkte = [{"id": "a", "payload": {"content": "Buderus Kessel"}}]
        build_index(punkte, pfad, "testcoll")
        # Kein Hintergrundfaden im Test: geprueft wird, dass der veraltete
        # Index diese Runde noch gelesen wird.
        monkeypatch.setattr(fts, "_rebuild_in_background", lambda *a, **k: None)
        index = ensure_index(_FakeQdrant(count=99), "testcoll", pfad)
        assert isinstance(index, KeywordIndex)
        assert index.search("Buderus", stopwords=STOPP)[0][0] == "a"

    def test_andere_sammlung_wird_nicht_ueberschrieben(self, tmp_path):
        pfad = tmp_path / "fts.db"
        build_index([{"id": "a", "payload": {"content": "Buderus Kessel"}}],
                    pfad, "nexus_hf")
        assert ensure_index(_FakeQdrant(count=1), "testcoll", pfad) is None
        # Der fremde Index bleibt unangetastet.
        assert KeywordIndex(pfad, "nexus_hf").count() == 1


# ── Plugin-Weg: Fusion ersetzt die Ja/Nein-Umsortierung ────────────────────

def _plugin():
    spec = importlib.util.spec_from_file_location(
        "nexus_plugin_fts_test",
        os.path.join(os.path.dirname(__file__), "..", "plugins", "memory",
                     "nexus", "__init__.py"),
    )
    plug = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(plug)
    return plug


class _Punkt:
    def __init__(self, pid, content, score=0.9):
        self.id, self.score = pid, score
        self.payload = {"content": content, "category": "fact"}


class TestPluginFusion:
    def test_rrf_ordnung_bevorzugt_das_aehnlichkeitsobjekt(self):
        plug = _plugin()
        aehnlich = [_Punkt("a", "erst"), _Punkt("b", "zweit")]
        stichwort = [plug._KeywordPoint("c", {"content": "dritt"}), aehnlich[0]]
        geordnet = plug._rrf_order(aehnlich, stichwort)
        assert [p.id for p in geordnet] == ["a", "c", "b"]
        assert geordnet[0] is aehnlich[0]

    def test_ohne_modul_bleibt_die_aehnlichkeitsliste(self, monkeypatch):
        plug = _plugin()
        aehnlich = [_Punkt("a", "erst"), _Punkt("b", "zweit")]
        monkeypatch.setattr(plug, "_keyword_index_mod", None)
        assert plug._keyword_candidates_from_index(None, "c", "frage", aehnlich) == []
        assert plug._rrf_order(aehnlich, []) == aehnlich


class TestPrefetchOhneJaNeinSortierung:
    def test_stichworttreffer_landet_im_block(self, tmp_path, monkeypatch):
        plug = _plugin()
        pfad = tmp_path / "fts.db"
        build_index([
            {"id": "etikett", "payload": {"content": "concept: Heizung (gas)"}},
            {"id": "antwort", "payload": {
                "content": "Heizöl-Bestellung 2000 Liter bei Marsolek Brennstoffe"}},
        ], pfad, "testcoll")
        monkeypatch.setenv("NEXUS_FTS_DB", str(pfad))

        class FakeQdrant:
            def query_points(self, collection_name, query, limit, **kw):
                return type("R", (), {"points": [_Punkt("etikett", "concept: Heizung (gas)")]})()

            def count(self, collection_name, exact=True):
                return type("R", (), {"count": 2})()

            def scroll(self, **_kw):
                return [], None

        prov = object.__new__(plug.NexusMemoryProvider)
        prov._prefetch_result = ""
        prov._prefetch_lock = threading.Lock()
        prov._collection = "testcoll"
        prov._agent_context = "primary"
        prov._chat_shared_cache = False
        prov._scope_centroids = None
        prov._skill_graph = None
        prov._skill_graph_lock = threading.Lock()

        class FakeEmbedder:
            def embed(self, text, is_query=True):
                return [0.0] * 8

            def embed_cached(self, text):
                return [0.0] * 8

        class FakeCache:
            def get(self, text, is_query=True):
                return None

            def put(self, text, vec, is_query=True):
                return None

        prov._embedder = FakeEmbedder()
        prov._embed_cache = FakeCache()
        prov._qdrant = FakeQdrant()
        prov._graph_boost = lambda pts, **kw: []
        # Die alte Ja/Nein-Sortierung darf im Einspielweg nicht mehr laufen.
        monkeypatch.setattr(plug, "_keyword_boost_order",
                            lambda *a, **k: (_ for _ in ()).throw(AssertionError(
                                "_keyword_boost_order darf nicht mehr laufen")))

        prov._do_prefetch_inner("Heizöl-Lieferant")
        assert "marsolek" in prov._prefetch_result.lower()
