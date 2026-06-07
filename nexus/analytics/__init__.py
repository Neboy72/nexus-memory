"""Graph Analytics — analysis and reports over the SkillGraph.

v2.1.0: Provides insight into the graph structure:
  - Hub Scores (most-connected facts)
  - Isolation Scores (isolated facts = knowledge gaps)
  - Clustering (Connected Components)
  - Knowledge Gaps Report

Usage:
    from nexus.analytics import GraphAnalytics
    analytics = GraphAnalytics(skillgraph)
    report = analytics.full_report()
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from nexus.graph.graph import SkillGraph
from nexus.analytics.scoring import (
    hub_scores,
    isolation_score,
    knowledge_gaps,
    relation_distribution,
)
from nexus.analytics.clustering import cluster_summary, find_clusters

_logger = logging.getLogger(__name__)


class GraphAnalytics:
    """Analysis and reporting layer over SkillGraph."""

    def __init__(self, skillgraph: SkillGraph):
        self._sg = skillgraph

    @property
    def graph(self) -> SkillGraph:
        return self._sg

    # ── Individual queries ─────────────────────────────────────────────────

    def hubs(self, top_n: int = 10) -> list[dict]:
        """Return the most connected facts."""
        return hub_scores(self._sg, top_n=top_n)

    def isolation(self, fact_id: str) -> dict:
        """Check how isolated a specific fact is."""
        return isolation_score(self._sg, fact_id)

    def gaps(self) -> list[dict]:
        """Find isolated facts with no edges."""
        return knowledge_gaps(self._sg)

    def clusters(self, min_size: int = 2) -> list[dict]:
        """Find knowledge clusters (connected components)."""
        return find_clusters(self._sg, min_size=min_size)

    def relations(self) -> dict[str, int]:
        """Count edges by relation type."""
        return relation_distribution(self._sg)

    # ── Full report ───────────────────────────────────────────────────────

    def full_report(self) -> dict:
        """Generate a comprehensive graph analytics report.

        Returns::

            {
                "graph_stats": {
                    "nodes": int,
                    "edges": int,
                    "db_path": str,
                },
                "top_hubs": [{"fact_id", "degree"}, ...],
                "relation_distribution": {"references": N, ...},
                "clusters": cluster_summary dict,
                "knowledge_gaps": int,  # number of isolated facts
                "gap_examples": [fact_id, ...],  # first 5 gaps
            }
        """
        stats = self._sg.stats()
        hubs = self.hubs(top_n=5)
        gap_list = self.gaps()
        clusters = cluster_summary(self._sg)
        relations = self.relations()

        return {
            "graph_stats": stats,
            "top_hubs": [
                {"fact_id": h["fact_id"], "degree": h["degree"]}
                for h in hubs
            ],
            "relation_distribution": relations,
            "clusters": clusters,
            "knowledge_gaps": len(gap_list),
            "gap_examples": [g["fact_id"] for g in gap_list[:5]],
        }

    def report_text(self, report: Optional[dict] = None) -> str:
        """Format the full report as human-readable text.

        Args:
            report: Pre-computed report (from full_report()). If None,
                    generates a fresh one.

        Returns:
            Formatted text with emoji headers.
        """
        if report is None:
            report = self.full_report()

        lines = ["📊 **SkillGraph Analytics Report**\n"]

        # Stats
        s = report["graph_stats"]
        lines.append(f"**Graph**: {s.get('nodes', 0)} nodes, {s.get('edges', 0)} edges")
        lines.append("")

        # Top hubs
        if report["top_hubs"]:
            lines.append("**🔥 Top Hubs (most connected):**")
            for h in report["top_hubs"]:
                lines.append(f"  • `{h['fact_id'][:24]}...` — {h['degree']} edges")
            lines.append("")

        # Relation distribution
        if report["relation_distribution"]:
            lines.append("**🔗 Relation Distribution:**")
            for rel, count in report["relation_distribution"].items():
                lines.append(f"  • **{rel}**: {count}")
            lines.append("")

        # Clusters
        c = report["clusters"]
        lines.append(
            f"**🧩 Clusters**: {c.get('num_clusters', 0)} clusters, "
            f"{c.get('singletons', 0)} singletons"
        )
        if c.get("largest_cluster_size", 0) > 0:
            lines.append(f"  • Largest cluster: {c['largest_cluster_size']} facts")

        # Knowledge gaps
        gaps = report.get("knowledge_gaps", 0)
        if gaps > 0:
            lines.append(f"\n**⚠️ Knowledge Gaps**: {gaps} isolated facts")
            for g in report.get("gap_examples", []):
                lines.append(f"  • `{g[:24]}...`")
            if gaps > 5:
                lines.append(f"  • ... and {gaps - 5} more")

        return "\n".join(lines)
