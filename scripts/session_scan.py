import sqlite3
from datetime import datetime, timezone, timedelta

berlin = timezone(timedelta(hours=2))
now = datetime.now(berlin)
cutoff = (now - timedelta(hours=24)).timestamp()
con = sqlite3.connect('/Users/miosha/.hermes/state.db')
con.row_factory = sqlite3.Row
rows = con.execute(
    "SELECT id, source, started_at, ended_at, end_reason, message_count, tool_call_count,"
    " input_tokens, output_tokens, title, display_name, session_key, last_activity_description"
    " FROM sessions WHERE started_at >= ? AND archived=0 AND hidden=0 ORDER BY started_at DESC",
    (cutoff,),
).fetchall()
print(f"Sessions letzte 24h: {len(rows)}")
for r in rows:
    st = datetime.fromtimestamp(r['started_at'], berlin).strftime('%d.%m %H:%M')
    en = datetime.fromtimestamp(r['ended_at'], berlin).strftime('%H:%M') if r['ended_at'] else 'LAEUFT'
    print(f"  {r['id']} | {st}-{en} | src={r['source']} | msgs={r['message_count']} tools={r['tool_call_count']} | out={r['output_tokens']} | end={r['end_reason']}")
    t = (r['title'] or '')[:110]
    lad = (r['last_activity_description'] or '')[:110]
    print(f"      title: {t}")
    print(f"      last:  {lad}")
con.close()