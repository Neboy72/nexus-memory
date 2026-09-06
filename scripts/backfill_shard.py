#!/usr/bin/env python3
"""backfill_shard.py — one-shard backfill worker (parallel-safe).

Usage: python3 backfill_shard.py --shard 0 --num-shards 4

Coordination: The dispatcher (backfill_parallel.py) pre-computes the open
backlog ONCE and splits it into N disjoint point-ID lists. Each worker
processes ONLY its own IDs (retrieve by ID → consolidate → mark). No scroll
race, no double work, resume-safe: processed IDs are skipped via
consolidated_by check before work.
"""
import argparse
import os
import sys
import time

os.environ["NEXUS_CONSOLIDATION"] = "0"  # no daemon thread inside workers
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


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

    store = MemoryStore()
    c = cons.Consolidator(store, os.environ.get("NEXUS_COLLECTION", "nexus"))

    # Collect MY ids: hash(point_id) % num_shards == shard, open only
    client = store.client
    flt = Filter(must=[
        FieldCondition(key="lifecycle_status", match=MatchValue(value="canonical")),
        FieldCondition(key="category", match=MatchValue(value="session")),
        IsEmptyCondition(is_empty=PayloadField(key="consolidated_by")),
    ])
    my_points, offset = [], None
    while True:
        batch, offset = client.scroll("nexus", scroll_filter=flt, limit=64,
                                      offset=offset, with_payload=False, with_vectors=False)
        for p in batch:
            if hash(str(p.id)) % args.num_shards == args.shard:
                my_points.append(str(p.id))
        if offset is None:
            break

    # NOTE: hash() is salted per-process on Py3 — use a stable hash instead:
    import zlib
    my_points = []
    client = store.client
    offset = None
    while True:
        batch, offset = client.scroll("nexus", scroll_filter=flt, limit=64,
                                      offset=offset, with_payload=True, with_vectors=False)
        for p in batch:
            pid = str(p.id)
            if zlib.crc32(pid.encode()) % args.num_shards == args.shard:
                my_points.append((pid, (p.payload or {}).get("content", "")))
        if offset is None:
            break

    total = len(my_points)
    print(f"[shard {args.shard}/{args.num_shards}] my points: {total}", flush=True)

    facts = superseded = dups = failed = 0
    t0 = time.time()
    date = time.strftime("%Y-%m-%d")
    for i, (pid, content) in enumerate(my_points, 1):
        try:
            conv = content[:cons._MAX_CONV_CHARS]
            fl = cons._parse_facts(c._llm(cons.DISTILL_PROMPT.format(date=date, conv=conv)))
            created_here = 0
            for fact in fl:
                decision, sup_ids = c._resolve_conflicts(fact)
                if decision == "duplicate":
                    dups += 1
                    continue
                new_id = c._store_fact(fact, pid)
                for old_id in sup_ids:
                    c._supersede_old(old_id, new_id)
                superseded += len(sup_ids)
                created_here += 1
                facts += 1
            c._mark_consolidated(pid, created_here)
        except Exception as exc:
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