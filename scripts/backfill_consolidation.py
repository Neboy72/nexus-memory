#!/usr/bin/env python3
"""backfill_consolidation.py — one-shot backfill of the consolidation backlog.

Uses the PRODUCTION Consolidator (same distill + conflict resolution path as
the daemon), just driven in a tight loop with a small pause between points,
so the whole historical backlog gets processed once. Then the daemon resumes
its normal 6h cadence for new sessions only.

Usage:
  python3 scripts/backfill_consolidation.py [--batch 25] [--max N] [--sleep 2.0]

Safety:
  - Same guarantees as the daemon: no deletes, per-point fail-safe, kill via
    Ctrl+C (processed points are marked, resume is automatic).
  - Uses the same fuel chain (Ollama first — free).
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from nexus_memory.consolidation import Consolidator


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", type=int, default=25)
    ap.add_argument("--max", type=int, default=0, help="0 = until empty")
    ap.add_argument("--sleep", type=float, default=2.0, help="pause between points (s)")
    args = ap.parse_args()

    # Daemon-Thread im Script aus: das Backfill IST die Konsolidierung.
    # (Ohne Fix laufen Daemon + Backfill parallel um dieselben Punkte und
    # Ollama-Cloud drosselt die Verbindungen — py-spy-Beweis 06.09.)
    os.environ["NEXUS_CONSOLIDATION"] = "0"
    from nexus_memory.mcp_server import MemoryStore
    store = MemoryStore()
    coll = os.environ.get("NEXUS_COLLECTION", "nexus")
    c = Consolidator(store, coll)
    total_done = 0
    t0 = time.time()
    print(f"[backfill] start — batch={args.batch} sleep={args.sleep}s")
    while True:
        remaining = args.max - total_done if args.max else 0
        bs = min(args.batch, remaining) if remaining else args.batch
        report = c.run(batch_size=bs)
        done = report.get("scanned", 0)
        total_done += done
        print(f"[{time.strftime('%H:%M:%S')}] +{done} scanned (facts={report.get('facts_created',0)}, "
              f"superseded={report.get('superseded',0)}, dup={report.get('duplicates',0)}, "
              f"failed={report.get('failed',0)}) | total={total_done} | "
              f"{(time.time()-t0)/60:.1f}min")
        if done == 0:
            print("[backfill] backlog empty — done.")
            break
        if args.max and total_done >= args.max:
            print(f"[backfill] reached max={args.max}")
            break
        time.sleep(args.sleep)
    print(f"[backfill] total processed: {total_done} in {(time.time()-t0)/60:.1f} min")
    return 0


if __name__ == "__main__":
    sys.exit(main())
