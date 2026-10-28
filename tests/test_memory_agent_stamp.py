"""Tests for the agent + instance stamp on stored memories.

Every memory must record WHICH agent stored it and from WHICH installation,
next to ``source`` (which says where the INFORMATION came from: mail, web,
consolidation). ``agent`` comes from ``agent_detect.resolve_agent_id()``
(explicit → ``NEXUS_AGENT_ID`` → ``UNIDENTIFIED_AGENT_ID``); ``instance`` from
``resolve_instance_id()`` (``NEXUS_INSTANCE_ID`` → this home's own
``instance-<seed hash>.json`` in the Nexus home, seeded on first use by
``HERMES_HOME``/``NEXUS_HOME``). Old points simply have no field (None); there
is no migration and no backfill.

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
    monkeypatch.delenv("HERMES_HOME", raising=False)


def _id_files(home: Path) -> list:
    """The per-home id files in ``home`` (the legacy ``instance.json`` is not one)."""
    return sorted(home.glob("instance-*.json")) if home.exists() else []


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
        files = _id_files(home)
        assert len(files) == 1
        assert files[0] != home / "instance.json"
        data = json.loads(files[0].read_text())
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
        assert len(_id_files(home)) == 1

    def test_broken_instance_file_is_replaced(self, monkeypatch, tmp_path):
        """(e) A corrupt id file yields a new valid id, never raises."""
        home = tmp_path / "nexus-home"
        monkeypatch.setenv("NEXUS_HOME", str(home))
        resolve_instance_id()
        id_file = _id_files(home)[0]
        id_file.write_text("{not json at all")

        instance_id = resolve_instance_id()

        assert len(instance_id) == 16
        assert all(c in "0123456789abcdef" for c in instance_id)
        data = json.loads(id_file.read_text())
        assert data["instance_id"] == instance_id

    def test_instance_file_without_id_is_replaced(self, monkeypatch, tmp_path):
        """(e) Valid JSON but no instance_id counts as unusable too."""
        home = tmp_path / "nexus-home"
        monkeypatch.setenv("NEXUS_HOME", str(home))
        resolve_instance_id()
        id_file = _id_files(home)[0]
        id_file.write_text(json.dumps({"created_at": "x"}))

        instance_id = resolve_instance_id()

        assert len(instance_id) == 16
        data = json.loads(id_file.read_text())
        assert data["instance_id"] == instance_id

    def test_shared_nexus_home_still_gives_one_id_per_profile(self, monkeypatch, tmp_path):
        """(g) Two HERMES_HOME values sharing ONE NEXUS_HOME get TWO ids.

        This is the case the derivation exists for: the Nexus home is shared,
        so the id files must be per profile, not one file for all of them.
        """
        home = tmp_path / "shared-nexus-home"
        monkeypatch.setenv("NEXUS_HOME", str(home))

        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-profile-a"))
        first = resolve_instance_id()

        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-profile-b"))
        second = resolve_instance_id()

        assert first != second
        assert len(first) == 16
        assert len(second) == 16
        files = _id_files(home)
        assert len(files) == 2
        stored = {json.loads(f.read_text())["instance_id"] for f in files}
        assert stored == {first, second}

    def test_profiles_in_the_default_home_get_two_ids(self, monkeypatch, tmp_path):
        """(g) The real layout: profiles share ~/.nexus-memory, ids still split."""
        monkeypatch.delenv("NEXUS_HOME", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        home = tmp_path / ".nexus-memory"

        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-profile-a"))
        first = resolve_instance_id()
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-profile-b"))
        second = resolve_instance_id()

        assert first != second
        assert len(_id_files(home)) == 2

    def test_a_corrupt_file_of_one_profile_leaves_the_other_alone(self, monkeypatch, tmp_path):
        """(j) Per-profile files are isolated: one broken file repairs only itself."""
        home = tmp_path / "shared-nexus-home"
        monkeypatch.setenv("NEXUS_HOME", str(home))

        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-profile-a"))
        id_a = resolve_instance_id()
        file_a = _id_files(home)[0]
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-profile-b"))
        id_b = resolve_instance_id()
        file_b = next(f for f in _id_files(home) if f != file_a)

        file_a.write_text("{not json at all")
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-profile-a"))
        repaired = resolve_instance_id()
        assert len(repaired) == 16
        assert json.loads(file_a.read_text())["instance_id"] == repaired

        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-profile-b"))
        assert resolve_instance_id() == id_b
        assert json.loads(file_b.read_text())["instance_id"] == id_b

    def test_stored_id_wins_over_a_changed_hermes_home(self, monkeypatch, tmp_path):
        """(h) A → B → A: the id stored for A is read back, never recomputed."""
        home = tmp_path / "nexus-home"
        monkeypatch.setenv("NEXUS_HOME", str(home))
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-profile-a"))
        resolve_instance_id()
        id_file_a = _id_files(home)[0]

        # A stored id must beat the derivation, so store one the seed cannot
        # produce: only a read-back (not a recompute) can return it.
        stored = "0123456789abcdef"
        id_file_a.write_text(json.dumps({"instance_id": stored, "created_at": "x"}))

        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-profile-b"))
        other = resolve_instance_id()

        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-profile-a"))
        again = resolve_instance_id()

        assert other != stored
        assert again == stored
        assert json.loads(id_file_a.read_text())["instance_id"] == stored

    def test_works_without_hermes_and_nexus_home(self, monkeypatch, tmp_path):
        """(i) Neither variable set: an id is still created and persisted."""
        monkeypatch.delenv("HERMES_HOME", raising=False)
        monkeypatch.delenv("NEXUS_HOME", raising=False)
        # Redirect the fallback home (~/.nexus-memory) into the test.
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        home = tmp_path / ".nexus-memory"

        first = resolve_instance_id()
        second = resolve_instance_id()

        assert first == second
        assert len(first) == 16
        assert all(c in "0123456789abcdef" for c in first)
        files = _id_files(home)
        assert len(files) == 1
        assert json.loads(files[0].read_text())["instance_id"] == first

    def test_legacy_instance_file_is_ignored(self, monkeypatch, tmp_path):
        """(j) The old shared instance.json is not read any more."""
        home = tmp_path / "nexus-home"
        home.mkdir(parents=True, exist_ok=True)
        legacy = home / "instance.json"
        legacy.write_text(json.dumps({"instance_id": "aaaaaaaaaaaaaaaa",
                                      "created_at": "x"}))
        monkeypatch.setenv("NEXUS_HOME", str(home))
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-profile"))

        instance_id = resolve_instance_id()

        assert instance_id != "aaaaaaaaaaaaaaaa"
        assert len(instance_id) == 16
        files = _id_files(home)
        assert len(files) == 1
        assert json.loads(files[0].read_text())["instance_id"] == instance_id
        # The legacy file is left alone: unread, not migrated, not deleted.
        assert json.loads(legacy.read_text())["instance_id"] == "aaaaaaaaaaaaaaaa"
