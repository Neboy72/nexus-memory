import contextlib
import os
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

# W32-11(a): Europe/Berlin via zoneinfo. A fixed UTC+2 offset was an hour off
# all winter (CET), silently shifting the 24h cutoff window and the displayed
# times.
berlin = ZoneInfo("Europe/Berlin")
now = datetime.now(berlin)
cutoff = (now - timedelta(hours=24)).timestamp()
# W32-11(b): no hard-coded /Users/... path in the repo (PII + not portable).
# Override via NEXUS_SESSION_DB, default to ~/.hermes/state.db.
db_path = os.environ.get("NEXUS_SESSION_DB", str(Path.home() / ".hermes" / "state.db"))
# W40-scan (medium): sqlite3.connect() silently CREATES an empty DB file for a
# wrong path and the query then fails with "no such table". A report tool must
# not leave stray files behind; fail with a clear message instead.
if not os.path.exists(db_path):
    print(f"Session-DB nicht gefunden: {db_path}", file=sys.stderr)
    sys.exit(1)
# W40-13: closing the connection only on the success path leaked the handle
# whenever the query or a print() raised. `with sqlite3.connect(...)` would
# only scope commit/rollback, so contextlib.closing is the right wrapper.
with contextlib.closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)) as con:
    # OCR-5 (maintainability low): the report is read-only — the old
    # read-write connect could take a write lock and mutate the state DB by
    # accident. mode=ro also fails fast when the path does not exist.
    con.row_factory = sqlite3.Row
    # W40-scan (medium): a still-RUNNING session started >24h ago has
    # ended_at IS NULL and fell out of the started_at-only window — the
    # long-runner vanished from the report. Keep it explicitly.
    # OCR-5 (bug high, /tmp/z352-proof.py): the previous COALESCE predicate
    # collapsed to `started_at >= ?` for a running row (ended_at NULL →
    # COALESCE = started_at), so the long-runner was STILL filtered out and
    # the first OR-branch was redundant. Honest window: recently started OR
    # still running OR recently finished. (Old-but-finished rows that ended
    # before the cutoff stay out — activity window is the ended_at bound.)
    rows = con.execute(
        "SELECT id, source, started_at, ended_at, end_reason, message_count, tool_call_count,"
        " output_tokens, title, last_activity_description"
        " FROM sessions WHERE (started_at >= ? OR ended_at IS NULL OR ended_at >= ?)"
        " AND archived=0 AND hidden=0 ORDER BY started_at DESC",
        (cutoff, cutoff),
    ).fetchall()
    print(f"Sessions letzte 24h: {len(rows)}")
    for r in rows:
        st = datetime.fromtimestamp(r['started_at'], berlin).strftime('%d.%m %H:%M')
        # W40-scan (low): truthiness dropped a legitimate 0 epoch (and NULL);
        # `IS NOT NULL` is the honest test.
        en = datetime.fromtimestamp(r['ended_at'], berlin).strftime('%H:%M') if r['ended_at'] is not None else 'LAEUFT'
        print(f"  {r['id']} | {st}-{en} | src={r['source']} | msgs={r['message_count']} tools={r['tool_call_count']} | out={r['output_tokens']} | end={r['end_reason']}")
        t = (r['title'] or '')[:110]
        lad = (r['last_activity_description'] or '')[:110]
        print(f"      title: {t}")
        print(f"      last:  {lad}")