#!/usr/bin/env python3
"""Claude Code Plugin — Knowledge Graph Traversal Script.

Provides graph traversal, entity search, subgraph, and related-facts queries
for Claude Code via PreToolExecution / hooks.

Usage:
    python3 graph_traverse.py --action traverse --fact-id <id> [--max-depth 3]
    python3 graph_traverse.py --action find_entities [--entity-type device]
    python3 graph_traverse.py --action subgraph --fact-id <id>
    python3 graph_traverse.py --action related --fact-id <id>
"""

import argparse
import json
import os
import sys

# Add nexus-memory to path
sys.path.insert(0, os.path.expanduser("~/nexus-memory"))

QDRANT_URL = os.environ.get("NEXUS_QDRANT_URL", "http://localhost:6333")
COLLECTION = os.environ.get("NEXUS_COLLECTION", "nexus")


def traverse(fact_id: str, max_depth: int = 3, relation: str = "", target_type: str = "") -> dict:
    """Multi-hop BFS traversal from a starting fact."""
    from nexus.graph.graph import SkillGraph
    from nexus.graph.traversal import GraphTraversal

    sg = SkillGraph(qdrant_url=QDRANT_URL, collection=COLLECTION)
    sg.initialize()
    gt = GraphTraversal(sg)
    results = gt.traverse(
        fact_id,
        max_depth=max_depth,
        relation=relation or None,
        target_type=target_type or None,
    )
    sg.store.close()
    return {"results": results}


def find_entities(entity_type: str = "", limit: int = 50) -> dict:
    """Find all entity-typed memories."""
    from nexus.graph.graph import SkillGraph
    from nexus.graph.traversal import GraphTraversal

    sg = SkillGraph(qdrant_url=QDRANT_URL, collection=COLLECTION)
    sg.initialize()
    gt = GraphTraversal(sg)
    results = gt.find_entities(
        entity_type=entity_type or None,
        limit=limit,
    )
    sg.store.close()
    return {"entities": results}


def get_subgraph(fact_id: str, max_depth: int = 2) -> dict:
    """Get a subgraph centered on a fact."""
    from nexus.graph.graph import SkillGraph
    from nexus.graph.traversal import GraphTraversal

    sg = SkillGraph(qdrant_url=QDRANT_URL, collection=COLLECTION)
    sg.initialize()
    gt = GraphTraversal(sg)
    result = gt.get_subgraph(fact_id, max_depth=max_depth)
    sg.store.close()
    return result


def get_related(fact_id: str, relation: str = "") -> dict:
    """Get directly related facts (1-hop)."""
    from nexus.graph.graph import SkillGraph
    from nexus.graph.traversal import GraphTraversal

    sg = SkillGraph(qdrant_url=QDRANT_URL, collection=COLLECTION)
    sg.initialize()
    gt = GraphTraversal(sg)
    results = gt.get_related(fact_id, relation=relation or None)
    sg.store.close()
    return {"results": results}


def main():
    parser = argparse.ArgumentParser(description="Nexus Memory Knowledge Graph")
    parser.add_argument("--action", required=True,
                        choices=["traverse", "find_entities", "subgraph", "related"],
                        help="Query action to perform")
    parser.add_argument("--fact-id", default="", help="Qdrant point ID (for traverse/subgraph/related)")
    parser.add_argument("--max-depth", type=int, default=3, help="Maximum hops")
    parser.add_argument("--relation", default="", help="Filter by relation type")
    parser.add_argument("--entity-type", default="", help="Filter by entity type (for find_entities)")
    parser.add_argument("--limit", type=int, default=50, help="Max results (for find_entities)")
    args = parser.parse_args()

    try:
        if args.action == "traverse":
            if not args.fact_id:
                print(json.dumps({"error": "fact-id required for traverse"}))
                return
            result = traverse(args.fact_id, args.max_depth, args.relation, args.entity_type)
        elif args.action == "find_entities":
            result = find_entities(args.entity_type, args.limit)
        elif args.action == "subgraph":
            if not args.fact_id:
                print(json.dumps({"error": "fact-id required for subgraph"}))
                return
            result = get_subgraph(args.fact_id, args.max_depth)
        elif args.action == "related":
            if not args.fact_id:
                print(json.dumps({"error": "fact-id required for related"}))
                return
            result = get_related(args.fact_id, args.relation)
        else:
            print(json.dumps({"error": f"Unknown action: {args.action}"}))
            return

        print(json.dumps(result, indent=2))
    except Exception as e:
        print(json.dumps({"status": "error", "error": str(e)}))


if __name__ == "__main__":
    main()