import os
import sqlite3, sys, re
from datetime import datetime
from zoneinfo import ZoneInfo


def build_query(session_id: str, role_filter: str | None = None) -> tuple[str, tuple]:
    """Build the messages query with the role filter as a bound parameter.

    Returns ``(sql, params)``.  The role value is never interpolated into
    the SQL string (SQL injection via argv).
    """
    q = ("SELECT id, role, content, tool_name, timestamp FROM messages "
         "WHERE session_id=? AND active=1")
    params: list = [session_id]
    if role_filter:
        q += " AND role=?"
        params.append(role_filter)
    q += " ORDER BY id"
    return q, tuple(params)


def main() -> None:
    if len(sys.argv) < 2:
        # Nr 273: bare invocation crashed with IndexError
        print("usage: session_dump.py <session_id> [role_filter] [limit] [maxlen]", file=sys.stderr)
        sys.exit(2)
    sid = sys.argv[1]
    role_filter = sys.argv[2] if len(sys.argv) > 2 else None
    try:
        limit = int(sys.argv[3]) if len(sys.argv) > 3 else 400
        maxlen = int(sys.argv[4]) if len(sys.argv) > 4 else 500
    except ValueError:
        # Nr 273: int() failures crashed with an ugly traceback
        print("limit and maxlen must be integers", file=sys.stderr)
        sys.exit(2)

    berlin = ZoneInfo("Europe/Berlin")  # Nr 447: DST-correct, not a fixed +2

    # Nr 446: no hard-coded home path — override via env, default to ~/.hermes.
    db_path = os.environ.get("NEXUS_STATE_DB") or os.path.expanduser("~/.hermes/state.db")
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    q, params = build_query(sid, role_filter)
    all_rows = con.execute(q, params).fetchall()
    # W32-12: ``rows[-limit:]`` is ``rows[0:]`` for limit == 0 (the whole
    # session) and slices from the wrong end for a negative limit. Clamp to a
    # non-negative int; 0 means "show nothing". The header keeps the TOTAL
    # count and reports how many rows are actually shown.
    limit = max(0, int(limit))
    rows = all_rows[-limit:] if limit > 0 else []
    print(f"=== {sid}: {len(all_rows)} msgs (showing last {len(rows)}, {maxlen} chars each)")
    for r in rows:
        ts = datetime.fromtimestamp(r['timestamp'], berlin).strftime('%H:%M')
        c = (r['content'] or '').strip()
        c = re.sub(r'\s+', ' ', c)
        tn = r['tool_name'] or ''
        if role_filter:
            line = c[:maxlen]
            print(f"[{ts}] {line}")
        else:
            if r['role'] == 'user':
                print(f"[{ts}] USER: {c[:maxlen]}")
            elif r['role'] == 'assistant':
                if c:
                    print(f"[{ts}] ASSI: {c[:maxlen]}")
            elif r['role'] == 'tool':
                if c and tn not in ('session_search',):
                    pass  # tool output noise, skip in overview mode
    con.close()


if __name__ == "__main__":
    main()
