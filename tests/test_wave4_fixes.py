"""Wave 4 (Päckchen 4 / group E) fixes — regression tests.

Covers the K/H findings from the wave-4 review:

- K2  ``nexus_remember`` / ``nexus_query_valid`` / ``nexus_consolidate`` /
      ``nexus_update`` resolve ``collection_name=None`` via
      ``nexus.config.get_collection`` instead of putting the literal
      ``None`` into the request URL (→ 404).
- K3  ``nexus.scripts.migrate.group_edges_by_source`` writes the payload
      keys that ``nexus.graph.store`` actually reads back
      (``edge_id`` / ``target_fact_id`` / ``relation`` / ``status``).
- K4  ``scripts/session_dump.py`` binds the role filter as a SQL
      parameter instead of interpolating argv into the query.

Everything is mocked — no network, no real Qdrant.
"""

from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import nexus  # noqa: E402
from nexus.graph.schema import Edge, EdgeStatus  # noqa: E402
from nexus.scripts.migrate import group_edges_by_source  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _resp(status_code, payload=None, text=""):
    r = MagicMock()
    r.status_code = status_code
    r.text = text
    r.json.return_value = payload if payload is not None else {}
    return r


def _load_session_dump():
    """Load scripts/session_dump.py as a module (no import side effects)."""
    path = _REPO_ROOT / "scripts" / "session_dump.py"
    spec = importlib.util.spec_from_file_location("nexus_session_dump_wave4", str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ===========================================================================
# K2 — collection default must be resolved, never rendered as "None"
# ===========================================================================


class _CollectionRecorder:
    """Stand-in for ``nexus.config.get_collection``."""

    def __init__(self, resolved: str = "resolved-collection"):
        self.resolved = resolved
        self.calls: list[object] = []

    def __call__(self, override=None):
        self.calls.append(override)
        return override or self.resolved


class TestK2CollectionDefault:
    def test_remember_resolves_none_default(self, monkeypatch):
        rec = _CollectionRecorder()
        monkeypatch.setattr(nexus, "get_collection", rec)
        captured: dict = {}
        monkeypatch.setattr(
            "requests.put",
            lambda url, **kw: captured.update(url=url) or _resp(200, {"status": "ok"}),
        )

        nexus.nexus_remember("hello world")

        assert rec.calls == [None]
        assert "/collections/resolved-collection/points" in captured["url"]
        assert "/None/" not in captured["url"]

    def test_remember_honours_explicit_collection(self, monkeypatch):
        rec = _CollectionRecorder()
        monkeypatch.setattr(nexus, "get_collection", rec)
        captured: dict = {}
        monkeypatch.setattr(
            "requests.put",
            lambda url, **kw: captured.update(url=url) or _resp(200, {"status": "ok"}),
        )

        nexus.nexus_remember("hello", collection_name="explicit")

        assert rec.calls == ["explicit"]
        assert "/collections/explicit/points" in captured["url"]

    def test_query_valid_resolves_none_default(self, monkeypatch):
        rec = _CollectionRecorder()
        monkeypatch.setattr(nexus, "get_collection", rec)
        captured: dict = {}
        monkeypatch.setattr(
            "requests.post",
            lambda url, **kw: captured.update(url=url)
            or _resp(200, {"result": {"points": []}}),
        )

        assert nexus.nexus_query_valid("q") == []

        assert rec.calls == [None]
        assert "/collections/resolved-collection/points/scroll" in captured["url"]

    def test_consolidate_resolves_none_default(self, monkeypatch):
        rec = _CollectionRecorder()
        monkeypatch.setattr(nexus, "get_collection", rec)

        assert nexus.nexus_consolidate([]) == []

        assert rec.calls == [None]

    def test_update_resolves_none_default(self, monkeypatch):
        rec = _CollectionRecorder()
        monkeypatch.setattr(nexus, "get_collection", rec)
        captured: dict = {}
        monkeypatch.setattr(
            "requests.post",
            lambda url, **kw: captured.update(url=url) or _resp(500, {}, "boom"),
        )

        with pytest.raises(RuntimeError):
            nexus.nexus_update("abcdefgh", new_content="x")

        assert rec.calls == [None]
        assert "/collections/resolved-collection/points/scroll" in captured["url"]


# ===========================================================================
# K3 — migrated edge payload keys match the consumer schema
# ===========================================================================


def _sqlite_edges() -> list[dict]:
    return [
        {
            "source_fact_id": "src-1",
            "target_fact_id": "tgt-1",
            "relation": "manages",
            "status": "active",
            "reason": "because",
            "created_at": "2026-01-01T00:00:00",
        },
        {
            "source_fact_id": "src-1",
            "target_fact_id": "tgt-2",
            "relation": "runs_on",
            "status": "active",
            "reason": "",
            "created_at": "2026-01-02T00:00:00",
        },
    ]


class TestK3MigratePayloadSchema:
    def test_consumer_keys_present(self):
        grouped = group_edges_by_source(_sqlite_edges())
        assert set(grouped) == {"src-1"}
        entry = grouped["src-1"][0]
        for key in ("edge_id", "target_fact_id", "relation", "status"):
            assert key in entry, f"missing consumer key: {key}"
        # legacy extras must survive
        for key in ("target_name", "confidence", "context", "source_doc_id",
                    "created_at"):
            assert key in entry
        assert entry["target_fact_id"] == "tgt-1"
        assert entry["relation"] == "manages"
        assert entry["status"] == "active"
        assert entry["source_doc_id"] == "src-1"
        assert entry["context"] == "because"

    def test_edge_id_is_deterministic_12_hex(self):
        a = group_edges_by_source(_sqlite_edges())["src-1"][0]["edge_id"]
        b = group_edges_by_source(_sqlite_edges())["src-1"][0]["edge_id"]
        assert a == b
        assert len(a) == 12
        int(a, 16)  # hex-decodable
        # different relation → different id
        assert a != group_edges_by_source(_sqlite_edges())["src-1"][1]["edge_id"]

    def test_entries_round_trip_through_edge_store_schema(self):
        """``Edge.from_payload_entry`` is what store.py uses to read back."""
        for entry in group_edges_by_source(_sqlite_edges())["src-1"]:
            edge = Edge.from_payload_entry(entry, source_fact_id="src-1")
            assert edge.edge_id == entry["edge_id"]
            assert edge.target_fact_id == entry["target_fact_id"]
            assert edge.relation == entry["relation"]
            assert edge.status == EdgeStatus.ACTIVE.value

    def test_legacy_keys_are_gone(self):
        entry = group_edges_by_source(_sqlite_edges())["src-1"][0]
        assert "target_id" not in entry
        assert "relation_type" not in entry


# ===========================================================================
# K4 — session_dump role filter is a bound parameter
# ===========================================================================


class TestK4SessionDumpSqlInjection:
    def test_role_filter_is_bound(self):
        mod = _load_session_dump()
        sql, params = mod.build_query("s1", "user")
        assert "role=?" in sql
        assert params == ("s1", "user")
        assert "user" not in sql

    def test_no_filter_keeps_single_param(self):
        mod = _load_session_dump()
        sql, params = mod.build_query("s1", None)
        assert "role=?" not in sql
        assert params == ("s1",)

    def test_injection_string_never_reaches_sql(self):
        mod = _load_session_dump()
        payload = "' OR 1=1 --"
        sql, params = mod.build_query("s1", payload)
        assert payload not in sql
        assert params == ("s1", payload)

    def test_injection_does_not_bypass_filter(self):
        """End-to-end against an in-memory DB: the injected OR is inert."""
        mod = _load_session_dump()
        con = sqlite3.connect(":memory:")
        con.execute(
            "CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, "
            "role TEXT, content TEXT, tool_name TEXT, timestamp REAL, active INTEGER)"
        )
        con.execute(
            "INSERT INTO messages (session_id, role, content, active) "
            "VALUES ('s1', 'user', 'u', 1)"
        )
        con.execute(
            "INSERT INTO messages (session_id, role, content, active) "
            "VALUES ('s1', 'assistant', 'a', 1)"
        )
        sql, params = mod.build_query("s1", "' OR 1=1 --")
        rows = con.execute(sql, params).fetchall()
        con.close()
        assert rows == []

        con = sqlite3.connect(":memory:")
        con.execute(
            "CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, "
            "role TEXT, content TEXT, tool_name TEXT, timestamp REAL, active INTEGER)"
        )
        con.execute(
            "INSERT INTO messages (session_id, role, content, active) "
            "VALUES ('s1', 'user', 'u', 1)"
        )
        con.execute(
            "INSERT INTO messages (session_id, role, content, active) "
            "VALUES ('s1', 'assistant', 'a', 1)"
        )
        sql, params = mod.build_query("s1", "user")
        roles = [r[1] for r in con.execute(sql, params).fetchall()]
        con.close()
        assert roles == ["user"]
