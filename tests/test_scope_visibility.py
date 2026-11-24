"""Geltungsbereich: Fakten gemeinsam, Protokolle im Verbund (15.11.2026).

Ein Gedaechtnis fuer viele Agenten soll Wissen teilen, aber Gerede trennen.
Entschieden:

- ``scope == "default"`` ist der GEMEINSAME Bereich (Fakten, Regeln, Lehren,
  Prozeduren, Entities, Praeferenzen, Beliefs, Entscheidungen). Jeder Agent
  liest sie, gleich wer sie geschrieben hat.
- Jeder andere Wert ist ein VERBUND — die Agenten-Kennung (``resolve_agent_id``,
  beim HTTP-Weg aus dem Agentenschluessel, seit 15.11.2026 nicht mehr aus dem
  Kopf ``x-nexus-agent-id``). Dort bleiben die Sitzungsprotokolle
  (``category = "session"``).
- Punkte OHNE ``scope``-Feld sind Altbestand (27.948 am 15.11.2026) und bleiben
  fuer alle sichtbar — sie duerfen nicht verschwinden.

Die Zugriffsstufe bleibt eine eigene, unabhaengige Schranke: ``private`` heisst
"nicht nach aussen", nicht "nur fuer den Schreiber".
"""
from __future__ import annotations

import os
import sys
import threading
import types
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

_SHARED = "default"


# ===========================================================================
# Hilfen
# ===========================================================================

class _Point:
    """Ein Qdrant-Punkt, so weit recall/prefetch ihn anfassen."""

    def __init__(self, pid: str, payload: dict, score: float = 0.9):
        self.id = pid
        self.payload = payload
        self.score = score
        self.version = 0


class _RecallClient:
    """Zeichnet die Filter auf, die recall an Qdrant gibt."""

    def __init__(self, points):
        self._points = points
        self.filters: list = []

    def query_points(self, **kwargs):
        self.filters.append(kwargs.get("query_filter"))
        return types.SimpleNamespace(points=list(self._points))

    def retrieve(self, **_kwargs):      # Access-Tracking
        return []

    def set_payload(self, **_kwargs):
        pass


def _recall_store(points):
    """Ein MemoryStore ohne Netz: nur der Aehnlichkeits-Rueckfall ist scharf."""
    import nexus_memory.mcp_server as mcp

    store = object.__new__(mcp.MemoryStore)
    store.client = _RecallClient(points)      # type: ignore[assignment]
    store._hybrid_retriever = None            # → Rueckfallpfad mit Qdrant-Filter
    store._skill_graph = None
    store._scope_centroids = None
    store.vector_name = None
    store._update_check_result = None
    store._update_nudged_at = 0.0

    async def _embed(_text, is_query=True):
        return [0.1] * 4

    store._embed = _embed
    return store


def _point(pid: str, text: str, *, scope=None, category="fact",
           access_level="public") -> _Point:
    payload = {"id": pid, "content": text, "category": category,
               "access_level": access_level}
    if scope is not None:
        payload["scope"] = scope
    return _Point(pid, payload)


def _as_agent(monkeypatch, agent: str) -> None:
    """Der Verbund des Aufrufers kommt aus der Agenten-Kennung."""
    import nexus_memory.mcp_server as mcp

    monkeypatch.delenv("NEXUS_AGENT_ID", raising=False)
    monkeypatch.setattr(mcp, "_request_agent_id", lambda: agent)


# ===========================================================================
# 1. Die drei Sichtbarkeits-Bedingungen
# ===========================================================================

class TestSichtbarkeit:
    def test_punkt_im_gemeinsamen_bereich_liest_jeder_verbund(self):
        import nexus_memory.mcp_server as mcp

        for bundle in ("hermes", "openclaw", "deepseek-harness"):
            assert mcp._scope_visible({"scope": _SHARED}, bundle) is True

    def test_eigener_verbund_sichtbar_fremder_nicht(self):
        import nexus_memory.mcp_server as mcp

        payload = {"scope": "hermes"}
        assert mcp._scope_visible(payload, "hermes") is True
        assert mcp._scope_visible(payload, "deepseek-harness") is False

    def test_punkt_ohne_scope_feld_bleibt_fuer_alle_sichtbar(self):
        """Altbestand: kein Feld (und kein Payload) ist wie 'default'."""
        import nexus_memory.mcp_server as mcp

        assert mcp._scope_visible({}, "deepseek-harness") is True
        assert mcp._scope_visible({"scope": None}, "deepseek-harness") is True
        assert mcp._scope_visible({"scope": ""}, "deepseek-harness") is True
        assert mcp._scope_visible(None, "deepseek-harness") is True

    def test_leere_grossschreibung_zaehlt_wie_geschrieben(self):
        """Ein gespeicherter Wert wird klein verglichen, nie umgedeutet."""
        import nexus_memory.mcp_server as mcp

        assert mcp._scope_visible({"scope": " Hermes "}, "hermes") is True
        assert mcp._scope_visible({"scope": "Fremd"}, "hermes") is False


class TestQdrantFilter:
    def test_filter_traegt_die_drei_bedingungen_als_should(self):
        import nexus_memory.mcp_server as mcp

        flt = mcp._scope_filter("deepseek-harness")

        # Nur ``should`` — kein ``must``: die drei Bedingungen sind eine ODER-
        # Liste, und der Zugriffsstufen-Filter bleibt davon unberuehrt.
        assert flt.must in (None, [])
        assert flt.should and len(flt.should) == 3

        werte = [c.match.value for c in flt.should
                 if getattr(c, "match", None) is not None]
        assert werte == [_SHARED, "deepseek-harness"]

        leer = [c for c in flt.should if getattr(c, "is_empty", None) is not None]
        assert len(leer) == 1
        assert leer[0].is_empty.key == "scope"

    def test_verbund_kommt_aus_der_agenten_kennung(self, monkeypatch):
        import nexus_memory.mcp_server as mcp

        _as_agent(monkeypatch, "dsh.harness")
        # Punkt/Unterstrich werden auf '-' abgebildet, damit die Kennung nicht
        # stillschweigend im gemeinsamen Bereich landet.
        assert mcp._caller_bundle() == "dsh-harness"


# ===========================================================================
# 2. recall(): fremde Protokolle kommen nicht zurueck
# ===========================================================================

class TestRecall:
    @pytest.mark.asyncio
    async def test_recall_sieht_gemeinsames_und_eigenes_nicht_fremdes(self, monkeypatch):
        store = _recall_store([
            _point("p-default", "gemeinsamer Fakt", scope=_SHARED),
            _point("p-eigen", "eigenes Protokoll", scope="deepseek-harness",
                   category="session"),
            _point("p-fremd", "fremdes Protokoll", scope="hermes", category="session"),
            _point("p-alt", "Altbestand ohne Bereich"),
        ])
        _as_agent(monkeypatch, "deepseek-harness")

        results = await store.recall("irgendwas", agent_level="public", limit=10)
        ids = {r["id"] for r in results}

        assert ids == {"p-default", "p-eigen", "p-alt"}

    @pytest.mark.asyncio
    async def test_recall_gibt_den_sichtbarkeitsfilter_an_qdrant(self, monkeypatch):
        store = _recall_store([])
        _as_agent(monkeypatch, "deepseek-harness")

        await store.recall("irgendwas", agent_level="public", limit=5)

        assert store.client.filters, "recall muss Qdrant einen Filter mitgeben"
        flt = store.client.filters[0]
        assert flt is not None
        assert len(flt.should) == 3

    @pytest.mark.asyncio
    async def test_zugriffsstufe_wirkt_unabhaengig(self, monkeypatch):
        """Ein privater Fakt im gemeinsamen Bereich bleibt fuer fremde Stufen
        unsichtbar; ein oeffentliches Protokoll im Verbund bleibt im Verbund."""
        store = _recall_store([
            _point("p-privat", "privater gemeinsamer Fakt", scope=_SHARED,
                   access_level="private"),
            _point("p-protokoll", "oeffentliches Protokoll", scope="deepseek-harness",
                   category="session", access_level="public"),
        ])
        _as_agent(monkeypatch, "deepseek-harness")

        # Fremder Verbund, hohe Stufe: das Protokoll des anderen Verbunds fehlt
        # (nicht vorhanden), der private Fakt ebenso (Stufe).
        fremd = await store.recall("x", agent_level="private", limit=10)
        assert {r["id"] for r in fremd} == {"p-privat", "p-protokoll"}

        # Eigener Verbund, niedrige Stufe: das oeffentliche Protokoll ist da,
        # der private Fakt nicht.
        eigen = await store.recall("x", agent_level="public", limit=10)
        assert {r["id"] for r in eigen} == {"p-protokoll"}

    @pytest.mark.asyncio
    async def test_oeffentliches_protokoll_bleibt_im_verbund(self, monkeypatch):
        points = [
            _point("p-protokoll", "oeffentliches Protokoll", scope="hermes",
                   category="session", access_level="public"),
        ]
        _as_agent(monkeypatch, "deepseek-harness")
        fremd = await _recall_store(points).recall("x", agent_level="public", limit=10)
        assert fremd == []

        _as_agent(monkeypatch, "hermes")
        eigen = await _recall_store(points).recall("x", agent_level="public", limit=10)
        assert [r["id"] for r in eigen] == ["p-protokoll"]


# ===========================================================================
# 3. Schreiben: session in den Verbund, alles andere gemeinsam
# ===========================================================================

def _write_store(monkeypatch, captured: dict):
    """Ein Store, dessen Schreibvorgang den Payload mitschreibt."""
    import nexus_memory.mcp_server as mcp
    import nexus_memory.collection_vectors as collection_vectors

    class _FakeClient:
        def query_points(self, **_kwargs):          # Auto-Supersession
            return types.SimpleNamespace(points=[])

        def set_payload(self, **_kwargs):
            pass

    store = object.__new__(mcp.MemoryStore)
    store.client = _FakeClient()                    # type: ignore[assignment]
    store._skill_graph = None
    store._scope_centroids = None
    store.vector_name = None

    async def _embed(_text, is_query=True):
        return [0.0] * 8

    store._embed = _embed
    monkeypatch.setattr(
        collection_vectors, "upsert_named",
        lambda _client, _collection, pts, _vector_name, **_kw: captured.update(pts[0].payload),
    )
    monkeypatch.setattr(mcp, "_index_written_point", lambda _payload: None)
    monkeypatch.setattr(mcp, "_count_memory_access", lambda **_kw: None)
    return store


class TestSchreiben:
    @pytest.mark.asyncio
    async def test_session_schreibt_in_den_eigenen_verbund(self, monkeypatch):
        captured: dict = {}
        store = _write_store(monkeypatch, captured)
        _as_agent(monkeypatch, "deepseek-harness")

        await store.remember("User: woran arbeiten wir?\nAssistant: am Verteiler",
                             "private", "session", source="chat")

        assert captured["scope"] == "deepseek-harness"

    @pytest.mark.asyncio
    async def test_fakt_schreibt_in_den_gemeinsamen_bereich(self, monkeypatch):
        captured: dict = {}
        store = _write_store(monkeypatch, captured)
        _as_agent(monkeypatch, "deepseek-harness")

        await store.remember("Der Verteiler steht im Flur", "public", "fact")

        assert captured["scope"] == _SHARED

    @pytest.mark.asyncio
    async def test_ausdruecklicher_bereich_gewinnt(self, monkeypatch):
        captured: dict = {}
        store = _write_store(monkeypatch, captured)
        _as_agent(monkeypatch, "deepseek-harness")

        await store.remember("Der Verteiler steht im Flur", "public", "fact",
                             scope="haushalt")

        assert captured["scope"] == "haushalt"


# ===========================================================================
# 4. Vorauswahl im Plugin: der eingespielte Block bleibt im Verbund
# ===========================================================================

class _FakeQdrant:
    def __init__(self, points):
        self._points = points
        self.filters: list = []

    def query_points(self, **_kwargs):
        self.filters.append(_kwargs.get("query_filter"))
        return types.SimpleNamespace(points=list(self._points))


def _prefetch_provider(monkeypatch, points):
    """Ein Provider-Gerippe, das nur die Vorauswahl faehrt."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "nexus_plugin_scope_visibility_test",
        str(Path(__file__).resolve().parents[1]
            / "plugins" / "memory" / "nexus" / "__init__.py"),
    )
    plug = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("nexus_plugin_scope_visibility_test", plug)
    spec.loader.exec_module(plug)

    monkeypatch.setenv("NEXUS_PREFETCH_CHARS", "2400")

    prov = object.__new__(plug.NexusMemoryProvider)
    prov._prefetch_result = ""
    prov._prefetch_lock = threading.Lock()
    prov._collection = "nexus"
    prov._scope_centroids = None

    class _Embedder:
        backend = "test"

        def embed_cached(self, _text):
            return [0.0] * 8

        def embed(self, _text, is_query=True):
            return [0.0] * 8

    prov._embedder = _Embedder()
    prov._qdrant = _FakeQdrant(points)
    prov._graph_boost = lambda pts, max_boost=3, max_depth=2: []
    return prov, plug


def _prefetch_result(prov) -> str:
    prov._do_prefetch("woran arbeiten wir")
    with prov._prefetch_lock:
        return prov._prefetch_result


class TestVorauswahl:
    def test_fremdes_protokoll_steht_nicht_im_block(self, monkeypatch):
        monkeypatch.setenv("NEXUS_AGENT_ID", "deepseek-harness")
        # 20.11.2026: Der Vorlauf laesst Sitzungsprotokolle standardmaessig
        # draussen. Dieser Test prueft die SICHTBARKEITSREGEL (eigenes
        # Protokoll ja, fremdes nein) — dafuer muss die Kategorieauswahl aus.
        monkeypatch.setenv("NEXUS_PREFETCH_EXCLUDE_CATEGORIES", "")
        prov, _ = _prefetch_provider(monkeypatch, [
            _Point("a", {"content": "gemeinsamer Fakt zur Arbeit", "category": "fact",
                         "scope": _SHARED}),
            _Point("b", {"content": "eigenes Sitzungsprotokoll", "category": "session",
                         "scope": "deepseek-harness"}),
            _Point("c", {"content": "fremdes Sitzungsprotokoll hermes", "category": "session",
                         "scope": "hermes"}),
            _Point("d", {"content": "Altbestand ohne Bereichsangabe", "category": "fact"}),
        ])

        result = _prefetch_result(prov)

        assert "gemeinsamer Fakt zur Arbeit" in result
        assert "eigenes Sitzungsprotokoll" in result
        assert "Altbestand ohne Bereichsangabe" in result
        assert "fremdes Sitzungsprotokoll hermes" not in result

    def test_vorauswahl_gibt_den_sichtbarkeitsfilter_an_qdrant(self, monkeypatch):
        monkeypatch.setenv("NEXUS_AGENT_ID", "deepseek-harness")
        prov, _ = _prefetch_provider(monkeypatch, [])

        _prefetch_result(prov)

        assert prov._qdrant.filters, "prefetch muss Qdrant einen Filter mitgeben"
        assert len(prov._qdrant.filters[0].should) == 3

    def test_eigener_verbund_wird_aus_der_kennung_gebildet(self, monkeypatch):
        monkeypatch.setenv("NEXUS_AGENT_ID", "dsh.harness")
        _, plug = _prefetch_provider(monkeypatch, [])
        assert plug._own_bundle() == "dsh-harness"

    def test_plugin_schreibt_protokolle_in_den_eigenen_verbund(self, monkeypatch):
        """Auto-Capture ist der Weg, auf dem die Protokolle entstehen."""
        monkeypatch.setenv("NEXUS_AGENT_ID", "deepseek-harness")
        monkeypatch.delenv("NEXUS_SCOPE", raising=False)
        _, plug = _prefetch_provider(monkeypatch, [])

        class _Provider(plug.NexusMemoryProvider):
            pass

        captured: dict = {}

        def _capture(_client, _collection, pts, _vector_name, **_kwargs):
            captured.update(pts[0].payload)

        import nexus_memory.collection_vectors as collection_vectors
        monkeypatch.setattr(collection_vectors, "upsert_named", _capture)
        monkeypatch.setattr(plug, "_index_written_point", lambda _payload, _collection: None)
        monkeypatch.setattr(plug, "qmodels", types.SimpleNamespace(
            PointStruct=lambda **kw: types.SimpleNamespace(**kw)))

        prov = object.__new__(plug.NexusMemoryProvider)
        prov._collection = "nexus"
        prov.vector_name = None

        class _Embedder:
            def embed(self, _text, is_query=False):
                return [0.0] * 8

        prov._embedder = _Embedder()
        prov._qdrant = object()

        prov._upsert("User: hallo\nAssistant: guten Tag", category="session",
                     access_level="private")

        assert captured["scope"] == "deepseek-harness"
