#!/usr/bin/env python3
"""Quick demo: Belief Drift Detection in action.

Usage:
    python3 examples/drift_demo.py

Shows how drift detection finds stale entries.
"""

from nexus.health import DriftDetector

# Sample entries — some stale, some fresh
entries = [
    {
        "id": "1",
        "content": "DeepSeek V4 Pro running as primary fallback provider.",
        "timestamp": "2026-04-01T10:00:00Z",
    },
    {
        "id": "2",
        "content": "Nomic embed text is the default embedding provider at 384 dimensions.",
        "timestamp": "2026-03-15T10:00:00Z",
    },
    {
        "id": "3",
        "content": "GLM 5.1 Cloud is set as the default model via Ollama Cloud.",
        "timestamp": "2026-05-18T08:00:00Z",
    },
    {
        "id": "4",
        "content": "Hybrid retrieval with BM25 + Vector + RRF is now active.",
        "timestamp": "2026-05-18T12:00:00Z",
    },
]

detector = DriftDetector()
report = detector.run_from_texts(entries)

print(f"\n🔍 Drift Report: {report.summary}\n")
print(f"  Total entries: {report.total_entries}")
print(f"  Stale entries: {len(report.stale)}")
print(f"  Old entries:   {len(report.old)}")
print(f"  Score:         {report.score:.1f}/10\n")

if report.stale:
    print("  ⚠️ Stale entries found:")
    for s in report.stale:
        print(f"    • {s['id']}: {', '.join(s['issues'])}")

print(f"\n✅ Drift detection working.\n")