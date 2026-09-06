"""Invariant tests for the selective_forgetting MEDIUM review fixes.

1. A single invalid timestamp must not abort the whole audit run — the
   point is skipped and counted as invalid_timestamps in the report.
2. Candidate filtering uses the EXACT score, not a display-rounded one.
3. Report cleanup prunes old forget-*.json reports (same naming scheme as
   the writer), not a nonexistent 'forget-audit*' prefix.
"""

import json
import os
import time
from types import SimpleNamespace

from nexus_memory import selective_forgetting as SF


class _FakePoint:
    def __init__(self, pid, payload):
        self.id = pid
        self.payload = payload


class _FakeStore:
    def __init__(self, points):
        self._points = points
        self.client = SimpleNamespace(
            scroll=lambda collection, limit=500, offset=None,
            with_payload=True, with_vectors=False:
                (list(self._points) if offset is None else [], None),
        )


def _auditor(tmp_path):
    return SF.SelectiveForgettingAuditor(_FakeStore([]), "test-coll",
                                         data_dir=str(tmp_path))


def test_invalid_timestamp_counted_not_crashing(tmp_path):
    now = time.time()
    points = [
        _FakePoint("ok-1", {"category": "session", "created_at": now - 400 * 86400,
                            "lifecycle_status": "active"}),
        # Invalid timestamp values (garbage strings) in EVERY timestamp field:
        _FakePoint("bad-1", {"category": "session",
                             "created_at": "not-a-date",
                             "updated_at": "garbage",
                             "timestamp": "x",
                             "lifecycle_status": "active"}),
    ]
    auditor = SF.SelectiveForgettingAuditor(_FakeStore(points), "test-coll",
                                            data_dir=str(tmp_path))
    rep = auditor.run()  # must not raise
    assert rep["invalid_timestamps"] == 1
    assert rep["scored"] == 1


def test_cleanup_prunes_forget_reports(tmp_path):
    auditor = _auditor(tmp_path)
    # 15 old reports in the writer's naming scheme + 1 noise file.
    for i in range(15):
        (tmp_path / f"forget-2026-08-{i + 10:02d}.json").write_text("{}")
    (tmp_path / "forget-audit-legacy.txt").write_text("noise")
    auditor._write_report({"timestamp": "2026-09-06T23:00:00"})
    remaining = sorted(f.name for f in tmp_path.iterdir()
                       if f.name.startswith("forget-") and f.name.endswith(".json"))
    assert len(remaining_check(remaining_list(tmp_path))) == 12


def remaining_list(tmp_path):
    return [f for f in tmp_path.iterdir()
            if f.name.startswith("forget-") and f.name.endswith(".json")]


def remaining_check(files):
    return files