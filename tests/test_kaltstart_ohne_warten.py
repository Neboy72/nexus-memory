"""Der Turn wartet nicht, und der Wirt laedt keine eigene Modellkopie (26.11.2026).

Zwei Befunde aus dem Betrieb, beide am 26.11. live gemessen:

A) Nach JEDEM neuen Agent-Prozess stand im Protokoll „Coldstart prefetch still
   running after 7.5s" — die Frist lief im TURN, der Turn zahlte sie und bekam
   danach einen leeren Merkblock (agent.log 10:43:03, 10:47:15; sechs
   Ladevorgaenge an einem Tag). Neu: der Turn startet den Nachlauf und gibt
   sofort leer zurueck; die Frist gilt nur noch im Hintergrund
   (Waechter-Faden), und der naechste Turn nimmt das Ergebnis.

B) Drei Prozesse hielten die Modelldatei der Einbettung offen, darunter der
   Hermes-Agent selbst mit 3,3 GB. Ursache: der Wirt baut zwar fuer seine
   eigenen Vektoren den Dienst, aber ``EdgeStore.initialize()`` erzeugte
   zusaetzlich einen eigenen ``EmbeddingProvider()`` im Agent-Prozess. Der
   Wirt gibt jetzt den Vektornamen mit, den er ohnehin kennt; dann wird kein
   Einbetter gebaut.
"""
from __future__ import annotations

import logging
import threading
import time

import pytest

import plugins.memory.nexus as nx
from plugins.memory.nexus import NexusMemoryProvider


_WAIT = 10.0


def _provider() -> NexusMemoryProvider:
    p = NexusMemoryProvider.__new__(NexusMemoryProvider)
    p._prefetch_result = ""
    p._prefetch_prepared = False
    p._prefetch_followup_pending = False
    p._prefetch_lock = threading.Lock()
    p._prefetch_gate = threading.Lock()
    p._prefetch_thread_lock = threading.Lock()
    p._coldstart_lock = threading.Lock()
    p._coldstart_thread = None
    p._shutting_down = False
    return p


# ── A) Kein Warten im Turn ──────────────────────────────────────────────────

def test_turn_wartet_nicht_auf_den_kaltstart():
    """Ein langsamer Nachlauf haelt den Turn nicht auf; der naechste nimmt ihn."""
    p = _provider()
    started = threading.Event()
    release = threading.Event()

    def slow(query, gate=None):
        started.set()
        assert release.wait(_WAIT)
        with p._prefetch_lock:
            p._prefetch_result = "[fact] spaeter Treffer"
            p._prefetch_prepared = True
            p._prefetch_followup_pending = False

    p._do_prefetch_inner = slow

    t0 = time.monotonic()
    assert p.prefetch("Was war der letzte Stand?") == ""
    elapsed = time.monotonic() - t0

    assert elapsed < 0.3, f"der Turn wartete {elapsed:.2f}s auf den Kaltstart"
    assert started.wait(_WAIT), "der Nachlauf wurde nie gestartet"
    assert p._prefetch_followup_pending is True, \
        "der offene Nachlauf wurde nicht vermerkt"

    release.set()
    p._coldstart_thread.join(_WAIT)
    assert p.prefetch("Was war der letzte Stand?") == "[fact] spaeter Treffer"


def test_frist_gilt_nur_noch_im_hintergrund(monkeypatch, caplog):
    """Nach der Frist tauscht der Waechter das Gate — ohne dass der Turn wartet."""
    monkeypatch.setenv("NEXUS_COLDSTART_TIMEOUT", "0.5")
    p = _provider()
    gate_before = p._prefetch_gate
    stuck = threading.Event()

    def stuck_inner(query, gate=None):
        stuck.wait(_WAIT)          # haengt bis zum Testende

    p._do_prefetch_inner = stuck_inner

    t0 = time.monotonic()
    with caplog.at_level(logging.WARNING, logger=nx.__name__):
        assert p.prefetch("Was war der letzte Stand?") == ""
    elapsed = time.monotonic() - t0
    assert elapsed < 0.3, f"der Turn wartete {elapsed:.2f}s"

    deadline = time.time() + 3.0
    while time.time() < deadline and p._prefetch_gate is gate_before:
        time.sleep(0.02)
    assert p._prefetch_gate is not gate_before, \
        "der Waechter hat das Gate nach der Frist nicht ersetzt"
    assert any("the turn did not wait" in r.getMessage()
               for r in caplog.records), [r.getMessage() for r in caplog.records]
    stuck.set()


# ── B) Der Wirt laedt keine eigene Modellkopie ──────────────────────────────

def test_wirt_waehlt_den_dienst_und_laedt_kein_modell(monkeypatch):
    """Wirt und Graph-Store kommen ohne EmbeddingProvider im Agent-Prozess aus."""
    import nexus_memory.embeddings as engine_embeddings

    monkeypatch.delenv("NEXUS_EMBED_IN_HOST", raising=False)
    monkeypatch.delenv("NEXUS_EMBED_SERVICE_URL", raising=False)
    monkeypatch.setattr(nx, "_ensure_embed_service", lambda: (True, None))

    def _boom(*args, **kwargs):
        raise AssertionError(
            "der Wirt hat EmbeddingProvider() gebaut — das ist die zweite "
            "Modellkopie im Agent-Prozess")

    monkeypatch.setattr(engine_embeddings, "EmbeddingProvider", _boom)

    # 1. Der Einbetter des Plugins nimmt den Dienst.
    emb = nx._Embedder()
    assert emb._impl is None, "der Wirt hat sich eine eigene Modellkopie gebaut"

    # 2. Der Graph-Store baut keinen Einbetter, wenn der Wirt den Vektornamen mitgibt.
    from nexus.graph.store import EdgeStore
    store = EdgeStore(client=object(), collection="nexus_hf",
                      vector_name="sentence-transformers__qwen__1024")
    assert store.initialize() is True


def test_skill_graph_bekommt_den_vektornamen_vom_wirt(monkeypatch):
    """_get_skill_graph gibt den bereits entdeckten Vektornamen weiter."""
    p = NexusMemoryProvider.__new__(NexusMemoryProvider)
    p._skill_graph = None
    p._skill_graph_lock = threading.Lock()
    p.vector_name = "sentence-transformers__qwen_qwen3-embedding-0_6b__1024"
    p._collection = "nexus_hf"
    seen = {}

    class FakeSkillGraph:
        def __init__(self, **kwargs):
            seen.update(kwargs)

        def initialize(self):
            return True

    monkeypatch.setattr("nexus.graph.graph.SkillGraph", FakeSkillGraph)
    p._get_skill_graph()

    assert seen.get("vector_name") == p.vector_name, (
        "der Graph-Store bekam den Vektornamen nicht — dann baut er sich "
        "selbst einen Einbetter und laedt das Modell im Agent-Prozess")
