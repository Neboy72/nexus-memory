#!/usr/bin/env python3
"""Claude Code Plugin — Knowledge Graph Traversal Script.

Provides graph traversal, entity search, subgraph, and related-facts queries
for Claude Code via PreToolExecution / hooks.

Usage:
    python3 graph_traverse.py --action traverse --fact-id <id> [--max-depth 3]
    python3 graph_traverse.py --action find_entities [--entity-type device] [--limit 50]
    python3 graph_traverse.py --action subgraph --fact-id <id> [--max-depth 2]
    python3 graph_traverse.py --action related --fact-id <id>

--max-depth defaults to 3 for traverse and 2 for subgraph; --entity-type also
acts as the target_type filter for traverse.
"""

import argparse
import json
import os
import sys

# Append (never prepend) nexus-memory to the module search path: a directory
# writable by the invoking user at sys.path[0] could shadow stdlib or
# third-party modules (import hijack).
sys.path.append(os.path.expanduser("~/nexus-memory"))

QDRANT_URL = os.environ.get("NEXUS_QDRANT_URL", "http://localhost:6333")
COLLECTION = os.environ.get("NEXUS_COLLECTION", "nexus")

# Upper bounds for user-supplied integers: an unbounded depth walks the whole
# graph, a negative limit is forwarded to the Qdrant scroll.
MAX_DEPTH_LIMIT = 10
LIMIT_MAX = 1000


def _emit_error(message: str) -> None:
    """Print the single error schema to stdout (pinned stdout contract, H1).

    Every failure path uses this shape so callers can branch on ``status``
    regardless of whether validation or the backend failed.
    """
    print(json.dumps({"status": "error", "error": message}))


def traverse(fact_id: str, max_depth: int = 3, relation: str = "", target_type: str = "") -> dict:
    """Multi-hop BFS traversal from a starting fact."""
    from nexus.graph.graph import SkillGraph
    from nexus.graph.traversal import GraphTraversal

    sg = SkillGraph(qdrant_url=QDRANT_URL, collection=COLLECTION)
    try:
        sg.initialize()
        gt = GraphTraversal(sg)
        results = gt.traverse(
            fact_id,
            max_depth=max_depth,
            relation=relation or None,
            target_type=target_type or None,
        )
        return {"results": results}
    finally:
        sg.store.close()


def find_entities(entity_type: str = "", limit: int = 50) -> dict:
    """Find all entity-typed memories."""
    from nexus.graph.graph import SkillGraph
    from nexus.graph.traversal import GraphTraversal

    sg = SkillGraph(qdrant_url=QDRANT_URL, collection=COLLECTION)
    try:
        sg.initialize()
        gt = GraphTraversal(sg)
        results = gt.find_entities(
            entity_type=entity_type or None,
            limit=limit,
        )
        return {"entities": results}
    finally:
        sg.store.close()


def get_subgraph(fact_id: str, max_depth: int = 2) -> dict:
    """Get a subgraph centered on a fact."""
    from nexus.graph.graph import SkillGraph
    from nexus.graph.traversal import GraphTraversal

    sg = SkillGraph(qdrant_url=QDRANT_URL, collection=COLLECTION)
    try:
        sg.initialize()
        gt = GraphTraversal(sg)
        result = gt.get_subgraph(fact_id, max_depth=max_depth)
        return result
    finally:
        sg.store.close()


def get_related(fact_id: str, relation: str = "") -> dict:
    """Get directly related facts (1-hop)."""
    from nexus.graph.graph import SkillGraph
    from nexus.graph.traversal import GraphTraversal

    sg = SkillGraph(qdrant_url=QDRANT_URL, collection=COLLECTION)
    try:
        sg.initialize()
        gt = GraphTraversal(sg)
        results = gt.get_related(fact_id, relation=relation or None)
        return {"results": results}
    finally:
        sg.store.close()


def main():
    parser = argparse.ArgumentParser(description="Nexus Memory Knowledge Graph")
    parser.add_argument("--action", required=True,
                        choices=["traverse", "find_entities", "subgraph", "related"],
                        help="Query action to perform")
    parser.add_argument("--fact-id", default="", help="Qdrant point ID (for traverse/subgraph/related)")
    parser.add_argument("--max-depth", type=int, default=None,
                        help=f"Maximum hops 1-{MAX_DEPTH_LIMIT} (default: 3 for traverse, 2 for subgraph)")
    parser.add_argument("--relation", default="", help="Filter by relation type")
    parser.add_argument("--entity-type", default="",
                        help="Filter by entity type (find_entities; for traverse it filters target_type)")
    parser.add_argument("--limit", type=int, default=50,
                        help=f"Max results for find_entities (1-{LIMIT_MAX})")
    args = parser.parse_args()

    # Validate ranges before they reach the traversal / Qdrant scroll layer.
    if args.max_depth is not None and not 1 <= args.max_depth <= MAX_DEPTH_LIMIT:
        _emit_error(f"--max-depth must be between 1 and {MAX_DEPTH_LIMIT}")
        sys.exit(1)
    if not 1 <= args.limit <= LIMIT_MAX:
        _emit_error(f"--limit must be between 1 and {LIMIT_MAX}")
        sys.exit(1)

    try:
        if args.action == "traverse":
            if not args.fact_id:
                _emit_error("fact-id required for traverse")
                sys.exit(1)
            result = traverse(args.fact_id, args.max_depth if args.max_depth is not None else 3,
                              args.relation, args.entity_type)
        elif args.action == "find_entities":
            result = find_entities(args.entity_type, args.limit)
        elif args.action == "subgraph":
            if not args.fact_id:
                _emit_error("fact-id required for subgraph")
                sys.exit(1)
            # Only forward an explicit --max-depth: get_subgraph()'s own
            # default (2) must win when the flag was omitted (the shared
            # parser default of 3 walked one hop deeper than documented).
            if args.max_depth is None:
                result = get_subgraph(args.fact_id)
            else:
                result = get_subgraph(args.fact_id, args.max_depth)
        elif args.action == "related":
            if not args.fact_id:
                _emit_error("fact-id required for related")
                sys.exit(1)
            result = get_related(args.fact_id, args.relation)
        else:
            _emit_error(f"Unknown action: {args.action}")
            sys.exit(1)

        print(json.dumps(result, indent=2))
    except Exception as e:
        # Include the exception type: an opaque str(e) made backend failures
        # (Qdrant/connection) indistinguishable from programming errors.
        _emit_error(f"{type(e).__name__}: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()