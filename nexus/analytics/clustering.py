"""Clustering — Connected Components analysis for SkillGraph.

v2.1.0: Uses NetworkX's built-in connected components to find
knowledge clusters in the SkillGraph.

Usage:
    from nexus.analytics.clustering import find_clusters, cluster_summary
    clusters = find_clusters(skillgraph)
"""

from __future__ import annotations

import networkx as nx

from nexus.graph.graph import SkillGraph

MIN_CLUSTER_SIZE = 2  # Clusters smaller than this are "singletons"


# W30-4: SkillGraph.graph is a MultiDiGraph (parallel relations per node pair).
def _build_clusters(graph: nx.MultiDiGraph, min_size: int) -> list[dict]:
    """Collect clusters from *graph* — shared by both public functions.

    Uses weakly connected components (undirected clusters), keeps only
    components with at least *min_size* nodes, sorts them by size descending
    and re-numbers their IDs 1..N. Single implementation prevents the ID
    drift that previously existed between ``find_clusters`` and
    ``cluster_summary``.
    """
    components = list(nx.weakly_connected_components(graph))

    clusters: list[dict] = []
    for component in components:
        members = sorted(component)
        if len(members) >= min_size:
            clusters.append({
                "cluster_id": 0,  # re-numbered below after sorting
                "size": len(members),
                "members": members,
            })

    clusters.sort(key=lambda x: x["size"], reverse=True)
    for idx, c in enumerate(clusters, start=1):
        c["cluster_id"] = idx
    return clusters


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
        sorted by size descending, re-numbered 1..N (identical numbering
        to ``cluster_summary``).
    """
    graph = sg.graph
    if graph.order() == 0:
        return []

    return _build_clusters(graph, min_size)


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
    graph = sg.graph
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

    clusters = _build_clusters(graph, MIN_CLUSTER_SIZE)
    largest = clusters[0]["size"] if clusters else 0
    # Nodes in the filtered clusters; the remainder are singletons (with
    # MIN_CLUSTER_SIZE == 2 every excluded component is a single node, so this
    # equals the old count of size-1 components).
    clustered_nodes = sum(c["size"] for c in clusters)
    singletons = total_nodes - clustered_nodes

    return {
        "total_nodes": total_nodes,
        "total_edges": total_edges,
        "num_clusters": len(clusters),
        "largest_cluster_size": largest,
        "singletons": singletons,
        "clusters": clusters,
    }
