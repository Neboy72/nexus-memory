"""Access-level filtering in the Knowledge-Graph layer.

The graph is a second way into the collection: a fact that an agent may not see
must not become reachable through an edge either. These tests pin the rule the
rest of the project already uses (``auto_recall.py``, ``mcp_server.py``, the
OpenClaw TS client):

* an agent at level N sees memories at level 0..N (public < trusted < private),
* a payload WITHOUT ``access_level`` counts as ``private`` (fail-closed),
* a hidden point is neither returned nor traversed through, and
* a hidden start point answers exactly like a missing one ("not found").

The graph is exercised against a real embedded Qdrant (same fixture style as
``test_graph.py``) so the ``find_entities`` scroll filter is evaluated by
Qdrant itself, not by a stub that mirrors our own logic.
"""

import importlib.util
import os
import uuid
from pathlib import Path

import pytest
from qdrant_client import QdrantClient, models

from nexus.graph.graph import SkillGraph
from nexus.graph.store import EdgeStore
from nexus.graph.traversal import GraphTraversal

from conftest import bind_test_collection, test_vector_name

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "plugins" / "claude-code" / "scripts"

COLLECTION = "test-graph-access"
DIM = 2


def fid(name: str) -> str:
    """Deterministic point ID (Qdrant local mode needs UUIDs or integers)."""
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, name))


class GraphFixture:
    """Real SkillGraph over embedded Qdrant, with access_levels per point."""

    def __init__(self, tmp_path: Path):
        self.client = QdrantClient(path=os.path.join(str(tmp_path), "qdrant"))
        binding = bind_test_collection(self.client, COLLECTION, DIM)
        self.store = EdgeStore(client=self.client, collection=COLLECTION, binding=binding)
        self.store.initialize()
        self.sg = SkillGraph(store=self.store)

    def add_fact(self, name: str, payload: dict) -> str:
        """Upsert a fact point. A payload without access_level stays without."""
        pid = fid(name)
        self.client.upsert(
            collection_name=COLLECTION,
            points=[models.PointStruct(
                id=pid,
                vector={test_vector_name(DIM): [0.0] * DIM},
                payload={"content": f"content of {name}", **payload},
            )],
        )
        return pid

    def add_entity(self, name: str, entity_type: str, payload: dict) -> str:
        return self.add_fact(name, {
            "category": "entity",
            "entity_type": entity_type,
            "entity_name": name,
            **payload,
        })

    def edge(self, source: str, target: str, relation: str = "connected_to") -> None:
        self.sg.add_edge(fid(source), fid(target), relation)

    def initialize(self) -> "GraphFixture":
        self.sg.initialize()
        return self

    def close(self) -> None:
        self.store.close()


@pytest.fixture
def graph(tmp_path):
    fixture = GraphFixture(tmp_path)
    try:
        yield fixture
    finally:
        fixture.close()


def _names(rows) -> list:
    """Entity names from find_entities rows, order-independent."""
    return sorted(r["name"] for r in rows)


# ── find_entities ───────────────────────────────────────────────────────────


class TestFindEntitiesAccessLevel:
    @pytest.fixture
    def entities(self, graph):
        graph.add_entity("pub", "device", {"access_level": "public"})
        graph.add_entity("tr", "device", {"access_level": "trusted"})
        graph.add_entity("priv", "device", {"access_level": "private"})
        graph.add_entity("nolevel", "device", {})  # no access_level at all
        graph.initialize()
        return graph

    def test_public_agent_sees_only_public(self, entities):
        gt = GraphTraversal(entities.sg, access_level="public")
        assert _names(gt.find_entities()) == ["pub"]

    def test_trusted_agent_sees_public_and_trusted(self, entities):
        gt = GraphTraversal(entities.sg, access_level="trusted")
        assert _names(gt.find_entities()) == ["pub", "tr"]

    def test_private_agent_sees_everything(self, entities):
        """No over-filtering: the owner's own private facts stay reachable.

        A point without ``access_level`` counts as private, so it is visible
        to a private agent too.
        """
        gt = GraphTraversal(entities.sg, access_level="private")
        assert _names(gt.find_entities()) == ["nolevel", "priv", "pub", "tr"]

    def test_entity_type_filter_still_applies(self, entities):
        gt = GraphTraversal(entities.sg, access_level="public")
        assert gt.find_entities(entity_type="person") == []


# ── traverse ────────────────────────────────────────────────────────────────


class TestTraverseAccessLevel:
    @pytest.fixture
    def graph_one_hop(self, graph):
        graph.add_fact("start", {"access_level": "public"})
        graph.add_fact("pub-n", {"access_level": "public"})
        graph.add_fact("tr-n", {"access_level": "trusted"})
        graph.add_fact("priv-n", {"access_level": "private"})
        graph.add_fact("nolevel-n", {})
        graph.edge("start", "pub-n")
        graph.edge("start", "tr-n")
        graph.edge("start", "priv-n")
        graph.edge("start", "nolevel-n")
        return graph.initialize()

    def test_public_agent_skips_private_and_trusted(self, graph_one_hop):
        gt = GraphTraversal(graph_one_hop.sg, access_level="public")
        found = [r["fact_id"] for r in gt.traverse(fid("start"), max_depth=1)]
        assert found == [fid("pub-n")]

    def test_missing_access_level_is_private(self, graph_one_hop):
        """A point without access_level is invisible to a public agent."""
        gt = GraphTraversal(graph_one_hop.sg, access_level="public")
        found = [r["fact_id"] for r in gt.traverse(fid("start"), max_depth=1)]
        assert fid("nolevel-n") not in found

    def test_visible_neighbor_passes_through_unchanged(self, graph_one_hop):
        gt = GraphTraversal(graph_one_hop.sg, access_level="trusted")
        rows = gt.traverse(fid("start"), max_depth=1)
        by_id = {r["fact_id"]: r for r in rows}
        assert by_id[fid("tr-n")] == {
            "fact_id": fid("tr-n"),
            "depth": 1,
            "relation": "connected_to",
            "path": [fid("tr-n")],
        }

    def test_default_level_is_public(self, graph_one_hop):
        """No level given → the most restrictive one, never the owner's."""
        gt = GraphTraversal(graph_one_hop.sg)
        found = [r["fact_id"] for r in gt.traverse(fid("start"), max_depth=1)]
        assert found == [fid("pub-n")]

    def test_unknown_level_sees_nothing(self, graph_one_hop):
        gt = GraphTraversal(graph_one_hop.sg, access_level="god-mode")
        assert gt.traverse(fid("start"), max_depth=1) == []

    def test_hidden_node_is_not_traversed_through(self, graph):
        """A -> hidden B -> C: a public agent must not reach C."""
        graph.add_fact("a", {"access_level": "public"})
        graph.add_fact("b", {"access_level": "private"})
        graph.add_fact("c", {"access_level": "public"})
        graph.edge("a", "b")
        graph.edge("b", "c")
        graph.initialize()

        public = GraphTraversal(graph.sg, access_level="public")
        assert public.traverse(fid("a"), max_depth=2) == []

        owner = GraphTraversal(graph.sg, access_level="private")
        reached = {r["fact_id"] for r in owner.traverse(fid("a"), max_depth=2)}
        assert reached == {fid("b"), fid("c")}

    def test_hidden_start_looks_like_missing_start(self, graph):
        """No existence leak: 'not found', never 'forbidden'."""
        graph.add_fact("secret", {"access_level": "private"})
        graph.add_fact("pub-child", {"access_level": "public"})
        graph.edge("secret", "pub-child")
        graph.initialize()

        public = GraphTraversal(graph.sg, access_level="public")
        hidden = public.traverse(fid("secret"), max_depth=2)
        missing = public.traverse(fid("never-existed"), max_depth=2)
        assert hidden == missing == []


# ── get_related ─────────────────────────────────────────────────────────────


class TestGetRelatedAccessLevel:
    @pytest.fixture
    def graph_related(self, graph):
        graph.add_fact("start", {"access_level": "public"})
        graph.add_fact("pub-n", {"access_level": "public"})
        graph.add_fact("priv-n", {"access_level": "private"})
        graph.edge("start", "pub-n", "manages")
        graph.edge("start", "priv-n", "manages")
        return graph.initialize()

    def test_public_agent_skips_private_neighbor(self, graph_related):
        gt = GraphTraversal(graph_related.sg, access_level="public")
        found = [n["fact_id"] for n in gt.get_related(fid("start"))]
        assert found == [fid("pub-n")]

    def test_private_agent_keeps_both(self, graph_related):
        gt = GraphTraversal(graph_related.sg, access_level="private")
        found = sorted(n["fact_id"] for n in gt.get_related(fid("start")))
        assert found == sorted([fid("pub-n"), fid("priv-n")])

    def test_hidden_start_returns_empty(self, graph_related):
        gt = GraphTraversal(graph_related.sg, access_level="public")
        assert gt.get_related(fid("priv-n")) == []


# ── get_subgraph ────────────────────────────────────────────────────────────


class TestGetSubgraphAccessLevel:
    @pytest.fixture
    def graph_sub(self, graph):
        graph.add_fact("start", {"access_level": "public"})
        graph.add_fact("pub-n", {"access_level": "public"})
        graph.add_fact("priv-n", {"access_level": "private"})
        graph.edge("start", "pub-n")
        graph.edge("start", "priv-n")
        return graph.initialize()

    def test_public_agent_skips_private_node_and_edge(self, graph_sub):
        gt = GraphTraversal(graph_sub.sg, access_level="public")
        result = gt.get_subgraph(fid("start"), max_depth=1)
        node_ids = {n["id"] for n in result["nodes"]}
        assert node_ids == {fid("start"), fid("pub-n")}
        assert all(e["target"] != fid("priv-n") for e in result["edges"])

    def test_hidden_start_returns_empty_subgraph(self, graph_sub):
        gt = GraphTraversal(graph_sub.sg, access_level="public")
        assert gt.get_subgraph(fid("priv-n"), max_depth=2) == {"nodes": [], "edges": []}


# ── Wiring: the Claude Code hook ────────────────────────────────────────────


def _load_graph_traverse():
    spec = importlib.util.spec_from_file_location(
        "graph_traverse_access", SCRIPTS / "graph_traverse.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeStore:
    def close(self) -> None:
        pass


class _FakeSkillGraph:
    def __init__(self, *args, **kwargs):
        self.store = _FakeStore()

    def initialize(self) -> None:
        pass


class TestClaudeCodeHookWiring:
    def test_hook_passes_resolved_level_to_the_core(self, monkeypatch):
        """graph_traverse.py must hand the agent's level to GraphTraversal."""
        import nexus.graph.traversal as traversal_mod
        import nexus.graph.graph as graph_mod

        mod = _load_graph_traverse()
        monkeypatch.setattr(mod, "_agent_level", lambda: "trusted")
        monkeypatch.setattr(graph_mod, "SkillGraph", _FakeSkillGraph)

        seen = {}

        class RecordingTraversal:
            def __init__(self, sg, access_level="public"):
                seen["access_level"] = access_level

            def traverse(self, *a, **k):
                return []

            def find_entities(self, *a, **k):
                return []

            def get_subgraph(self, *a, **k):
                return {"nodes": [], "edges": []}

            def get_related(self, *a, **k):
                return []

        monkeypatch.setattr(traversal_mod, "GraphTraversal", RecordingTraversal)

        mod.traverse(fid("a"))
        assert seen["access_level"] == "trusted"
        mod.find_entities()
        assert seen["access_level"] == "trusted"
        mod.get_subgraph(fid("a"))
        assert seen["access_level"] == "trusted"
        mod.get_related(fid("a"))
        assert seen["access_level"] == "trusted"

    def test_agent_level_fails_closed_to_public(self, monkeypatch):
        """An unreadable registry must not widen what the hook may see."""
        mod = _load_graph_traverse()
        monkeypatch.setitem(__import__("sys").modules, "scope_auto", None)
        assert mod._agent_level() == "public"


# ── The shared trust-level resolver ─────────────────────────────────────────


class TestScopeAutoResolveTrustLevel:
    def _module(self, monkeypatch):
        import sys
        monkeypatch.syspath_prepend(str(SCRIPTS))
        return importlib.import_module("scope_auto")

    def _write_registry(self, monkeypatch, tmp_path, payload: str):
        registry = tmp_path / ".nexus-memory" / "agents.json"
        registry.parent.mkdir(parents=True, exist_ok=True)
        registry.write_text(payload)
        monkeypatch.setenv("HOME", str(tmp_path))
        return registry

    def test_known_agent_gets_its_level(self, monkeypatch, tmp_path):
        mod = self._module(monkeypatch)
        self._write_registry(
            monkeypatch, tmp_path,
            '{"agents": [{"id": "claude-code", "trust_level": "trusted"}]}',
        )
        assert mod.resolve_trust_level() == "trusted"

    def test_unknown_agent_is_public(self, monkeypatch, tmp_path):
        mod = self._module(monkeypatch)
        self._write_registry(monkeypatch, tmp_path, '{"agents": []}')
        assert mod.resolve_trust_level() == "public"

    def test_invalid_level_is_public(self, monkeypatch, tmp_path):
        mod = self._module(monkeypatch)
        self._write_registry(
            monkeypatch, tmp_path,
            '{"agents": [{"id": "claude-code", "trust_level": "root"}]}',
        )
        assert mod.resolve_trust_level() == "public"

    def test_missing_registry_is_public(self, monkeypatch, tmp_path):
        mod = self._module(monkeypatch)
        monkeypatch.setenv("HOME", str(tmp_path))
        assert mod.resolve_trust_level() == "public"
