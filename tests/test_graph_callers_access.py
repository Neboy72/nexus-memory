"""The graph's callers hand their own visibility level to the core.

``GraphTraversal`` defaults to ``"public"`` on purpose (fail-closed). A caller
that forgets to pass its level therefore hides nearly every legacy point —
payloads without ``access_level`` count as ``private``. The rule itself is
pinned in ``test_graph_access_level.py``; this file pins the WIRING of the
shipped callers: the MCP server's graph tools and the Hermes plugin.

The MCP tests run against a real embedded Qdrant (same fixture as
``test_graph_access_level.py``) and patch only ``SkillGraph``, so the handler
code path — validation, level pass-through, traversal — is exercised end to end.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

import nexus.graph.graph as graph_mod
import nexus.graph.traversal as traversal_mod
import nexus_memory.mcp_server as mcp
from nexus.graph.traversal import visible_access_levels

# Reuse the real-Qdrant fixture instead of duplicating it: both files probe the
# same wiring at different layers.
from test_graph_access_level import GraphFixture, fid  # noqa: F401

REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_PATH = REPO_ROOT / "plugins" / "memory" / "nexus" / "__init__.py"


@pytest.fixture
def graph(tmp_path):
    fixture = GraphFixture(tmp_path)
    try:
        yield fixture
    finally:
        fixture.close()


@pytest.fixture
def patched_graph(graph, monkeypatch):
    """Route the MCP handler's ``SkillGraph`` to the fixture's real graph.

    ``store.close()`` becomes a no-op: the fixture still needs the client after
    the handler returns, and the handler closes the store on every path.
    """

    class _Store:
        def __init__(self, real):
            self._real = real

        def close(self) -> None:
            pass

        @property
        def client(self):
            return self._real.client

        @property
        def _collection(self):
            return self._real._collection

    class _Graph:
        def __init__(self, *args, **kwargs):
            self.store = _Store(graph.store)

        def initialize(self) -> None:
            pass

        def has_node(self, fact_id: str) -> bool:
            return graph.sg.has_node(fact_id)

        def neighbors(self, *args, **kwargs):
            return graph.sg.neighbors(*args, **kwargs)

    monkeypatch.setattr(graph_mod, "SkillGraph", _Graph)
    return graph


@pytest.fixture
def mcp_server():
    """Minimal stand-in for the dispatcher's singleton store.

    The graph tools only read ``store._binding`` (absent → ``None``).
    """
    class _Server:
        pass

    previous = mcp._store
    mcp._store = _Server()
    try:
        yield mcp
    finally:
        mcp._store = previous


def _body(out) -> dict:
    return json.loads(out[0].text)


@pytest.fixture
def one_hop(patched_graph):
    """start --(connected_to)--> pub-n ; start --> priv-n."""
    patched_graph.add_fact("start", {"access_level": "public"})
    patched_graph.add_fact("pub-n", {"access_level": "public"})
    patched_graph.add_fact("priv-n", {"access_level": "private"})
    patched_graph.edge("start", "pub-n")
    patched_graph.edge("start", "priv-n")
    return patched_graph.initialize()


# ── MCP server: graph_traverse ──────────────────────────────────────────────


class TestMcpGraphTraverseAccessLevel:
    async def test_public_caller_does_not_get_a_private_target(self, mcp_server, one_hop):
        out = await mcp_server.handle_call_tool(
            "graph_traverse",
            {"fact_id": fid("start"), "max_depth": 1, "access_level": "public"},
        )
        found = [r["fact_id"] for r in _body(out)["results"]]
        assert found == [fid("pub-n")]
        assert fid("priv-n") not in found

    async def test_private_caller_gets_the_private_target(self, mcp_server, one_hop):
        out = await mcp_server.handle_call_tool(
            "graph_traverse",
            {"fact_id": fid("start"), "max_depth": 1, "access_level": "private"},
        )
        found = sorted(r["fact_id"] for r in _body(out)["results"])
        assert found == sorted([fid("pub-n"), fid("priv-n")])

    async def test_default_level_is_private(self, mcp_server, one_hop):
        """The server's convention: an omitted level is the owner's own."""
        out = await mcp_server.handle_call_tool(
            "graph_traverse", {"fact_id": fid("start"), "max_depth": 1},
        )
        found = sorted(r["fact_id"] for r in _body(out)["results"])
        assert found == sorted([fid("pub-n"), fid("priv-n")])

    async def test_invalid_level_is_an_error_not_an_empty_result(self, mcp_server, one_hop):
        out = await mcp_server.handle_call_tool(
            "graph_traverse",
            {"fact_id": fid("start"), "max_depth": 1, "access_level": "god-mode"},
        )
        body = _body(out)
        assert body["status"] == "error"
        assert "access_level" in body["error"]
        assert "results" not in body


class TestMcpOtherGraphToolsPassTheLevel:
    async def test_find_entities(self, mcp_server, patched_graph):
        patched_graph.add_entity("pub", "device", {"access_level": "public"})
        patched_graph.add_entity("priv", "device", {"access_level": "private"})
        patched_graph.initialize()

        public = await mcp_server.handle_call_tool(
            "find_entities", {"access_level": "public"},
        )
        assert [e["name"] for e in _body(public)["entities"]] == ["pub"]

        owner = await mcp_server.handle_call_tool(
            "find_entities", {"access_level": "private"},
        )
        assert sorted(e["name"] for e in _body(owner)["entities"]) == ["priv", "pub"]

    async def test_get_related(self, mcp_server, one_hop):
        out = await mcp_server.handle_call_tool(
            "get_related", {"fact_id": fid("start"), "access_level": "public"},
        )
        assert [r["fact_id"] for r in _body(out)["results"]] == [fid("pub-n")]

    async def test_get_subgraph(self, mcp_server, one_hop):
        out = await mcp_server.handle_call_tool(
            "get_subgraph",
            {"fact_id": fid("start"), "max_depth": 1, "access_level": "public"},
        )
        node_ids = {n["id"] for n in _body(out)["nodes"]}
        assert node_ids == {fid("start"), fid("pub-n")}

    @pytest.mark.parametrize(
        "tool, arguments",
        [
            ("find_entities", {}),
            ("get_subgraph", {"fact_id": fid("start")}),
            ("get_related", {"fact_id": fid("start")}),
        ],
    )
    async def test_invalid_level_is_rejected(self, mcp_server, one_hop, tool, arguments):
        out = await mcp_server.handle_call_tool(
            tool, {**arguments, "access_level": "god-mode"},
        )
        assert _body(out)["status"] == "error"


# ── Hermes plugin: _agent_level + the graph tools ───────────────────────────


@pytest.fixture(scope="module")
def plugin():
    spec = importlib.util.spec_from_file_location(
        "nexus_hermes_plugin_callers", str(PLUGIN_PATH)
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestHermesAgentLevel:
    def test_primary_context_sees_private(self, plugin):
        provider = plugin.NexusMemoryProvider()
        provider._agent_context = "primary"
        assert provider._visible_levels() == {"public", "trusted", "private"}
        assert provider._agent_level() == "private"

    def test_foreign_context_sees_public_only(self, plugin):
        provider = plugin.NexusMemoryProvider()
        provider._agent_context = "shared"
        assert provider._visible_levels() == {"public"}
        assert provider._agent_level() == "public"

    def test_empty_visible_set_is_unknown_and_sees_nothing(self, plugin, monkeypatch):
        """No guessing: an empty set becomes a level the core treats as unknown."""
        provider = plugin.NexusMemoryProvider()
        monkeypatch.setattr(provider, "_visible_levels", lambda: set())
        level = provider._agent_level()
        assert level not in ("public", "trusted", "private")
        assert visible_access_levels(level) == ()

    def test_graph_traverse_tool_passes_the_level_to_the_core(self, plugin, monkeypatch):
        provider = plugin.NexusMemoryProvider()
        provider._qdrant = object()
        provider._collection = "test"
        provider._agent_context = "primary"

        seen = {}

        class _RecordingTraversal:
            def __init__(self, sg, access_level="public"):
                seen["access_level"] = access_level

            def traverse(self, *args, **kwargs):
                return []

        class _FakeStore:
            def close(self) -> None:
                pass

        class _FakeGraph:
            def __init__(self, *args, **kwargs):
                self.store = _FakeStore()

            def initialize(self) -> None:
                pass

        monkeypatch.setattr(graph_mod, "SkillGraph", _FakeGraph)
        monkeypatch.setattr(traversal_mod, "GraphTraversal", _RecordingTraversal)

        assert provider._graph_traverse(fid("a")) == {"results": []}
        assert seen["access_level"] == "private"
