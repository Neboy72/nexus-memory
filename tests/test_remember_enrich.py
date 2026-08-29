"""Tests for roadmap 1.1/4.1: nexus_remember auto entity enrichment.

nexus_remember queues an entity-extraction pass in a daemon thread
(fail-open, single-flight). Short texts are skipped; long texts extract
entities via entity_extractor and store graph edges.
"""

from __future__ import annotations

import importlib.util
import sys
import threading
import time
from pathlib import Path
import pytest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC = str(_REPO_ROOT / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

_PLUGIN_PATH = _REPO_ROOT / "plugins" / "memory" / "nexus" / "__init__.py"
_spec = importlib.util.spec_from_file_location("nexus_hermes_plugin_enrich", str(_PLUGIN_PATH))
_nexus_plugin = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_nexus_plugin)

NexusMemoryProvider = _nexus_plugin.NexusMemoryProvider


def _provider():
    prov = NexusMemoryProvider.__new__(NexusMemoryProvider)
    prov._collection = "c"
    prov._rerank_cfg = {"enabled": False}
    prov._rerank_lock = threading.Lock()
    prov._skill_graph = None
    prov._skill_graph_lock = threading.Lock()
    prov._prefetch_result = ""
    prov._prefetch_lock = threading.Lock()
    prov._entity_extract_lock = threading.Lock()
    prov._hermes_home = ""
    prov._write_stop = threading.Event()
    prov._embedder = SimpleNamespace(dim=1024, embed=lambda t: [0.0] * 1024)
    client = MagicMock()
    client.query_points.return_value = SimpleNamespace(points=[])
    prov._qdrant = client
    return prov, client


def test_short_text_skipped():
    prov, _ = _provider()
    prov._enqueue_entity_extraction("kurz")
    # single-flight lock must be free afterwards (thread skipped)
    assert prov._entity_extract_lock.acquire(blocking=False)
    prov._entity_extract_lock.release()


def test_enrich_runs_extraction_and_stores(monkeypatch):
    from nexus_memory import entity_extractor as ee

    prov, client = _provider()

    fake = ee.ExtractionResult(entities=[ee.Entity("TestDevice", "device", {"ip": "1.2.3.4"})], relationships=[])

    monkeypatch.setattr(_nexus_plugin, "_HOST", "localhost")
    monkeypatch.setattr(_nexus_plugin, "_PORT", "6333")

    stored = []
    monkeypatch.setattr(prov, "_upsert_entity",
                        lambda e, **kw: (stored.append(e), {"id": "ent-" + e.name.replace(" ", "_")})[1])

    edges = []
    class _FakeEdgeStore:
        def __init__(self, **kw): pass
        def add_edge(self, **kw): edges.append(kw)
        def close(self): pass
    import nexus.graph.store as gs
    monkeypatch.setattr(gs, "EdgeStore", _FakeEdgeStore)

    # extract_entities is imported inside the function from the module:
    monkeypatch.setattr("nexus_memory.entity_extractor.extract_entities",
                        lambda t, hermes_home=None: fake)

    prov._enqueue_entity_extraction(
        "Our wallbox model TestDevice sits at ip 1.2.3.4 in the garage "
        "and its backend uses the OCPP 1.6 protocol for charging sessions."
    )
    # wait for the daemon thread
    deadline = time.time() + 5
    while not stored and time.time() < deadline:
        time.sleep(0.05)
    assert stored and stored[0].name == "TestDevice"


def test_enrich_fail_open(monkeypatch):
    prov, _ = _provider()
    def boom(*a, **k):
        raise RuntimeError("extractor down")
    monkeypatch.setattr("nexus_memory.entity_extractor.extract_entities", boom)
    prov._enqueue_entity_extraction(
        "A long enough text to pass the noise guard threshold for entity extraction."
    )
    deadline = time.time() + 5
    while not prov._entity_extract_lock.acquire(blocking=False) and time.time() < deadline:
        time.sleep(0.05)
    prov._entity_extract_lock.release()  # lock released by the thread = fail-open ok


def test_single_flight_lock_held_skips():
    """Review nit: enqueue while lock held must return immediately (no queue)."""
    prov, _ = _provider()
    prov._entity_extract_lock = threading.Lock()
    if not prov._entity_extract_lock.acquire(blocking=False):
        pytest.skip("could not pre-acquire lock")
    fake_calls = []
    prov._extract_entities_from_text = lambda *a, **k: fake_calls.append(1) or {}
    try:
        # should not raise or block
        prov._enqueue_entity_extraction("A" * 100)
    finally:
        prov._entity_extract_lock.release()
        # thread may have died pre-spawn; make sure it's really dead
        time.sleep(0.2)
    # lock-held: no extraction thread ran
    assert not fake_calls


def test_duplicate_text_hash_skips():
    """Efficiency review: same text twice - second is a no-op (hash dedup)."""
    calls = []
    prov, _ = _provider()
    def fake_extract(text, source="nexus_remember", access_level="public"):
        calls.append(text)
        return {"entities": 1, "edges": 0}
    prov._extract_entities_from_text = fake_extract
    prov._enqueue_entity_extraction("Unique text for the hash dedup test " * 2)
    # daemon thread needs a beat
    deadline = time.time() + 3
    while not calls and time.time() < deadline:
        time.sleep(0.05)
    n = len(calls)
    prov._enqueue_entity_extraction("Unique text for the hash dedup test " * 2)
    time.sleep(0.3)
    assert len(calls) == n  # dedup: no second call


def test_relationship_edge_stored():
    """Review nit: EdgeStore path is exercised with a relationship."""
    from types import SimpleNamespace
    import nexus_memory.entity_extractor as ee
    prov, _q = _provider()
    monkeypatch = None  # pattern: local monkeypatch via patch()

    fake = SimpleNamespace(
        entities=[
            ee.Entity("RelDevice", "device", {"ip": "9.9.9.9"}),
            ee.Entity("OtherDev", "device", {"role": "backend"}),
        ],
        relationships=[ee.Relationship("RelDevice", "OtherDev", "connected_to")],
    )
    fake.is_empty = lambda: not fake.entities and not fake.relationships

    edges = []
    class _FakeEdgeStore:
        def __init__(self, **kw): pass
        def add_edge(self, **kw): edges.append(kw)
        def close(self): pass

    with patch("nexus_memory.entity_extractor.extract_entities", lambda t, hermes_home=None: fake):
        with patch("nexus.graph.store.EdgeStore", _FakeEdgeStore):
            prov._extract_entities_from_text(
                "RelDevice at 9.9.9.9 connects to OtherDev via connected_to relation here."
            )
    assert len(edges) == 1
    assert edges[0]["relation"] == "connected_to"
    assert edges[0]["reason"] == "nexus_remember"


def test_second_recall_uses_cache():
    prov, _client = _provider()
    prov._embed_cache = None  # tests bypass __init__
    """L0: 2. recall mit gleichem Query = kein zweiter Embed-Call."""
    import importlib.util as ilu
    prov = _provider()[0]
    calls = {"n": 0}
    class _E:
        dim = 1024
        def embed(self, t):
            calls["n"] += 1
            return [0.1] * 1024
    prov._embedder = _E()
    prov._recall("gleiche frage", limit=2)
    prov._recall("gleiche frage", limit=2)
    prov._recall("gleiche frage", limit=2)
    assert calls["n"] == 1  # 1x embed, 2x cache hit
    assert prov._get_embed_cache().hit_rate >= 0.6


def test_prefetch_respects_budget():
    """L1: prefetch total stays under NEXUS_PREFETCH_CHARS."""
    prov, _c = _provider()
    prov._embed_cache = None
    import os
    os.environ["NEXUS_PREFETCH_CHARS"] = "400"
    try:
        class _P:
            id = "x"; score = 0.9
            payload = {"content": "A" * 300 + " " + "B" * 300, "category": "fact"}

        class _C:
            def query_points(self, **kw):
                return SimpleNamespace(points=[_P(i) for i in range(4)])
        class _P:
            def __init__(self, i):
                self.id = f"p{i}"; self.score = 0.95 - i * 0.1
                self.payload = {"content": ("word " * 120) + str(i), "category": "fact"}
        prov._qdrant = _C()
        prov._do_prefetch("test budget query")
    finally:
        del os.environ["NEXUS_PREFETCH_CHARS"]
    assert len(prov._prefetch_result) <= 450  # budget + overhead
