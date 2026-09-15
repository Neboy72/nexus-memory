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
import hashlib
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
    # try/finally: close on every exit path (early return, exception, success).
    # NB: ``with sqlite3.connect(...)`` only manages the transaction, it does
    # NOT close the connection — hence the explicit close here.
    try:
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
        return [dict(row) for row in cursor.fetchall()]
    finally:
        conn.close()


def _edge_id(source_fact_id: str, target_fact_id: str, relation: str) -> str:
    """Deterministic edge id for migrated edges (12 hex chars)."""
    raw = f"{source_fact_id}:{target_fact_id}:{relation}".encode()
    return hashlib.sha1(raw).hexdigest()[:12]


def group_edges_by_source(edges: list[dict]) -> dict[str, list[dict]]:
    """Group edges by source_fact_id for Qdrant payload injection.

    Payload keys must match what ``nexus.graph.store`` reads back:
    ``edge_id``/``target_fact_id``/``relation``/``status``. Legacy
    fields (target_name, confidence, context, source_doc_id) are kept
    as extra keys — the store ignores unknown keys.
    """
    grouped = {}
    for e in edges:
        source = e["source_fact_id"]
        if source not in grouped:
            grouped[source] = []
        grouped[source].append({
            "edge_id": _edge_id(source, e["target_fact_id"], e["relation"]),
            "target_fact_id": e["target_fact_id"],
            "relation": e["relation"],
            "status": e["status"],
            "target_name": "",
            "confidence": 1,
            "context": e.get("reason", ""),
            "source_doc_id": source,
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
    
    # Inject per point — MERGE into any edges already present on the point.
    # set_payload replaces the whole ``edges`` array, so writing the migrated
    # batch blindly would drop live/earlier-migrated edges.
    updated = 0
    errors = 0
    merged_total = 0  # newly appended migrated edges
    kept_total = 0    # pre-existing edges preserved on the points
    for source_id, payload_edges in grouped.items():
        try:
            # Fetch the point WITH payload so we can merge instead of replace.
            scroll_result = client.scroll(
                collection_name=collection,
                limit=1,
                filter=models.Filter(
                    must=[models.FieldCondition(
                        key="fact_id",
                        match=models.MatchValue(value=source_id),
                    )]
                ),
                with_payload=True,
            )

            if scroll_result[0]:
                existing_payload = scroll_result[0][0].payload or {}
                existing_edges = existing_payload.get("edges")
                if not isinstance(existing_edges, list):
                    existing_edges = []

                # Merge: keep existing entries, append only unseen edge_ids.
                merged_edges = list(existing_edges)
                known_ids = {
                    e.get("edge_id")
                    for e in existing_edges
                    if isinstance(e, dict)
                }
                appended = 0
                for e in payload_edges:
                    if e.get("edge_id") in known_ids:
                        continue
                    known_ids.add(e.get("edge_id"))
                    merged_edges.append(e)
                    appended += 1

                merged_total += appended
                kept_total += len(existing_edges)

                client.set_payload(
                    collection_name=collection,
                    payload={"edges": merged_edges},
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
    print(f"   {merged_total} edges merged, {kept_total} existing edges kept")
    print(f"   {errors} errors")

    return {
        "total_edges": len(edges),
        "points_updated": updated,
        "edges_merged": merged_total,
        "edges_kept": kept_total,
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
