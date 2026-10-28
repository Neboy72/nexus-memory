"""Tests for the agent + instance stamp on stored memories.

Every memory must record WHICH agent stored it and from WHICH installation,
next to ``source`` (which says where the INFORMATION came from: mail, web,
consolidation). ``agent`` comes from ``agent_detect.resolve_agent_id()``
(explicit → ``NEXUS_AGENT_ID`` → ``UNIDENTIFIED_AGENT_ID``); ``instance`` from
``resolve_instance_id()`` (``NEXUS_INSTANCE_ID`` → ``instance.json`` in the
Nexus home). Old points simply have no field (None); there is no migration and
no backfill.

No Qdrant server needed: the client is replaced by a small fake, the same
pattern ``tests/test_scope.py`` and ``tests/test_scope_auto.py`` use.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from nexus_memory.agent_detect import (  # noqa: E402
    UNIDENTIFIED_AGENT_ID,
    resolve_instance_id,
)
from nexus_memory.scope_auto import ScopeCentroids  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Never touch the real ~/.nexus-memory and never inherit an id from env."""
    monkeypatch.setenv("NEXUS_HOME", str(tmp_path / "nexus-home"))
    monkeypatch.delenv("NEXUS_INSTANCE_ID", raising=False)


def _store_capturing(monkeypatch, captured: dict):
    """Build a MemoryStore whose upsert payload is captured, no Qdrant."""
    from nexus_memory import mcp_server as mcp

    class FakeClient:
        def upsert(self, collection_name, points):
            captured["payload"] = points[0].payload

        def query_points(self, **kw):  # auto-supersession probe
            class R:
                points = []
            return R()

        def set_payload(self, **_):
            pass

    store = object.__new__(mcp.MemoryStore)
    store.client = FakeClient()  # type: ignore[assignment]
    store._skill_graph = None
    # No scope areas in the system → auto-inference keeps 'default' (and never
    # touches the fake client's missing scroll()).
    store._scope_centroids = ScopeCentroids(None, "nexus")
    store._scope_centroids._cache = {}
    store._scope_centroids._cache_at = float("inf")

    async def fake_embed(self, text, is_query=True):
        return [0.0] * 8

    monkeypatch.setattr(mcp.MemoryStore, "_embed", fake_embed)
    return store


class TestAgentStamp:
    @pytest.mark.asyncio
    async def test_stored_point_carries_agent_and_instance(self, monkeypatch):
        """(a) A stored point has both stamps."""
        monkeypatch.setenv("NEXUS_AGENT_ID", "hermes")
        captured: dict = {}
        store = _store_capturing(monkeypatch, captured)

        await store.remember("A fact worth remembering", "public", "fact")

        assert captured["payload"]["agent"]
        assert captured["payload"]["instance"]

    @pytest.mark.asyncio
    async def test_agent_comes_from_env(self, monkeypatch):
        """(b) NEXUS_AGENT_ID is picked up when set."""
        monkeypatch.setenv("NEXUS_AGENT_ID", "openclaw")
        captured: dict = {}
        store = _store_capturing(monkeypatch, captured)

        await store.remember("A fact worth remembering", "public", "fact")

        assert captured["payload"]["agent"] == "openclaw"

    @pytest.mark.asyncio
    async def test_placeholder_without_env(self, monkeypatch):
        """(b) Without the variable the unidentified placeholder is stored."""
        monkeypatch.delenv("NEXUS_AGENT_ID", raising=False)
        captured: dict = {}
        store = _store_capturing(monkeypatch, captured)

        await store.remember("A fact worth remembering", "public", "fact")

        assert captured["payload"]["agent"] == UNIDENTIFIED_AGENT_ID

    @pytest.mark.asyncio
    async def test_source_stays_untouched(self, monkeypatch):
        """(f) ``source`` is not overwritten by the agent stamp."""
        monkeypatch.setenv("NEXUS_AGENT_ID", "hermes")
        captured: dict = {}
        store = _store_capturing(monkeypatch, captured)

        await store.remember(
            "A fact worth remembering", "public", "fact", source="mail"
        )

        assert captured["payload"]["source"] == "mail"
        assert captured["payload"]["agent"] == "hermes"
        assert captured["payload"]["source"] != captured["payload"]["agent"]


class TestInstanceStamp:
    @pytest.mark.asyncio
    async def test_instance_comes_from_env(self, monkeypatch):
        """(c) NEXUS_INSTANCE_ID wins when set."""
        monkeypatch.setenv("NEXUS_INSTANCE_ID", "  abc123def4567890  ")
        captured: dict = {}
        store = _store_capturing(monkeypatch, captured)

        await store.remember("A fact worth remembering", "public", "fact")

        assert captured["payload"]["instance"] == "abc123def4567890"

    def test_instance_is_generated_and_persisted(self, monkeypatch, tmp_path):
        """(d) Without the variable an id is created, written, and reused."""
        home = tmp_path / "nexus-home"
        monkeypatch.setenv("NEXUS_HOME", str(home))

        first = resolve_instance_id()
        second = resolve_instance_id()

        assert first == second
        assert len(first) == 16
        assert all(c in "0123456789abcdef" for c in first)
        data = json.loads((home / "instance.json").read_text())
        assert data["instance_id"] == first
        assert data["created_at"]

    @pytest.mark.asyncio
    async def test_remember_reuses_the_persisted_instance(self, monkeypatch, tmp_path):
        """(d) Two stores in one installation stamp the SAME instance id."""
        home = tmp_path / "nexus-home"
        monkeypatch.setenv("NEXUS_HOME", str(home))
        first: dict = {}
        second: dict = {}

        await _store_capturing(monkeypatch, first).remember(
            "A fact worth remembering", "public", "fact"
        )
        await _store_capturing(monkeypatch, second).remember(
            "Another fact worth remembering", "public", "fact"
        )

        assert first["payload"]["instance"] == second["payload"]["instance"]
        assert (home / "instance.json").exists()

    def test_broken_instance_file_is_replaced(self, monkeypatch, tmp_path):
        """(e) A corrupt instance.json yields a new valid id, never raises."""
        home = tmp_path / "nexus-home"
        home.mkdir(parents=True, exist_ok=True)
        (home / "instance.json").write_text("{not json at all")
        monkeypatch.setenv("NEXUS_HOME", str(home))

        instance_id = resolve_instance_id()

        assert len(instance_id) == 16
        assert all(c in "0123456789abcdef" for c in instance_id)
        data = json.loads((home / "instance.json").read_text())
        assert data["instance_id"] == instance_id

    def test_instance_file_without_id_is_replaced(self, monkeypatch, tmp_path):
        """(e) Valid JSON but no instance_id counts as unusable too."""
        home = tmp_path / "nexus-home"
        home.mkdir(parents=True, exist_ok=True)
        (home / "instance.json").write_text(json.dumps({"created_at": "x"}))
        monkeypatch.setenv("NEXUS_HOME", str(home))

        instance_id = resolve_instance_id()

        assert len(instance_id) == 16
        data = json.loads((home / "instance.json").read_text())
        assert data["instance_id"] == instance_id
