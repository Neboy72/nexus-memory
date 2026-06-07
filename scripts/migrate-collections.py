#!/usr/bin/env python3
"""
Migrate all Points from old collections (hermes-memory, openclaw-memory, etc.)
into the unified 'nexus' Collection.

Strategy:
  1. Read all Points (with vectors + payload) from source collections
  2. Deduplicate by content hash (same text = same hash)
  3. Batch upsert into 'nexus' collection
  4. Add 'source_collection' tag to each point for provenance

Dry-run with --dry-run flag.
"""

import argparse
import hashlib
import sys
import time
from collections import defaultdict
from typing import Optional

import requests

QDRANT_HOST = "http://localhost:6333"
BATCH_SIZE = 100  # upsert batch
SCROLL_LIMIT = 1000  # scroll page size
TARGET_COLLECTION = "nexus"

SOURCE_COLLECTIONS = [
    "hermes-memory",
    "openclaw-memory",
]


def qdrant_request(method: str, path: str, json_data: Optional[dict] = None) -> dict:
    url = f"{QDRANT_HOST}/{path.lstrip('/')}"
    resp = requests.request(method, url, json=json_data, timeout=30)
    if not resp.ok:
        raise RuntimeError(f"Qdrant {method} {path} failed: {resp.status_code} {resp.text}")
    data = resp.json()
    if data.get("status") == "error":
        raise RuntimeError(f"Qdrant error: {data.get('status', {}).get('error', data)}")
    return data.get("result", data)


def get_collection_info(name: str) -> dict:
    return qdrant_request("GET", f"/collections/{name}")


def scroll_all_points(collection: str) -> list[dict]:
    """Scroll ALL points from a collection, including vectors and payload."""
    points = []
    offset = None
    total = 0
    while True:
        body = {
            "limit": SCROLL_LIMIT,
            "with_payload": True,
            "with_vector": True,
        }
        if offset:
            body["offset"] = offset

        result = qdrant_request("POST", f"/collections/{collection}/points/scroll", body)
        batch = result.get("points", [])
        points.extend(batch)
        total += len(batch)
        print(f"  Scrolled {len(batch)} points (total: {total})", file=sys.stderr)
        offset = result.get("next_page_offset")
        if offset is None:
            break
    return points


def deduplicate(points: list[dict]) -> dict:
    """Deduplicate by content hash (sha256 of text in payload).
    Returns: {hash: point} mapping."""
    seen = {}
    dupes = 0
    for pt in points:
        payload = pt.get("payload", {})
        # Build hash from text content (skip metadata fields)
        raw = payload.get("text") or payload.get("content") or ""
        if not isinstance(raw, str):
            raw = str(raw)
        text = raw if isinstance(raw, str) else str(raw)
        h = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if h in seen:
            dupes += 1
            continue
        seen[h] = pt
    if dupes:
        print(f"  Removed {dupes} duplicates", file=sys.stderr)
    return seen


def upsert_points(points: list[dict], dry_run: bool = False) -> int:
    """Batch upsert points into target collection."""
    total = len(points)
    upserted = 0
    for i in range(0, total, BATCH_SIZE):
        batch = points[i : i + BATCH_SIZE]
        qdrant_batch = []
        for pt in batch:
            qdrant_batch.append({
                "id": pt["id"],
                "vector": pt.get("vector", pt.get("vectors", {})),
                "payload": pt.get("payload", {}),
            })
        if dry_run:
            print(f"  Would upsert {len(qdrant_batch)} points (batch {i//BATCH_SIZE + 1})", file=sys.stderr)
            upserted += len(qdrant_batch)
            continue
        try:
            qdrant_request("PUT", f"/collections/{TARGET_COLLECTION}/points", {
                "points": qdrant_batch,
                "wait": False,
            })
            upserted += len(qdrant_batch)
            print(f"  Upserted {len(qdrant_batch)} points (batch {i//BATCH_SIZE + 1}, {upserted}/{total})", file=sys.stderr)
        except Exception as e:
            print(f"  ❌ Batch {i//BATCH_SIZE + 1} failed: {e}", file=sys.stderr)
    return upserted


def main():
    parser = argparse.ArgumentParser(description="Migrate Qdrant collections to 'nexus'")
    parser.add_argument("--dry-run", action="store_true", help="Dry run — no writes")
    args = parser.parse_args()

    print(f"{'🔍 DRY RUN' if args.dry_run else '🔐 MIGRATION'} — Target: {TARGET_COLLECTION}", file=sys.stderr)

    # Collect all points from all source collections
    all_points = {}
    for col in SOURCE_COLLECTIONS:
        info = get_collection_info(col)
        cnt = info.get("points_count", 0)
        print(f"\n📦 {col}: {cnt} points", file=sys.stderr)

        if cnt == 0:
            print("  Skipping — empty", file=sys.stderr)
            continue

        raw_points = scroll_all_points(col)

        # Tag with source collection
        for pt in raw_points:
            if "payload" in pt:
                pt["payload"]["_source_collection"] = col

        # Deduplicate
        deduped = deduplicate(raw_points)
        print(f"  → {len(deduped)} unique points after dedup", file=sys.stderr)

        # Merge into global dict
        before = len(all_points)
        all_points.update(deduped)
        print(f"  → {len(all_points) - before} new points added to global pool", file=sys.stderr)

    total_points = len(all_points)
    print(f"\n{'='*50}", file=sys.stderr)
    print(f"📊 Total unique points to migrate: {total_points}", file=sys.stderr)

    if total_points == 0:
        print("⚠️  Nothing to migrate.", file=sys.stderr)
        return

    # Show source breakdown
    sources = defaultdict(int)
    for pt in all_points.values():
        src = pt.get("payload", {}).get("_source_collection", "unknown")
        sources[src] += 1
    for src, cnt in sorted(sources.items()):
        print(f"  {src}: {cnt} points", file=sys.stderr)

    if args.dry_run:
        print(f"\n✅ Dry run complete. Would upsert {total_points} unique points.", file=sys.stderr)
        return

    # Upsert
    print(f"\n📤 Upserting {total_points} points into '{TARGET_COLLECTION}'...", file=sys.stderr)
    points_list = list(all_points.values())
    upserted = upsert_points(points_list)

    print(f"\n{'='*50}", file=sys.stderr)
    print(f"✅ Migration complete: {upserted}/{total_points} points upserted into '{TARGET_COLLECTION}'", file=sys.stderr)

    # Verify
    time.sleep(2)  # Let Qdrant process
    target_info = get_collection_info(TARGET_COLLECTION)
    final_cnt = target_info.get("points_count", 0)
    print(f"📊 Target collection '{TARGET_COLLECTION}' now has {final_cnt} points", file=sys.stderr)


if __name__ == "__main__":
    main()
