"""Scoring — isolation, hub, and knowledge-gap detection on SkillGraph.

v2.1.0: All functions operate on a ``SkillGraph`` instance and access
its internal ``nx.DiGraph`` for pure graph math.

Usage::

    from nexus.analytics.scoring import isolation_score, hub_scores

    score = isolation_score(skillgraph, "fact-123")
    hubs = hub_scores(skillgraph, top_n=5)
"""

from __future__ import annotations

import logging

import networkx as nx

from nexus.graph.graph import SkillGraph

_logger = logging.getLogger(__name__)


def _graph(sg: SkillGraph) -> nx.DiGraph:
    """Safely access internal NetworkX graph from SkillGraph."""
    return sg._graph


def hub_scores(sg: SkillGraph, top_n: int = 10) -> list[dict]:
    """Find the most connected facts (hub scoring by total degree).

    Args:
        sg: Initialised ``SkillGraph`` instance.
        top_n: Number of top hubs to return.

    Returns:
        List of ``{"fact_id", "degree", "in_degree", "out_degree"}``
        sorted by total degree descending.
    """
    graph = _graph(sg)
    if graph.order() == 0:
        return []

    scores = []
    for node in graph.nodes():
        scores.append({
            "fact_id": node,
            "degree": graph.degree(node),
            "in_degree": graph.in_degree(node),
            "out_degree": graph.out_degree(node),
        })

    scores.sort(key=lambda x: x["degree"], reverse=True)
    return scores[:top_n]


def isolation_score(sg: SkillGraph, fact_id: str) -> dict:
    """Compute how isolated a fact is in the knowledge graph.

    Returns:
        ``{"fact_id", "degree", "is_isolated", "neighbor_count"}``.
    """
    graph = _graph(sg)
    if not graph.has_node(fact_id):
        return {
            "fact_id": fact_id,
            "degree": 0,
            "is_isolated": True,
            "neighbor_count": 0,
            "error": "Fact not in graph (no edges yet)",
        }

    total_deg = graph.degree(fact_id)
    return {
        "fact_id": fact_id,
        "degree": total_deg,
        "in_degree": graph.in_degree(fact_id),
        "out_degree": graph.out_degree(fact_id),
        "is_isolated": total_deg == 0,
        "neighbor_count": total_deg,
    }


def knowledge_gaps(sg: SkillGraph, isolation_threshold: float = 0.9) -> list[dict]:
    """Find isolated or near-isolated facts.

    Args:
        sg: Initialised ``SkillGraph`` instance.
        isolation_threshold: ``1 / (1 + degree) >= threshold`` to be a gap.
            Default 0.9 means degree 0 or 1 are gaps.

    Returns:
        List of ``{"fact_id", "degree": N, "isolation_score": float}``.
    """
    graph = _graph(sg)
    if graph.order() == 0:
        return []

    gaps = []
    for node in graph.nodes():
        deg = graph.degree(node)
        score = 1.0 / (1.0 + deg) if deg >= 0 else 1.0
        if score >= isolation_threshold:
            gaps.append({
                "fact_id": node,
                "degree": deg,
                "isolation_score": round(score, 4),
            })

    return gaps


def relation_distribution(sg: SkillGraph) -> dict[str, int]:
    """Count edges by relation type.

    Returns:
        ``{"references": N, "depends_on": N, ...}``
    """
    graph = _graph(sg)
    counts: dict[str, int] = {}

    for _, _, data in graph.edges(data=True):
        rel = data.get("relation", "unknown")
        counts[rel] = counts.get(rel, 0) + 1

    return dict(sorted(counts.items(), key=lambda x: x[1], reverse=True))
