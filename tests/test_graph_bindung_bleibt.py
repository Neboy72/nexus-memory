"""Wo wird die Sammlung angelegt und geprueft? (26.11.2026)

Inspector hat an 0.22.74 einen Widerspruch gefunden: ``SkillGraph`` hatte den
Default ``vector_name=None``. Weil ``None is not _NO_VECTOR_NAME`` gilt, landete
damit JEDER Aufrufer ohne Vektornamen im Wirtszweig — bei ihm fielen sowohl
``EmbeddingProvider()`` als auch ``binding.ensure()`` weg. Die vier Graph-
Werkzeuge des Plugins riefen ``SkillGraph`` ohne Namen auf, also lief die
Sammlungspruefung in diesen Prozessen nicht mehr.

Diese Datei haelt beide Seiten fest:
  1. Ohne Wirtsnamen verhaelt sich alles exakt wie vorher (Einbetter + Bindung
     + ``ensure()``) — bei ``EdgeStore`` UND bei ``SkillGraph``.
  2. Mit Wirtsnamen wird kein Einbetter gebaut und ``ensure()`` laeuft nicht:
     die Sammlung gehoert dem Dienst. Angelegt und geprueft wird sie dort, in
     ``nexus_memory.mcp_server.MemoryStore.__init__`` — der einzige Ort, der
     den Dienst ausmacht.
  3. Die vier Plugin-Werkzeuge geben den Namen ausdruecklich mit.
"""
from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

import plugins.memory.nexus as _nx
from plugins.memory.nexus import NexusMemoryProvider


def _fake_engine(monkeypatch):
    """Ersetzt den Einbetter und die Bindung der Engine durch Mitschreiber."""
    import nexus_memory.collection_vectors as cv
    import nexus_memory.embeddings as em

    seen = {"provider": 0, "ensure": 0, "collections": []}

    class _Provider:
        def __init__(self, *a, **k):
            seen["provider"] += 1
            self.backend = "sentence-transformers"
            self.model_name = "qwen"
            self.dim = 1024
            self.available = True

    class _Binding:
        def __init__(self, client, collection, provider, **kw):
            self.provider = provider
            seen["collections"].append(collection)

        def ensure(self):
            seen["ensure"] += 1
            return self

        def vector_name(self):
            return "sentence-transformers__qwen__1024"

    monkeypatch.setattr(em, "EmbeddingProvider", _Provider)
    monkeypatch.setattr(cv, "CollectionBinding", _Binding)
    return seen


# ── 1. Ohne Wirtsnamen: alles wie vorher ────────────────────────────────────

def test_ohne_wirtsnamen_baut_edge_store_einbetter_und_bindet(monkeypatch):
    from nexus.graph.store import EdgeStore

    seen = _fake_engine(monkeypatch)
    store = EdgeStore(client=object(), collection="nexus_hf")
    assert store.initialize() is True

    assert seen["provider"] == 1, "ohne Wirtsnamen muss ein Einbetter entstehen"
    assert seen["ensure"] == 1, "ohne Wirtsnamen muss ensure() laufen"


def test_skill_graph_ohne_vektornamen_reicht_den_sentinel_weiter(monkeypatch):
    """Der Default von SkillGraph darf den Sentinel nicht durch None ersetzen.

    ``None`` waere fuer den EdgeStore ein ausdruecklicher Wirtsname
    (``None is not _NO_VECTOR_NAME``) und liesse bei jedem Aufrufer ohne Namen
    Einbetter UND ``ensure()`` verschwinden. Der Test haelt genau die
    Weitergabe fest; dass der Sentinel beim Store wieder Einbetter + ``ensure()``
    bedeutet, zeigt ``test_ohne_wirtsnamen_baut_edge_store_einbetter_und_bindet``.
    """
    import nexus.graph.store as store_mod

    seen = {}

    class _Recorder:
        def __init__(self, **kwargs):
            seen.update(kwargs)

    monkeypatch.setattr("nexus.graph.graph.EdgeStore", _Recorder)
    from nexus.graph.graph import SkillGraph

    SkillGraph(qdrant_url="http://127.0.0.1:6333", collection="nexus_hf")

    assert seen.get("vector_name") is store_mod._NO_VECTOR_NAME, (
        "SkillGraph ohne Vektornamen reicht nicht den Sentinel weiter — beim "
        "Store faellt dann Einbetter und ensure() weg")


# ── 2. Mit Wirtsnamen: kein Einbetter, und ensure() gehoert dem Dienst ─────

def test_skill_graph_mit_wirtsnamen_baut_keinen_einbetter(monkeypatch):
    from nexus.graph.store import EdgeStore

    seen = _fake_engine(monkeypatch)
    store = EdgeStore(client=object(), collection="nexus_hf",
                      vector_name="sentence-transformers__qwen__1024")
    assert store.initialize() is True

    assert seen["provider"] == 0, "der Wirt hat eine eigene Modellkopie gebaut"
    assert seen["ensure"] == 0, "ensure() laeuft im Wirt und soll es nicht"


def test_die_sammlung_prueft_der_dienst(monkeypatch):
    """Genannter Ort im neuen Stand: MemoryStore des Dienstes legt an/prueft."""
    import nexus_memory.mcp_server as mcp

    seen = {"provider": 0, "ensure": 0}

    class _Provider:
        def __init__(self, *a, **k):
            seen["provider"] += 1

    class _Binding:
        def __init__(self, client, collection, provider, **kw):
            self.provider = provider

        def ensure(self):
            seen["ensure"] += 1
            return self

        def vector_name(self):
            return "sentence-transformers__qwen__1024"

    monkeypatch.setattr(mcp, "EmbeddingProvider", _Provider)
    monkeypatch.setattr(mcp, "CollectionBinding", _Binding)
    monkeypatch.setattr(mcp, "QdrantClient", lambda **kw: object())

    store = mcp.MemoryStore()

    assert seen["provider"] == 1
    # Der Dienst bindet an zwei Stellen (eigener Store und Skill-Graph) —
    # beides derselbe Prozess; entscheidend ist, dass er es ueberhaupt tut.
    assert seen["ensure"] >= 1, "der Dienst prueft die Sammlung nicht mehr"
    assert store.vector_name == "sentence-transformers__qwen__1024"


# ── 3. Die vier Plugin-Werkzeuge geben den Namen mit ───────────────────────

def _provider_for_tools() -> NexusMemoryProvider:
    p = NexusMemoryProvider.__new__(NexusMemoryProvider)
    p._qdrant = object()
    p._collection = "nexus_hf"
    p.vector_name = "sentence-transformers__qwen_qwen3-embedding-0_6b__1024"
    p._agent_context = "primary"
    return p


@pytest.mark.parametrize("method,args", [
    ("_graph_traverse", ("abc",)),
    ("_find_entities", ()),
    ("_get_subgraph", ("abc",)),
    ("_get_related", ("abc",)),
])
def test_die_graph_werkzeuge_geben_den_vektornamen_mit(monkeypatch, method, args):
    """Ohne den Namen baute jedes Werkzeug einen Einbetter im Agent-Prozess."""
    p = _provider_for_tools()
    seen = {}

    class _FakeSkillGraph:
        def __init__(self, **kwargs):
            seen.update(kwargs)
            self.store = SimpleNamespace(close=lambda: None)

        def initialize(self):
            raise RuntimeError("stop after recording")

    monkeypatch.setattr("nexus.graph.graph.SkillGraph", _FakeSkillGraph)
    getattr(p, method)(*args)

    assert seen.get("vector_name") == p.vector_name, (
        f"{method} gibt den Vektornamen nicht mit — dann baut der Graph-Store "
        "dort seinen eigenen Einbetter")
