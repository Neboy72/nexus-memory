"""Die Auswahlregel im Dienst: ``recall(categories=…, min_score=…)``.

Der Unit-Test (``test_recall_policy.py``) prueft die Regel; dieser Test prueft
die Einbindung: dass der Dienst die Schalter annimmt, dass ``relevance`` die
vergleichbare Zahl ist und dass ein leerer Block entsteht, wenn nichts passt.

Bewusst am echten ``MemoryStore.recall`` und nicht an einem nachgebauten
Ablauf: ein Fake, der selbst filtert, wuerde nur den Fake testen.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import nexus_memory.mcp_server as mcp  # noqa: E402


class _FakeClient:
    """Nur ``retrieve`` — die Anreicherung der Hybrid-Treffer."""

    def __init__(self, payloads: dict[str, dict]):
        self._payloads = payloads

    def retrieve(self, **_kwargs):
        ids = _kwargs.get("ids") or []
        return [
            SimpleNamespace(id=pid, payload=dict(self._payloads[str(pid)]))
            for pid in ids
            if str(pid) in self._payloads
        ]


class _FakeRetriever:
    """Liefert die Trefferliste, die der Test vorgibt (kein Qdrant, kein BM25)."""

    def __init__(self, hits):
        self._hits = hits

    def search(self, _query, query_vector=None, top_k=10, **_kwargs):
        return [dict(hit) for hit in self._hits]


def _payload(punkt_id: str, *, kategorie=None, typ="memory", level="private"):
    payload = {"id": punkt_id, "content": f"Inhalt {punkt_id}", "type": typ,
               "access_level": level, "created_at": "2026-11-01T10:00:00+00:00"}
    if kategorie:
        payload["category"] = kategorie
    return payload


@pytest.fixture
def store(monkeypatch):
    """``MemoryStore`` ohne Qdrant: nur der Abrufweg, den der Test braucht."""
    hits = [
        # Rang 1, schlechter Kosinus: muss unter der Schwelle fallen.
        {"id": "h-junk", "rrf_score": 0.030, "vector_score": 0.12, "text": "junk"},
        # gute Aehnlichkeit, aber Entity: faellt am Kategorienfilter.
        {"id": "h-entity", "rrf_score": 0.025, "vector_score": 0.94, "text": "spotify"},
        # Tatsache ueber der Schwelle: bleibt.
        {"id": "h-fact", "rrf_score": 0.020, "vector_score": 0.72, "text": "fakt"},
        # Regel knapp ueber der Schwelle: bleibt.
        {"id": "h-rule", "rrf_score": 0.015, "vector_score": 0.41, "text": "regel"},
        # Turn ohne Kategorie: gilt als Sitzung, faellt raus.
        {"id": "h-turn", "rrf_score": 0.014, "vector_score": 0.90, "text": "halber satz"},
        # Sitzung: faellt raus (auch mit hoher Aehnlichkeit).
        {"id": "h-session", "rrf_score": 0.010, "vector_score": 0.88,
         "text": "protokoll", "vector_override": True},
    ]
    payloads = {
        "h-junk": _payload("h-junk", kategorie="fact"),
        "h-entity": _payload("h-entity", kategorie="entity"),
        "h-fact": _payload("h-fact", kategorie="fact"),
        "h-rule": _payload("h-rule", kategorie="rule"),
        "h-turn": _payload("h-turn", typ="turn"),
        "h-session": _payload("h-session", kategorie="session"),
    }

    memory_store = object.__new__(mcp.MemoryStore)
    memory_store.vector_name = None
    memory_store._skill_graph = None
    memory_store._scope_centroids = None
    memory_store._update_check_result = None
    memory_store._update_check_time = 0
    memory_store._update_nudged_at = 0
    memory_store.client = _FakeClient(payloads)
    memory_store._hybrid_retriever = _FakeRetriever(hits)

    async def _embed(_text, is_query=True):
        return [0.1] * 4

    async def _keine_quellen(_urls):
        return {}

    memory_store._embed = _embed
    memory_store._check_sources = _keine_quellen
    # Zaehler und Nutzungsstaerke sind Schreibwege; ein Test schreibt nicht.
    monkeypatch.setattr(mcp, "_count_memory_access", lambda *a, **k: None)
    return memory_store


def _ids(treffer):
    return [t["doc_id"] for t in treffer]


# ── Die Schalter wirken ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ausschluss_wirkt(store):
    """session und entity bleiben draussen, Wissen bleibt drin."""
    treffer = await store.recall(
        "frage", agent_level="private", limit=5,
        exclude_categories=["session", "entity"],
    )
    assert _ids(treffer) == ["h-junk", "h-fact", "h-rule"]


@pytest.mark.asyncio
async def test_schwelle_wirkt_zusaetzlich(store):
    """Die Schwelle ist eine bewusst zu setzende Option, standardmaessig aus."""
    treffer = await store.recall(
        "frage", agent_level="private", limit=5,
        exclude_categories=["session", "entity"], min_score=0.35,
    )
    assert _ids(treffer) == ["h-fact", "h-rule"]


@pytest.mark.asyncio
async def test_ohne_angabe_unveraendert(store):
    """Andere Aufrufer sehen weiter alles, in derselben Reihenfolge."""
    treffer = await store.recall("frage", agent_level="private", limit=5)
    assert _ids(treffer) == [
        "h-junk", "h-entity", "h-fact", "h-rule", "h-turn",
    ]


@pytest.mark.asyncio
async def test_relevance_ist_die_vergleichbare_zahl(store):
    """``relevance`` traegt den Kosinus, nicht den normalisierten RRF-Wert."""
    treffer = await store.recall("frage", agent_level="private", limit=5)
    nach_id = {t["doc_id"]: t for t in treffer}
    assert nach_id["h-entity"]["relevance"] == pytest.approx(0.94, abs=1e-6)
    assert nach_id["h-junk"]["relevance"] == pytest.approx(0.12, abs=1e-6)
    # Der normalisierte Rang ist beim Spitzenreiter (h-junk) 1.0 — genau
    # deshalb taugt er nicht als Schwelle.
    assert nach_id["h-junk"]["score"] == pytest.approx(1.0, abs=1e-3)


@pytest.mark.asyncio
async def test_grenzfall_leerer_block_statt_fuellmaterial(store):
    """Nichts ueber der Schwelle ergibt eine leere Liste."""
    treffer = await store.recall(
        "frage", agent_level="private", limit=5,
        exclude_categories=["session", "entity"], min_score=0.99,
    )
    assert treffer == []


@pytest.mark.asyncio
async def test_unbekannte_kategorie_scheitert_laut(store):
    """Ein Tippfehler darf nicht stillschweigend alles durchlassen."""
    with pytest.raises(ValueError):
        await store.recall("frage", agent_level="private", limit=5,
                           exclude_categories=["fakten"])


@pytest.mark.asyncio
async def test_nur_woertlicher_treffer_besteht_die_schwelle(store):
    """Ein BM25-Treffer ohne Kosinus ist Evidenz, keine Luecke."""
    store._hybrid_retriever = _FakeRetriever([
        {"id": "h-lex", "rrf_score": 0.02, "bm25_score": 12.5, "text": "kennung"},
        {"id": "h-blind", "rrf_score": 0.01, "text": "ohne alles"},
    ])
    store.client = _FakeClient({
        "h-lex": _payload("h-lex", kategorie="fact"),
        "h-blind": _payload("h-blind", kategorie="fact"),
    })
    treffer = await store.recall("kennung", agent_level="private", limit=5,
                                 min_score=0.35)
    assert _ids(treffer) == ["h-lex"]


# ── Lese-Protokoll am Abrufweg ───────────────────────────────────────


def _lesezeilen() -> list[dict]:
    from nexus_memory import service_log

    tag = __import__("datetime").datetime.now(
        __import__("datetime").timezone.utc).date().isoformat()
    path = service_log.log_dir() / f"read-{tag}.jsonl"
    if not path.exists():
        return []
    return [__import__("json").loads(line)
            for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


@pytest.mark.asyncio
async def test_abruf_schreibt_eine_lesezeile_mit_den_kennungen(store):
    await store.recall("frage", agent_level="private", limit=5,
                       exclude_categories=["session", "entity"],
                       via="mcp", actor="leser")
    zeilen = _lesezeilen()
    assert len(zeilen) == 1
    assert zeilen[0]["action"] == "read"
    assert zeilen[0]["hits"] == 3
    assert zeilen[0]["ids"] == ["h-junk", "h-fact", "h-rule"]
    assert zeilen[0]["via"] == "mcp"
    # Kein Klartext der Anfrage.
    assert "frage" not in __import__("json").dumps(zeilen[0])


@pytest.mark.asyncio
async def test_leerer_abruf_schreibt_keine_zeile(store):
    treffer = await store.recall("frage", agent_level="private", limit=5,
                                 min_score=0.99)
    assert treffer == []
    assert _lesezeilen() == []
