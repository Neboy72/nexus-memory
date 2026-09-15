"""Tests for OCR review Wave 7 — HIGH findings H1–H10.

One test class per finding. Store-level tests use an in-memory fake Qdrant
client (thread-safe) so H1 can exercise a real two-thread race without
depending on embedded-Qdrant concurrency behaviour.
"""

from __future__ import annotations

import importlib.util
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from qdrant_client import models

REPO_ROOT = Path(__file__).resolve().parents[1]


def _fid(name: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, name))


F_A = _fid("w7-a")
F_B = _fid("w7-b")
F_C = _fid("w7-c")


# ── Fake Qdrant client (thread-safe, in-memory) ─────────────────────────────


class _FakePoint:
    def __init__(self, pid):
        self.id = pid
        self.payload = {}


class _FakeClient:
    """Minimal stand-in for QdrantClient used by EdgeStore.

    ``scroll`` sleeps briefly to widen the read-modify-write window, so the
    H1 two-thread test is deterministic rather than probabilistic.
    """

    def __init__(self, read_delay: float = 0.03):
        self._points: dict = {}
        self._lock = threading.Lock()
        self._read_delay = read_delay

    def add_point(self, pid: str) -> None:
        self._points[pid] = _FakePoint(pid)

    def scroll(self, collection_name=None, limit=1000, offset=None,
               with_payload=True, with_vectors=False, scroll_filter=None):
        if self._read_delay:
            time.sleep(self._read_delay)
        with self._lock:
            points = list(self._points.values())
        must = getattr(scroll_filter, "must", None) if scroll_filter else None
        if must:
            for cond in must:
                has_id = getattr(cond, "has_id", None)
                if has_id:
                    points = [p for p in points if p.id in has_id]
        return points[:limit], None

    def set_payload(self, collection_name=None, payload=None, points=None):
        with self._lock:
            for pid in points:
                self._points[pid].payload.update(payload)


def _make_store(client):
    from nexus.graph.store import EdgeStore
    return EdgeStore(client=client, collection="test-collection")


# ── H1: read-modify-write race on the edges array ─────────────────────────────


class TestH1EdgeStoreThreadRace:
    def test_concurrent_add_edge_keeps_both_edges(self):
        client = _FakeClient()
        client.add_point(F_A)
        store = _make_store(client)

        barrier = threading.Barrier(2)
        errors: list = []

        def worker(target):
            try:
                barrier.wait()  # both threads enter add_edge together
                store.add_edge(F_A, target, "supports")
            except Exception as exc:  # pragma: no cover - failure detail
                errors.append(exc)

        threads = [
            threading.Thread(target=worker, args=(t,)) for t in (F_B, F_C)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, errors
        edges = client._points[F_A].payload["edges"]
        targets = {e["target_fact_id"] for e in edges}
        assert targets == {F_B, F_C}
        assert len(edges) == 2

    def test_rmw_runs_under_instance_lock(self):
        """set_payload is reached while self._edges_lock is held."""
        client = _FakeClient(read_delay=0.0)
        client.add_point(F_A)
        store = _make_store(client)

        held: list = []
        original = client.set_payload

        def probing_set_payload(**kwargs):
            held.append(store._edges_lock.locked())
            return original(**kwargs)

        client.set_payload = probing_set_payload  # type: ignore[method-assign]
        store.add_edge(F_A, F_B, "supports")
        assert held == [True]


# ── H2: promote/reject/deprecate must not report a foreign status as success ──


class TestH2LifecycleTransitionsReturnNone:
    def test_promote_active_edge_returns_none(self):
        client = _FakeClient(read_delay=0.0)
        client.add_point(F_A)
        store = _make_store(client)
        active = store.add_edge(F_A, F_B, "supports")
        assert store.promote_edge(active.edge_id) is None
        # active edge untouched
        assert store.get_edge(active.edge_id).status == "active"

    def test_reject_already_rejected_returns_none(self):
        client = _FakeClient(read_delay=0.0)
        client.add_point(F_A)
        store = _make_store(client)
        edge = store.add_edge(F_A, F_B, "supports")
        assert store.reject_edge(edge.edge_id) is not None
        assert store.reject_edge(edge.edge_id) is None

    def test_deprecate_proposed_edge_returns_none(self):
        client = _FakeClient(read_delay=0.0)
        client.add_point(F_A)
        store = _make_store(client)
        proposed = store.add_proposed_edge(F_A, F_B, "supports")
        assert store.deprecate_edge(proposed.edge_id) is None
        assert store.get_edge(proposed.edge_id).status == "proposed"

    def test_promote_proposed_edge_still_works(self):
        client = _FakeClient(read_delay=0.0)
        client.add_point(F_A)
        store = _make_store(client)
        proposed = store.add_proposed_edge(F_A, F_B, "supports")
        promoted = store.promote_edge(proposed.edge_id)
        assert promoted is not None
        assert promoted.status == "active"


# ── H3: provenance formatter checks the key scan_provenance sets ─────────────


class TestH3ProvenanceConfidenceLine:
    def test_confidence_line_appears(self):
        from nexus.provenance import format_provenance_report

        report = format_provenance_report({
            "source_stats": {"chat": 3},
            "creator_stats": {"Kiosha": 3},
            "confidence_avg": 0.85,
            "confidence_min": 0.7,
            "confidence_max": 1.0,
            "criticality_count": 0,
            "total_scanned": 3,
            "no_provenance": 0,
            "provenance_rate": 100.0,
        })
        assert "**Confidence:**" in report
        assert "0.85" in report


# ── H4: a PENDING version must not evict the live canonical ──────────────────


class TestH4PendingDoesNotEvictCanonical:
    def test_pending_supersede_keeps_canonical(self):
        from nexus.lifecycle import CanonicalView, FactVersion

        pending = FactVersion.new_pending({"content": "v1"})
        canonical = FactVersion.promote(pending)
        view = CanonicalView()
        view.set(canonical)
        assert view.get(canonical.fact_id).version_id == canonical.version_id

        newer_pending = FactVersion.new_pending(
            {"content": "v2"},
            fact_id=canonical.fact_id,
            supersedes=canonical.version_id,
        )
        view.set(newer_pending)
        still = view.get(canonical.fact_id)
        assert still is not None
        assert still.version_id == canonical.version_id

    def test_history_supersede_still_evicts_canonical(self):
        from nexus.lifecycle import CanonicalView, FactVersion

        pending = FactVersion.new_pending({"content": "v1"})
        canonical = FactVersion.promote(pending)
        view = CanonicalView()
        view.set(canonical)

        deprecated = FactVersion.deprecate(canonical)
        view.set(deprecated)
        assert view.get(canonical.fact_id) is None


# ── H5: integrity checks are real raises, not asserts ────────────────────────


class TestH5IntegrityChecksAreRaises:
    def test_no_bare_asserts_in_lifecycle_source(self):
        import inspect
        import nexus.lifecycle as lifecycle

        src = inspect.getsource(lifecycle)
        assert "assert pending_version" not in src
        assert "assert previous_version" not in src

    def test_promote_non_pending_raises_runtime_error(self):
        from nexus.lifecycle import CanonicalView, FactVersion

        pending = FactVersion.new_pending({"content": "v1"})
        canonical = FactVersion.promote(pending)
        with pytest.raises(RuntimeError):
            FactVersion.promote(canonical)

    def test_promote_content_hash_mismatch_raises(self):
        from nexus.lifecycle import FactVersion

        pending = FactVersion.new_pending({"content": "v1"})
        pending.content = {"content": "tampered"}
        with pytest.raises(RuntimeError):
            FactVersion.promote(pending)

    def test_deprecate_terminal_version_raises(self):
        from nexus.lifecycle import FactVersion

        pending = FactVersion.new_pending({"content": "v1"})
        canonical = FactVersion.promote(pending)
        deprecated = FactVersion.deprecate(canonical)
        with pytest.raises(RuntimeError):
            FactVersion.deprecate(deprecated)


# ── H6: auxiliary indexes rebuilt on update_index / cache load ───────────────


class TestH6AuxIndexRebuild:
    def test_update_index_rebuilds_aux_indexes(self, tmp_path):
        pytest.importorskip("bm25s")
        from nexus.retrieval import HybridRetriever

        r = HybridRetriever(collection_name="test-collection")
        r._index_dir = tmp_path / "bm25"
        r._ids = ["id1"]
        r._texts = ["iphone in berlin"]
        r._bm25 = None
        r._rebuild_aux_indexes()
        assert "id1" in r._chunk_text_lookup

        r.update_index(memories_to_add=[("id2", "python and Docker in London")])
        assert r._chunk_text_lookup["id2"] == "python and docker in london"
        assert any("id2" in ids for ids in r._entity_index.values())

        # Removal keeps the lookups consistent too.
        r.update_index(memories_to_remove=["id2"])
        assert "id2" not in r._chunk_text_lookup

    def test_empty_corpus_invalidates_bm25(self, tmp_path):
        pytest.importorskip("bm25s")
        from nexus.retrieval import HybridRetriever

        r = HybridRetriever(collection_name="test-collection")
        r._index_dir = tmp_path / "bm25"
        r._ids = ["id1"]
        r._texts = ["iphone"]
        r._bm25 = None
        r.update_index(memories_to_remove=["id1"])
        assert r._bm25 is None
        assert r._chunk_text_lookup == {}

    def test_load_bm25_cache_rebuilds_aux_indexes(self, tmp_path, monkeypatch):
        pytest.importorskip("bm25s")
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        from nexus.retrieval import HybridRetriever

        r = HybridRetriever(collection_name="test-collection")
        r.index_from_texts(
            ["iphone in berlin", "python and docker"], ["id1", "id2"]
        )
        assert r._save_bm25_cache() is True

        r2 = HybridRetriever(collection_name="test-collection")
        assert r2._load_bm25_cache() is True
        assert r2._chunk_text_lookup.get("id1") == "iphone in berlin"
        assert any("id2" in ids for ids in r2._entity_index.values())


# ── H7: SQLite connection closed on early return / exception ─────────────────


class _ConnProxy:
    """Wraps a sqlite3 connection to observe close() calls."""

    def __init__(self, conn, closed, cursor_factory=None):
        object.__setattr__(self, "_conn", conn)
        object.__setattr__(self, "_closed", closed)
        object.__setattr__(self, "_cursor_factory", cursor_factory)

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def __setattr__(self, name, value):
        setattr(self._conn, name, value)

    def cursor(self):
        if self._cursor_factory is not None:
            return self._cursor_factory()
        return self._conn.cursor()

    def close(self):
        self._closed.append(True)
        self._conn.close()


class TestH7SqliteConnectionClosed:
    def _patch(self, monkeypatch, closed, cursor_factory=None):
        from nexus.scripts import migrate

        real_connect = sqlite3.connect

        def fake_connect(*args, **kwargs):
            return _ConnProxy(real_connect(*args, **kwargs), closed, cursor_factory)

        monkeypatch.setattr(migrate.sqlite3, "connect", fake_connect)

    def test_closed_on_missing_edges_table(self, tmp_path, monkeypatch):
        from nexus.scripts.migrate import read_edges_from_sqlite

        db = tmp_path / "empty.db"
        sqlite3.connect(str(db)).close()  # create empty db, no 'edges' table
        closed: list = []
        self._patch(monkeypatch, closed)

        assert read_edges_from_sqlite(str(db)) == []
        assert closed == [True]

    def test_closed_on_exception(self, tmp_path, monkeypatch):
        from nexus.scripts.migrate import read_edges_from_sqlite

        closed: list = []

        class _BoomCursor:
            def execute(self, *a, **k):
                raise RuntimeError("boom")

            def fetchone(self):
                return None

            def fetchall(self):
                return []

        self._patch(monkeypatch, closed, cursor_factory=_BoomCursor)
        with pytest.raises(RuntimeError):
            read_edges_from_sqlite(str(tmp_path / "anything.db"))
        assert closed == [True]


# ── H8: unguarded int cast on access_count ───────────────────────────────────


class TestH8AccessCountCast:
    def test_non_numeric_access_count_does_not_crash(self):
        from nexus.sica import _detect_low_confidence

        points = [
            {
                "id": "bad",
                "payload": {
                    "category": "fact",
                    "provenance": {"confidence": 0.1},
                    "access_count": "abc",
                    "created_at": "2000-01-01T00:00:00Z",
                },
            },
            {
                "id": "good",
                "payload": {
                    "category": "fact",
                    "provenance": {"confidence": 0.1},
                    "access_count": 0,
                    "created_at": "2000-01-01T00:00:00Z",
                },
            },
        ]
        issues = _detect_low_confidence(points, low_confidence_threshold=0.5)
        ids = {i["id"] for i in issues}
        assert "good" in ids
        assert "bad" not in ids  # skipped via `continue`, never raised


# ── H9: migration merges into existing edges instead of replacing them ───────


class TestH9MigrateMergesEdges:
    def test_existing_edges_survive_migration(self, monkeypatch):
        from nexus.scripts import migrate

        point = _FakePoint(F_A)
        point.payload = {
            "edges": [
                {
                    "edge_id": "existing-edge",
                    "target_fact_id": F_C,
                    "relation": "supports",
                    "status": "active",
                }
            ]
        }

        writes: list = []

        class FakeMigrationClient:
            def __init__(self, url=None):
                pass

            def get_collections(self):
                return SimpleNamespace(
                    collections=[SimpleNamespace(name="test-collection")]
                )

            def scroll(self, collection_name=None, limit=1, filter=None,
                       with_payload=True):
                return [point], None

            def set_payload(self, collection_name=None, payload=None, points=None):
                writes.append(payload)
                point.payload.update(payload)

        monkeypatch.setattr(
            migrate, "QdrantClient", lambda url=None: FakeMigrationClient()
        )
        monkeypatch.setattr(
            migrate,
            "read_edges_from_sqlite",
            lambda db_path: [
                {
                    "source_fact_id": F_A,
                    "target_fact_id": F_B,
                    "relation": "supports",
                    "status": "active",
                    "reason": "",
                    "created_at": "2025-01-01T00:00:00",
                }
            ],
        )

        result = migrate.migrate(
            db_path="unused.db", collection="test-collection"
        )

        assert writes, "set_payload was never called"
        edge_ids = {e["edge_id"] for e in point.payload["edges"]}
        assert "existing-edge" in edge_ids  # pre-existing edge preserved
        assert len(point.payload["edges"]) == 2  # merged, not replaced
        assert result["edges_kept"] == 1
        assert result["edges_merged"] == 1

    def test_duplicate_edge_id_is_not_appended_twice(self, monkeypatch):
        from nexus.scripts import migrate

        point = _FakePoint(F_A)
        point.payload = {"edges": []}

        class FakeMigrationClient:
            def __init__(self, url=None):
                pass

            def get_collections(self):
                return SimpleNamespace(
                    collections=[SimpleNamespace(name="test-collection")]
                )

            def scroll(self, collection_name=None, limit=1, filter=None,
                       with_payload=True):
                return [point], None

            def set_payload(self, collection_name=None, payload=None, points=None):
                point.payload.update(payload)

        monkeypatch.setattr(
            migrate, "QdrantClient", lambda url=None: FakeMigrationClient()
        )
        edge = {
            "source_fact_id": F_A,
            "target_fact_id": F_B,
            "relation": "supports",
            "status": "active",
            "reason": "",
            "created_at": "2025-01-01T00:00:00",
        }
        monkeypatch.setattr(
            migrate, "read_edges_from_sqlite", lambda db_path: [edge, dict(edge)]
        )

        result = migrate.migrate(
            db_path="unused.db", collection="test-collection"
        )
        # Both sqlite rows map to the same deterministic edge_id → one append.
        assert len(point.payload["edges"]) == 1
        assert result["edges_merged"] == 1


# ── H10: graph_traverse closes the store on exception ────────────────────────


def _load_graph_traverse():
    script = REPO_ROOT / "plugins" / "claude-code" / "scripts" / "graph_traverse.py"
    spec = importlib.util.spec_from_file_location("graph_traverse_wave7", script)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestH10GraphTraverseClosesStore:
    @pytest.mark.parametrize(
        "func,args",
        [
            ("traverse", (F_A,)),
            ("find_entities", ()),
            ("get_subgraph", (F_A,)),
            ("get_related", (F_A,)),
        ],
    )
    def test_store_closed_on_initialize_exception(self, monkeypatch, func, args):
        import nexus.graph.graph as graph_mod

        instances: list = []

        class FakeStore:
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True

        class FakeSkillGraph:
            def __init__(self, *a, **k):
                self.store = FakeStore()
                instances.append(self)

            def initialize(self):
                raise RuntimeError("boom")

        monkeypatch.setattr(graph_mod, "SkillGraph", FakeSkillGraph)
        mod = _load_graph_traverse()

        with pytest.raises(RuntimeError):
            getattr(mod, func)(*args)

        assert len(instances) == 1
        assert instances[0].store.closed is True

    def test_close_happens_in_finally(self):
        src = (
            REPO_ROOT / "plugins" / "claude-code" / "scripts" / "graph_traverse.py"
        ).read_text()
        assert src.count("finally:") >= 4
        assert src.count("sg.store.close()") >= 4
