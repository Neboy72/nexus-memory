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


def test_flywheel_bumps_access_count():
    """4.9: recall erhöht access_count der Top-3-Treffer (fire-and-forget)."""
    import time as _t
    prov, _client = _provider()
    prov._embed_cache = None
    bumps = {}
    use_bumps = {}
    class _P:
        def __init__(self, pid):
            self.id = pid
            self.score = 0.9
            self.payload = {"id": pid, "content": f"fact {pid}", "category": "fact",
                            "access_count": 5, "use_count": 7}
    store = {"p1": _P("p1"), "p2": _P("p2"), "p3": _P("p3"), "p4": _P("p4")}
    class _C:
        def query_points(self, **kw):
            return SimpleNamespace(points=list(store.values()))
        def retrieve(self, **kw):
            pid = kw["ids"][0]
            return [store[pid]] if pid in store else []
        def set_payload(self, **kw):
            bumps[kw["points"][0]] = kw["payload"]["access_count"]
            use_bumps[kw["points"][0]] = kw["payload"]["use_count"]
    prov._qdrant = _C()

    prov._recall("flywheel test query", limit=5)
    deadline = _t.time() + 3
    while len(bumps) < 3 and _t.time() < deadline:
        _t.sleep(0.05)
    assert set(bumps) >= {"p1", "p2"}  # top hits versorgt
    assert all(v >= 6 for v in bumps.values())  # access_count: 5+1
    assert all(u >= 8 for u in use_bumps.values())  # use_count: 7+1 (eigene Basis)


def test_flywheel_bump_uses_fresh_counters_not_snapshot():
    """v0.15 Review-Fix (Race): Bump liest AKTUELLEN Stand vor dem Write.

    Snapshot sagt use_count=7, aber die DB ist inzwischen bei 10 — der Bump
    muss 12 schreiben (10+1+1: frisch 11 wäre zwischen Retrieve und Write,
    hier deterministisch 11) und NICHT 8 (Snapshot+1, Lost-Update).
    """
    import time as _t
    prov, _client = _provider()
    prov._embed_cache = None
    bumps = {}
    class _P:
        def __init__(self, pid):
            self.id = pid
            self.score = 0.9
            self.payload = {"id": pid, "content": f"fact {pid}", "category": "fact",
                            "access_count": 5, "use_count": 7}
    store = {"p1": _P("p1")}
    # "DB" ist inzwischen weiter als der Snapshot: use_count=10, access_count=9
    fresh_db = {"p1": {"use_count": 10, "access_count": 9}}
    class _C:
        def retrieve(self, **kw):
            return [SimpleNamespace(id=i, payload=fresh_db.get(str(i), {}))
                    for i in kw["ids"]]
        def set_payload(self, **kw):
            bumps[kw["points"][0]] = kw["payload"]
    prov._qdrant = _C()
    prov._flywheel_bump([("p1", 7, 5, "canonical")])
    _t.sleep(0.05)  # kein Thread hier — direkter Call, synchron
    p = bumps["p1"]
    assert p["use_count"] == 11  # frisch 10+1, NICHT snapshot 7+1=8
    assert p["access_count"] == 10  # frisch 9+1, NICHT snapshot 5+1=6


def test_nexus_remember_passthrough_salience_source_url():
    """v0.15 (Verifier B2): handle_tool_call reicht salience/confidence/
    source_url wirklich an _upsert durch — Schema verspricht sie."""
    import plugins.memory.nexus as mod
    prov = object.__new__(mod.NexusMemoryProvider)
    captured = {}

    def _fake_upsert(text="", category="fact", access_level="public",
                     source="", confidence=0.7, salience=None, source_url=""):
        captured.update(text=text, category=category, access_level=access_level,
                        source=source, confidence=confidence, salience=salience,
                        source_url=source_url)
        return {"status": "ok", "id": "x"}

    prov._upsert = _fake_upsert
    prov._enqueue_entity_extraction = lambda *a, **k: None
    prov.handle_tool_call("nexus_remember", {
        "text": "Test-Fakt", "category": "rule", "salience": 0.95,
        "confidence": 0.9, "source_url": "https://example.com", "source": "Unit-Test",
    })
    assert captured["salience"] == 0.95
    assert captured["confidence"] == 0.9
    assert captured["source_url"] == "https://example.com"
    assert captured["source"] == "Unit-Test"


def test_salience_fallback_parity_with_normalize_salience():
    """_salience_fallback (MCP, import-fail-Pfad) verhält sich identisch zu
    normalize_salience (memory_dynamics) — behauptete Parität, jetzt bewiesen."""
    from nexus_memory.memory_dynamics import normalize_salience
    from nexus_memory.mcp_server import _salience_fallback
    for sal, cat in [(None, "rule"), (None, "temp"), (None, "session"),
                     (None, "fact"), (None, "procedure"), (None, "kurios"),
                     (1.7, "fact"), (-0.5, "fact"), (0.9, "fact"),
                     ("kaputt", "fact"), (0.0, "rule"), (2.0, "temp")]:
        assert _salience_fallback(sal, cat) == normalize_salience(sal, cat), \
            f"Parität verletzt bei salience={sal}, category={cat}"


def test_plugin_window_sort_windows_on_base_score():
    """v0.15 (Verifier M2): Fenster auf BASIS-Score, nicht auf eff.

    Zwei Punkte mit deutlich verschiedenem Basis-Score (0.9 vs 0.5) dürfen
    NIE umsortiert werden — auch wenn der schwächere mehr use_count hat.
    Bei fast gleichem Basis-Score (0.90 vs 0.89) entscheidet die Dynamik.
    Testet die ECHTE Provider-Methode (keine Test-Kopie des Algorithmus).
    """
    import plugins.memory.nexus as mod
    prov = object.__new__(mod.NexusMemoryProvider)
    from nexus_memory.memory_dynamics import effective_score as eff

    def _pt(pid, score, use_count):
        return SimpleNamespace(id=pid, score=score,
                               payload={"id": pid, "content": f"c {pid}",
                                        "use_count": use_count})

    # Fall 1: unterschiedliche Basis-Relevanz → Rerank-Ordnung bleibt
    pts = [_pt("stark", 0.9, 0), _pt("schwach", 0.5, 10**6)]
    out = prov._apply_dynamics_tiebreak(pts, eff)
    assert [p.id for p in out] == ["stark", "schwach"]

    # Fall 2: near-tie auf Basis (0.89 in 0.02-Fenster um 0.90) → Dynamik entscheidet
    pts = [_pt("frisch", 0.90, 0), _pt("oft", 0.89, 50)]
    out = prov._apply_dynamics_tiebreak(pts, eff)
    assert [p.id for p in out] == ["oft", "frisch"]

    # Fall 3: Grenze — außerhalb des Fensters bleibt Ordnung (0.9 vs 0.85 > EPS)
    pts = [_pt("a", 0.90, 0), _pt("b", 0.85, 50)]
    out = prov._apply_dynamics_tiebreak(pts, eff)
    assert [p.id for p in out] == ["a", "b"]
