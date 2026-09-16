#!/usr/bin/env python3
"""backfill_shard.py — one-shard backfill worker (parallel-safe).

Usage: python3 backfill_shard.py --shard 0 --num-shards 4

Coordination: The dispatcher (backfill_parallel.py) pre-computes the open
backlog ONCE and splits it into N disjoint point-ID lists. Each worker
processes ONLY its own IDs (retrieve by ID → consolidate → mark). No scroll
race, no double work, resume-safe: processed IDs are skipped via
consolidated_by check before work.

Conflict-resolution caveat: only the SOURCE point IDs are disjoint across
shards. _resolve_conflicts/_store_fact/_supersede_old all operate on the
shared fact collection with no cross-shard lock, so two shards CAN
concurrently distill similar content and produce duplicate/superseding
facts. Run a dedup pass (SICA / health audit) after a sharded backfill.
"""
import argparse
import os
import sys
import time
from datetime import datetime, timezone

os.environ["NEXUS_CONSOLIDATION"] = "0"  # no daemon thread inside workers
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


def _iso_date(value) -> "str | None":
    """Coerce a payload ``created_at`` into a ``YYYY-MM-DD`` string or None.

    W32-5: the payload is free-form. A legacy point may carry an epoch int,
    in which case the old ``src_date[:10]`` raised ``TypeError`` OUTSIDE the
    per-point try/except and killed the whole shard run. Anything that is not
    a string and not a number is treated as "no date" (None).
    """
    if isinstance(value, str):
        return value[:10] or None
    # bool is an int subclass — a boolean timestamp is meaningless, skip it.
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            # timezone.utc variant: utcfromtimestamp() is deprecated in 3.12+.
            return datetime.fromtimestamp(value, timezone.utc).strftime("%Y-%m-%d")
        except (OverflowError, OSError, ValueError):
            return None
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--shard", type=int, required=True)
    ap.add_argument("--num-shards", type=int, default=4)
    ap.add_argument("--sleep", type=float, default=0.2)
    args = ap.parse_args()

    from qdrant_client import QdrantClient
    from qdrant_client.models import (Filter, FieldCondition, MatchValue,
                                      IsEmptyCondition, PayloadField)
    from nexus_memory.mcp_server import MemoryStore
    from nexus_memory import consolidation as cons

    coll = os.environ.get("NEXUS_COLLECTION", "nexus")
    store = MemoryStore()
    c = cons.Consolidator(store, coll)

    # Collect MY ids: hash(point_id) % num_shards == shard, open only
    client = store.client
    flt = Filter(must=[
        FieldCondition(key="lifecycle_status", match=MatchValue(value="canonical")),
        FieldCondition(key="category", match=MatchValue(value="session")),
        IsEmptyCondition(is_empty=PayloadField(key="consolidated_by")),
    ])
    # NOTE: hash() is salted per-process on Py3 — use a stable hash instead:
    import zlib
    my_points = []
    client = store.client
    offset = None
    while True:
        batch, offset = client.scroll(coll, scroll_filter=flt, limit=64,
                                      offset=offset, with_payload=True, with_vectors=False)
        for p in batch:
            pid = str(p.id)
            if zlib.crc32(pid.encode()) % args.num_shards == args.shard:
                # W32-5: tolerate non-string created_at (epoch int/float).
                src_date = _iso_date((p.payload or {}).get("created_at"))
                my_points.append((pid, (p.payload or {}).get("content", ""), src_date))
        if offset is None:
            break

    total = len(my_points)
    print(f"[shard {args.shard}/{args.num_shards}] my points: {total}", flush=True)

    facts = superseded = dups = failed = 0
    t0 = time.time()
    for i, (pid, content, src_date) in enumerate(my_points, 1):
        try:
            stats = c.consolidate_point(pid, content, source_date=src_date)
            dups += stats["duplicates"]
            superseded += stats["superseded"]
            facts += stats["created"]
            created_here = stats["created"]
        except Exception as exc:
            # Nr 283: pipeline errors (LLM/parse/store) still fail the whole
            # point — same as the daemon; supersede failures are handled inside.
            failed += 1
            print(f"[shard {args.shard}] WARN point {pid}: {exc}", flush=True)
        if i % 5 == 0 or i == total:
            dt = time.time() - t0
            rate = i / dt if dt > 0 else 0
            print(f"[shard {args.shard}] {i}/{total} (facts={facts}, sup={superseded}, "
                  f"dup={dups}, fail={failed}) | {rate*60:.1f}/min | eta {total/max(rate,0.01)/60:.0f}min",
                  flush=True)
        time.sleep(args.sleep)
    print(f"[shard {args.shard}] DONE: {total} points in {(time.time()-t0)/60:.1f} min", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())