#!/usr/bin/env python3
"""Quick demo: Belief Drift Detection in action.

Usage:
    python3 examples/drift_demo.py

Shows how drift detection finds stale entries.
"""

from datetime import datetime, timedelta, timezone

from nexus.health import DriftDetector

# W40-6: timestamps are derived from "now" instead of hard-coded absolute
# dates. The hard-coded 2026-03/05 values made EVERY entry older than the
# 90-day old_threshold (and expired under the default NORMAL policy), so the
# "fresh" entries below were reported as stale/old/expired too.
_now = datetime.now(timezone.utc)


def _ago(days: int) -> str:
    return (_now - timedelta(days=days)).isoformat()


# Sample entries — some stale, some fresh
entries = [
    {
        "id": "1",
        "content": "DeepSeek V4 Pro running as primary fallback provider.",
        "timestamp": _ago(200),
    },
    {
        "id": "2",
        "content": "Nomic embed text is the default embedding provider at 384 dimensions.",
        "timestamp": _ago(150),
    },
    {
        "id": "3",
        "content": "GLM 5.1 Cloud is set as the default model via Ollama Cloud.",
        "timestamp": _ago(5),
    },
    {
        "id": "4",
        "content": "Hybrid retrieval with BM25 + Vector + RRF is now active.",
        "timestamp": _ago(2),
    },
]

detector = DriftDetector()
report = detector.run_from_texts(entries)

print(f"\n🔍 Drift Report: {report.summary}\n")
print(f"  Total entries: {report.total_entries}")
print(f"  Stale entries: {len(report.stale)}")
print(f"  Old entries:   {len(report.old)}")
# W40-6: `expired` is part of the score (weight 0.5) — without it the printed
# score could not be reconciled with the counts shown above it.
print(f"  Expired:       {len(report.expired)}")
print(f"  Excluded:      {report.excluded_count}")
print(f"  Score:         {report.score:.1f}/10\n")

if report.stale:
    print("  ⚠️ Stale entries found:")
    for s in report.stale:
        print(f"    • {s['id']}: {', '.join(s['issues'])}")

# W40-6: a run that finds nothing must not print the success banner — a
# pattern regression would otherwise look like a healthy demo.
if report.stale or report.old or report.expired:
    print("\n✅ Drift detection working.\n")
else:
    print("\n⚠️ No stale/old/expired entries found — check the sample data.\n")
