"""Universal access counting for the agents registry.

The registry counters used to be fed only at two protocol edges (the MCP
handler and the Hermes plugin) and only when ``NEXUS_AGENT_ID`` was set; an
unknown id was silently dropped. These tests pin the replacement contract:

1. an unknown id is REGISTERED on first contact and counted,
2. the core store counts every read/write itself (exactly once — the MCP
   handler no longer counts remember/recall on top),
3. a missing id counts under the fixed label ``unidentified``,
4. a broken registry never breaks a memory operation.

The ``isolated_agents_registry`` fixture (conftest) redirects agents.json to a
temp file for every test, so no real registry is touched.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock

import pytest

import nexus_memory.mcp_server as mcp
from nexus_memory import agent_detect as ad


# ---------------------------------------------------------------------------
# Helpers — a MemoryStore with mocked Qdrant/embedder (no network)
# ---------------------------------------------------------------------------

class _FakeEmbedder:
    """Deterministic stand-in; no provider, no model download."""

    dim = 8
    vector_name = None

    async def embed(self, text: str, is_query: bool = True) -> list[float]:
        return [0.0] * 8


def _make_store() -> "mcp.MemoryStore":
    store = mcp.MemoryStore.__new__(mcp.MemoryStore)
    store.client = MagicMock()
    store._embedder = _FakeEmbedder()
    store._hybrid_retriever = None     # force the vector-fallback path
    store._skill_graph = None
    store._scope_centroids = None
    store.vector_name = None
    store._update_check_result = None
    store._update_nudged_at = 0
    empty = MagicMock()
    empty.points = []
    store.client.query_points.return_value = empty
    store.client.retrieve.return_value = []
    store.client.scroll.return_value = ([], None)
    return store


def _agents(reg_path) -> list[dict]:
    if not reg_path.exists():
        return []
    return json.loads(reg_path.read_text()).get("agents", [])


def _counts(reg_path, agent_id: str) -> tuple[int, int]:
    entry = next(a for a in _agents(reg_path) if a["id"] == agent_id)
    return entry["reads"], entry["writes"]


# ---------------------------------------------------------------------------
# 1. Unknown agent ids are registered, not discarded
# ---------------------------------------------------------------------------

def test_unknown_agent_id_is_registered_on_first_write(isolated_agents_registry):
    reg = isolated_agents_registry

    ad.update_agent_stats("brandneuer-agent", write=True)

    agents = _agents(reg)
    assert len(agents) == 1, "an unknown id must be registered, not dropped"
    entry = agents[0]
    assert entry["id"] == "brandneuer-agent"
    assert entry["name"] == "brandneuer-agent", "name defaults to the id"
    assert entry["trust_level"] == "public", "safe default: no visibility gain"
    assert entry["writes"] == 1
    assert entry["reads"] == 0
    assert entry["connected_at"], "connected_at must be stamped"
    assert entry["last_seen"], "last_seen must be stamped"
    assert entry["install_type"] and entry["host_label"]


def test_second_call_counts_again_without_a_second_entry(
        isolated_agents_registry):
    reg = isolated_agents_registry

    ad.update_agent_stats("brandneuer-agent", write=True)
    ad.update_agent_stats("brandneuer-agent", write=True)

    agents = _agents(reg)
    assert len(agents) == 1, "the entry is reused, not duplicated"
    assert agents[0]["writes"] == 2


def test_known_agent_keeps_its_registered_metadata(isolated_agents_registry):
    """Auto-registration must never overwrite a wizard-registered entry."""
    reg = isolated_agents_registry
    ad.register_agent("known-agent", "Known Agent", "K", "trusted", "plugin+mcp")

    ad.update_agent_stats("known-agent", read=True)

    entry = _agents(reg)[0]
    assert entry["trust_level"] == "trusted"
    assert entry["name"] == "Known Agent"
    assert entry["reads"] == 1


# ---------------------------------------------------------------------------
# 2. The core store counts — exactly once
# ---------------------------------------------------------------------------

def test_core_recall_counts_exactly_one_read(isolated_agents_registry):
    reg = isolated_agents_registry
    store = _make_store()

    asyncio.run(store.recall("irgendwas"))

    entry = _agents(reg)[0]
    assert entry["reads"] == 1, "one recall() == exactly one read"
    assert entry["writes"] == 0


def test_core_remember_counts_exactly_one_write(isolated_agents_registry):
    reg = isolated_agents_registry
    store = _make_store()

    asyncio.run(store.remember(
        "Ein neuer Fakt", category="session", scope="test-scope",
    ))

    entry = _agents(reg)[0]
    assert entry["writes"] == 1, "one remember() == exactly one write"
    assert entry["reads"] == 0


def test_mcp_recall_tool_does_not_double_count(isolated_agents_registry,
                                               monkeypatch):
    """The handler must not count recall on top of the store (that was the
    historical double-count risk once the core counts)."""
    reg = isolated_agents_registry
    store = _make_store()
    monkeypatch.setattr(mcp, "get_store", lambda: store)
    monkeypatch.setattr(mcp.handle_call_tool, "_skip_stats", False,
                        raising=False)

    asyncio.run(mcp.handle_call_tool("recall", {"query": "irgendwas"}))

    assert _counts(reg, ad.UNIDENTIFIED_AGENT_ID) == (1, 0)


def test_mcp_remember_tool_does_not_double_count(isolated_agents_registry,
                                                 monkeypatch):
    reg = isolated_agents_registry
    store = _make_store()
    monkeypatch.setattr(mcp, "get_store", lambda: store)
    monkeypatch.setattr(mcp.handle_call_tool, "_skip_stats", False,
                        raising=False)

    asyncio.run(mcp.handle_call_tool(
        "remember",
        {"text": "Ein neuer Fakt", "category": "session",
         "scope": "test-scope"},
    ))

    assert _counts(reg, ad.UNIDENTIFIED_AGENT_ID) == (0, 1)


# ---------------------------------------------------------------------------
# 3. No id anywhere -> counted under "unidentified"
# ---------------------------------------------------------------------------

def test_missing_id_counts_under_unidentified(isolated_agents_registry,
                                              monkeypatch):
    reg = isolated_agents_registry
    monkeypatch.delenv("NEXUS_AGENT_ID", raising=False)

    ad.record_agent_access(read=True)

    ids = [a["id"] for a in _agents(reg)]
    assert ids == [ad.UNIDENTIFIED_AGENT_ID]
    assert _counts(reg, ad.UNIDENTIFIED_AGENT_ID) == (1, 0)


def test_env_id_is_used_when_no_explicit_id(isolated_agents_registry,
                                            monkeypatch):
    reg = isolated_agents_registry
    monkeypatch.setenv("NEXUS_AGENT_ID", "env-caller")

    ad.record_agent_access(write=True)

    assert _counts(reg, "env-caller") == (0, 1)


def test_explicit_id_wins_over_env(isolated_agents_registry, monkeypatch):
    reg = isolated_agents_registry
    monkeypatch.setenv("NEXUS_AGENT_ID", "env-caller")

    ad.record_agent_access("explicit-caller", read=True)

    ids = [a["id"] for a in _agents(reg)]
    assert ids == ["explicit-caller"]


# ---------------------------------------------------------------------------
# 3b. Self-tests are not agents
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("flag", ["1", "true", "TRUE"])
def test_selftest_registers_nothing(isolated_agents_registry, monkeypatch, flag):
    """A watchdog self-test must not become a dashboard card."""
    reg = isolated_agents_registry
    monkeypatch.setenv("NEXUS_AGENT_ID", "wache-selftest")
    monkeypatch.setenv("NEXUS_AGENT_SELFTEST", flag)

    ad.record_agent_access(read=True)
    # The Hermes plugin path calls update_agent_stats directly.
    ad.update_agent_stats("wache-selftest", write=True)

    assert _agents(reg) == [], "a self-test must not register an agent"


def test_selftest_does_not_increment_an_existing_entry(
        isolated_agents_registry, monkeypatch):
    reg = isolated_agents_registry
    ad.register_agent("known-agent", "Known Agent", "K", "trusted", "plugin+mcp")
    before = _agents(reg)[0]

    monkeypatch.setenv("NEXUS_AGENT_ID", "known-agent")
    monkeypatch.setenv("NEXUS_AGENT_SELFTEST", "1")
    ad.record_agent_access(read=True, write=True)

    after = _agents(reg)[0]
    assert after["reads"] == before["reads"]
    assert after["writes"] == before["writes"]
    assert after["last_seen"] == before["last_seen"]


# ---------------------------------------------------------------------------
# 4. A broken registry never breaks a memory operation
# ---------------------------------------------------------------------------

def test_corrupt_registry_never_breaks_the_counting_call(
        isolated_agents_registry):
    isolated_agents_registry.write_text("{kaputtes json")

    # Must not raise: counting is telemetry.
    ad.record_agent_access("egal", read=True, write=True)


def test_corrupt_registry_never_breaks_recall(isolated_agents_registry):
    isolated_agents_registry.write_text("{kaputtes json")
    store = _make_store()

    results = asyncio.run(store.recall("irgendwas"))

    assert results == [], "the memory operation must run to completion"
    assert isolated_agents_registry.read_text() == "{kaputtes json", (
        "a broken registry must never be overwritten by the counter"
    )


def test_corrupt_registry_never_breaks_remember(isolated_agents_registry):
    isolated_agents_registry.write_text("{kaputtes json")
    store = _make_store()

    result = asyncio.run(store.remember(
        "Ein neuer Fakt", category="session", scope="test-scope",
    ))

    assert result["status"] == "ok"


def test_unwritable_registry_path_never_breaks_recall(tmp_path, monkeypatch):
    """A path that cannot be a file (e.g. a directory) must stay silent too."""
    broken = tmp_path / "agents.json"
    broken.mkdir()
    monkeypatch.setattr(ad, "_get_agents_registry_path", lambda: broken)

    results = asyncio.run(_make_store().recall("irgendwas"))

    assert results == []
