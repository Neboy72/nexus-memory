"""Tests for the graph traversal module."""

import pytest
from unittest.mock import MagicMock, patch
from nexus.graph.traversal import GraphTraversal, KG_RELATIONS
from nexus.graph.schema import EdgeRelation


class TestKGRelations:
    """Test that KG relation types are correctly defined."""

    def test_kg_relations_present(self):
        assert "manages" in KG_RELATIONS
        assert "runs_on" in KG_RELATIONS
        assert "connected_to" in KG_RELATIONS
        assert "installed_at" in KG_RELATIONS

    def test_kg_relations_excludes_core(self):
        """Core relations (supports, contradicts) should NOT be in KG_RELATIONS."""
        assert "supports" not in KG_RELATIONS
        assert "contradicts" not in KG_RELATIONS
        assert "supersedes" not in KG_RELATIONS

    def test_new_edge_relations_in_enum(self):
        """New KG relations should be in EdgeRelation enum."""
        assert EdgeRelation.INSTALLED_AT.value == "installed_at"
        assert EdgeRelation.MANAGES.value == "manages"
        assert EdgeRelation.RUNS_ON.value == "runs_on"
        assert EdgeRelation.CONTROLS.value == "controls"
        assert EdgeRelation.USES.value == "uses"
        assert EdgeRelation.PROVIDES.value == "provides"


class TestGraphTraversal:
    """Tests for GraphTraversal (mocked SkillGraph)."""

    def test_traverse_empty_graph(self):
        """traverse() on non-existent node returns []."""
        mock_graph = MagicMock()
        mock_graph.has_node.return_value = False
        gt = GraphTraversal(mock_graph)
        result = gt.traverse("nonexistent")
        assert result == []

    def test_traverse_no_neighbors(self):
        """Node with no neighbors returns []."""
        mock_graph = MagicMock()
        mock_graph.has_node.return_value = True
        mock_graph.neighbors.return_value = []
        gt = GraphTraversal(mock_graph)
        result = gt.traverse("lonely-node")
        assert result == []

    def test_traverse_one_hop(self):
        """Single-hop traversal finds immediate neighbors."""
        mock_graph = MagicMock()
        mock_graph.has_node.return_value = True
        mock_graph.neighbors.return_value = [
            {"fact_id": "neighbor-1", "relation": "manages", "edge_id": "e1"},
        ]
        gt = GraphTraversal(mock_graph)
        result = gt.traverse("start", max_depth=1)
        assert len(result) == 1
        assert result[0]["fact_id"] == "neighbor-1"
        assert result[0]["depth"] == 1
        assert result[0]["relation"] == "manages"

    def test_traverse_two_hops(self):
        """Two-hop traversal finds neighbors of neighbors."""
        mock_graph = MagicMock()
        mock_graph.has_node.return_value = True

        # First call: start node has 1 neighbor
        # Second call: neighbor has 1 more neighbor
        mock_graph.neighbors.side_effect = [
            [{"fact_id": "mid", "relation": "runs_on", "edge_id": "e1"}],
            [{"fact_id": "end", "relation": "connected_to", "edge_id": "e2"}],
            [],  # end node has no more neighbors
        ]
        gt = GraphTraversal(mock_graph)
        result = gt.traverse("start", max_depth=2)
        assert len(result) >= 1
        # Should find "mid" at depth 1
        depth1 = [r for r in result if r["depth"] == 1]
        assert len(depth1) == 1
        assert depth1[0]["fact_id"] == "mid"

    def test_traverse_avoids_cycles(self):
        """Traversal should not revisit nodes."""
        mock_graph = MagicMock()
        mock_graph.has_node.return_value = True
        # start -> A, A -> start (cycle)
        mock_graph.neighbors.side_effect = [
            [{"fact_id": "A", "relation": "manages", "edge_id": "e1"}],
            [{"fact_id": "start", "relation": "manages", "edge_id": "e2"}],
        ]
        gt = GraphTraversal(mock_graph)
        result = gt.traverse("start", max_depth=5)
        # Should not include "start" again
        fact_ids = [r["fact_id"] for r in result]
        assert "start" not in fact_ids
        assert "A" in fact_ids

    def test_traverse_relation_filter(self):
        """Relation filter should pass through to neighbors()."""
        mock_graph = MagicMock()
        mock_graph.has_node.return_value = True
        mock_graph.neighbors.return_value = []
        gt = GraphTraversal(mock_graph)
        gt.traverse("start", relation="manages")
        mock_graph.neighbors.assert_called_with("start", relation="manages")

    def test_get_related(self):
        """get_related returns immediate neighbors."""
        mock_graph = MagicMock()
        mock_graph.neighbors.return_value = [
            {"fact_id": "n1", "relation": "manages", "edge_id": "e1"},
        ]
        gt = GraphTraversal(mock_graph)
        result = gt.get_related("start")
        assert len(result) == 1
        assert result[0]["fact_id"] == "n1"

    def test_get_subgraph_empty(self):
        """get_subgraph on non-existent node returns empty."""
        mock_graph = MagicMock()
        mock_graph.has_node.return_value = False
        gt = GraphTraversal(mock_graph)
        result = gt.get_subgraph("nonexistent")
        assert result == {"nodes": [], "edges": []}

    def test_get_subgraph_with_neighbors(self):
        """get_subgraph returns nodes and edges."""
        mock_graph = MagicMock()
        mock_graph.has_node.return_value = True
        mock_graph.neighbors.return_value = [
            {"fact_id": "n1", "relation": "manages", "edge_id": "e1"},
        ]
        gt = GraphTraversal(mock_graph)
        result = gt.get_subgraph("start", max_depth=1)
        assert len(result["nodes"]) >= 1
        assert "start" in [n["id"] for n in result["nodes"]]

    def test_stats_includes_kg_edges(self):
        """stats() should include kg_edges count."""
        mock_graph = MagicMock()
        mock_graph.stats.return_value = {"nodes": 10, "edges": 5}
        mock_graph.list_edges.return_value = []
        gt = GraphTraversal(mock_graph)
        stats = gt.stats()
        assert "kg_edges" in stats
        assert "total_relations" in stats