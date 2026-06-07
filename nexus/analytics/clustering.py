"""Clustering — Connected Components analysis for SkillGraph.

v2.1.0: Uses NetworkX's built-in connected components to find
knowledge clusters in the SkillGraph.

Usage:
    from nexus.analytics.clustering import find_clusters, cluster_summary
    clusters = find_clusters(skillgraph)
"""

from __future__ import annotations

import logging

import networkx as nx

from nexus.graph.graph import SkillGraph

_logger = logging.getLogger(__name__)

MIN_CLUSTER_SIZE = 2  # Clusters smaller than this are "singletons"


def find_clusters(
    sg: SkillGraph,
    min_size: int = MIN_CLUSTER_SIZE,
) -> list[dict]:
    """Find weakly connected components (clusters) in the graph.

    Since SkillGraph is a DiGraph, we use the undirected version
    for clustering (weakly connected components).

    Args:
        sg: Initialised ``SkillGraph`` instance.
        min_size: Minimum cluster size to include.

    Returns:
        List of ``{"cluster_id", "size", "members": [fact_id, ...]}``
        sorted by size descending.
    """
    graph = sg._graph
    if graph.order() == 0:
        return []

    # Use weakly connected components (undirected clusters)
    components = list(nx.weakly_connected_components(graph))

    clusters = []
    for i, component in enumerate(components):
        members = sorted(component)
        if len(members) >= min_size:
            clusters.append({
                "cluster_id": i + 1,
                "size": len(members),
                "members": members,
            })

    clusters.sort(key=lambda x: x["size"], reverse=True)
    return clusters


def cluster_summary(sg: SkillGraph) -> dict:
    """Generate a summary of all clusters in the graph.

    Returns::

        {
            "total_nodes": int,
            "total_edges": int,
            "num_clusters": int,
            "largest_cluster_size": int,
            "singletons": int,
            "clusters": [{"cluster_id", "size", "members"}, ...],
        }
    """
    graph = sg._graph
    total_nodes = graph.order()
    total_edges = graph.size()

    if total_nodes == 0:
        return {
            "total_nodes": 0,
            "total_edges": 0,
            "num_clusters": 0,
            "largest_cluster_size": 0,
            "singletons": 0,
            "clusters": [],
        }

    components = list(nx.weakly_connected_components(graph))

    clusters = []
    singletons = 0
    largest = 0

    for i, component in enumerate(components):
        members = sorted(component)
        size = len(members)
        if size >= MIN_CLUSTER_SIZE:
            clusters.append({
                "cluster_id": i + 1,
                "size": size,
                "members": members,
            })
            largest = max(largest, size)
        else:
            singletons += 1

    clusters.sort(key=lambda x: x["size"], reverse=True)

    # Re-number after sorting
    for idx, c in enumerate(clusters):
        c["cluster_id"] = idx + 1

    return {
        "total_nodes": total_nodes,
        "total_edges": total_edges,
        "num_clusters": len(clusters),
        "largest_cluster_size": largest,
        "singletons": singletons,
        "clusters": clusters,
    }
