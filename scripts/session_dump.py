import sqlite3, sys, re
from datetime import datetime, timezone, timedelta

berlin = timezone(timedelta(hours=2))
sid = sys.argv[1]
role_filter = sys.argv[2] if len(sys.argv) > 2 else None
limit = int(sys.argv[3]) if len(sys.argv) > 3 else 400
maxlen = int(sys.argv[4]) if len(sys.argv) > 4 else 500

con = sqlite3.connect('/Users/miosha/.hermes/state.db')
con.row_factory = sqlite3.Row
q = ("SELECT id, role, content, tool_name, timestamp FROM messages WHERE session_id=? AND active=1")
if role_filter:
    q += f" AND role='{role_filter}'"
q += " ORDER BY id"
rows = con.execute(q, (sid,)).fetchall()
print(f"=== {sid}: {len(rows)} msgs (showing last {limit}, {maxlen} chars each)")
for r in rows[-limit:]:
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