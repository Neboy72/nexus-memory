"""
Migration: SQLite skillgraph.db → Qdrant Payload Edges.

Reads existing edges from a v2.0.x SQLite database (skillgraph.db)
and writes them as `edges` arrays into Qdrant point payloads.

Usage:
    python3 -m nexus.scripts.migrate \\
        --db /path/to/skillgraph.db \\
        --collection hermes-memory \\
        --qdrant-url http://localhost:6333
"""

import argparse
import logging
import sqlite3
import sys
from typing import Optional

from qdrant_client import QdrantClient
from qdrant_client.http import models

from nexus.graph.schema import EdgeRelation, EdgeStatus

_logger = logging.getLogger(__name__)


def read_edges_from_sqlite(db_path: str) -> list[dict]:
    """Lies alle aktiven Edges aus der SQLite-Datenbank."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    # Check if edges table exists
    cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='edges'"
    )
    if not cursor.fetchone():
        print(f"⚠️  Table 'edges' not found in {db_path}")
        return []
    
    cursor.execute(
        "SELECT * FROM edges WHERE status = 'active' ORDER BY created_at"
    )
    rows = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return rows


def group_edges_by_source(edges: list[dict]) -> dict[str, list[dict]]:
    """Group edges by source_fact_id for Qdrant payload injection."""
    grouped = {}
    for e in edges:
        source = e["source_fact_id"]
        if source not in grouped:
            grouped[source] = []
        grouped[source].append({
            "target_id": e["target_fact_id"],
            "target_name": "",
            "relation_type": e["relation"],
            "confidence": 1,
            "context": e.get("reason", ""),
            "source_doc_id": source,
            "status": e["status"],
            "created_at": e.get("created_at", ""),
        })
    return grouped


def migrate(
    db_path: str,
    collection: str,
    qdrant_url: str = "http://localhost:6333",
    dry_run: bool = False,
) -> dict:
    """Run migration: SQLite → Qdrant Payloads.

    Returns: dict with statistics.
    """
    print(f"\n🔍 Lese Edges aus: {db_path}")
    edges = read_edges_from_sqlite(db_path)
    print(f"   {len(edges)} aktive Edges gefunden")
    
    if not edges:
        return {"total_edges": 0, "points_updated": 0, "dry_run": dry_run}
    
    grouped = group_edges_by_source(edges)
    print(f"   {len(grouped)} Quell-Facts mit Edges")
    
    if dry_run:
        print(f"\n✅ Dry-Run abgeschlossen. {len(edges)} Edges zu migrieren.")
        return {
            "total_edges": len(edges),
            "points_updated": len(grouped),
            "dry_run": True,
        }
    
    # Verbinde zu Qdrant
    print(f"\n🔗 Verbinde zu Qdrant: {qdrant_url}")
    client = QdrantClient(url=qdrant_url)
    
    # Check collection
    collections = [c.name for c in client.get_collections().collections]
    if collection not in collections:
        print(f"⚠️  Collection '{collection}' does not exist in Qdrant")
        return {"error": f"collection '{collection}' not found"}
    
    print(f"   Collection '{collection}' found")
    
    # Inject per point
    updated = 0
    errors = 0
    for source_id, payload_edges in grouped.items():
        try:
            # Check if the point exists
            scroll_result = client.scroll(
                collection_name=collection,
                limit=1,
                filter=models.Filter(
                    must=[models.FieldCondition(
                        key="fact_id",
                        match=models.MatchValue(value=source_id),
                    )]
                ),
                with_payload=False,
            )
            
            if scroll_result[0]:
                client.set_payload(
                    collection_name=collection,
                    payload={"edges": payload_edges},
                    points=[source_id],
                )
                updated += 1
                if updated % 10 == 0:
                    print(f"   Progress: {updated}/{len(grouped)} points updated")
            else:
                _logger.warning(f"Point {source_id} not found in collection — skipping")
                errors += 1
        except Exception as e:
            _logger.error(f"Error on point {source_id}: {e}")
            errors += 1
    
    print(f"\n✅ Migration complete:")
    print(f"   {len(edges)} Edges read")
    print(f"   {updated} Qdrant points updated")
    print(f"   {errors} errors")
    
    return {
        "total_edges": len(edges),
        "points_updated": updated,
        "errors": errors,
    }


def main():
    parser = argparse.ArgumentParser(description="Migrate SQLite skillgraph.db → Qdrant Payload Edges")
    parser.add_argument("--db", required=True, help="Path to skillgraph.db")
    parser.add_argument("--collection", required=True, help="Qdrant collection name")
    parser.add_argument("--qdrant-url", default="http://localhost:6333", help="Qdrant HTTP URL")
    parser.add_argument("--dry-run", action="store_true", help="Read only, do not write")
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable logging")
    
    args = parser.parse_args()
    
    if args.verbose:
        logging.basicConfig(level=logging.INFO)
    
    result = migrate(
        db_path=args.db,
        collection=args.collection,
        qdrant_url=args.qdrant_url,
        dry_run=args.dry_run,
    )
    
    if "error" in result:
        print(f"\n❌ {result['error']}")
        sys.exit(1)


if __name__ == "__main__":
    main()
