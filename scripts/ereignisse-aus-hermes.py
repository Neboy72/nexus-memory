#!/usr/bin/env python3
"""Hermes-Vorfälle an Nexus melden — optionaler Zulieferer, kein Kernstück.

Das Träumen liest keine fremde Sitzungsdatenbank (das war bis 0.22.60 der
Fall und koppelte das Gedächtnis an Hermes). Dieses Skript ist die andere
Richtung: es liest Hermes, destilliert die gescheiterten Werkzeugaufrufe und
schickt sie als neutrale Vorfälle an Nexus — in die lokale Ablage oder per
HTTP an ``POST /events``. Ohne dieses Skript träumt Nexus über das, was ihm
andere Agenten melden; mit ihm kommen Hermes-Vorfälle dazu.

    python3 scripts/ereignisse-aus-hermes.py --days 30
    python3 scripts/ereignisse-aus-hermes.py --days 7 --url http://127.0.0.1:9122/events --key-file ~/.nexus-memory/agent_keys/hermes.key
    python3 scripts/ereignisse-aus-hermes.py --days 30 --dry-run

Gelesen wird nur: ``role='tool'``-Nachrichten mit ``exit_code != 0`` oder
einem gesetzten ``error``. Erfolgreiche Aufrufe werden nicht übertragen.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


def _tool_ergebnis(content):
    """(fehlgeschlagen, fehlertext, hint) — dieselbe Regel wie im Träumen."""
    if isinstance(content, (bytes, bytearray)):
        content = content.decode("utf-8", "replace")
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except Exception:
            return False, "", ""
    if isinstance(content, list):
        for eintrag in content:
            fehler, text, hint = _tool_ergebnis(json.dumps(eintrag))
            if fehler:
                return fehler, text, hint
        return False, "", ""
    if not isinstance(content, dict):
        return False, "", ""
    hint = str(content.get("hint") or "").strip()
    exit_code = content.get("exit_code")
    fehler = content.get("error")
    if isinstance(exit_code, int) and exit_code != 0:
        return True, str(content.get("output") or fehler or ""), hint
    if isinstance(fehler, str) and fehler.strip():
        return True, str(fehler), hint
    if str(content.get("status") or "").lower() == "error":
        return True, str(content.get("output") or ""), hint
    return False, "", hint


def sammeln(db: Path, tage: float):
    cutoff = time.time() - tage * 86400
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    sitzungen = conn.execute(
        "SELECT id FROM sessions WHERE started_at > ? AND tool_call_count >= 3",
        (cutoff,)).fetchall()
    vorfaelle = []
    for (sid,) in sitzungen:
        for tool, content, ts in conn.execute(
                "SELECT tool_name, content, timestamp FROM messages "
                "WHERE session_id = ? AND role = 'tool' ORDER BY timestamp",
                (sid,)):
            fehler, text, hint = _tool_ergebnis(content)
            if not fehler or not text:
                continue
            vorfaelle.append({"agent": "hermes", "tool": tool or "unbekannt",
                              "ok": False, "error": text, "hint": hint,
                              "session": sid, "ts": float(ts or 0)})
    conn.close()
    return vorfaelle


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=str(Path.home() / ".hermes" / "state.db"))
    ap.add_argument("--days", type=float, default=30)
    ap.add_argument("--url", default="", help="POST /events statt lokaler Ablage")
    ap.add_argument("--key-file", default=str(
        Path.home() / ".nexus-memory" / "agent_keys" / "hermes.key"))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    db = Path(args.db)
    if not db.exists():
        print(f"keine Datenbank: {db}", file=sys.stderr)
        return 1
    vorfaelle = sammeln(db, args.days)
    print(f"{len(vorfaelle)} gescheiterte Werkzeugaufrufe aus {args.days:g} Tagen")
    if args.dry_run:
        for v in vorfaelle[:5]:
            print("  ", v["tool"], "|", v["error"][:90].replace("\n", " "))
        return 0

    if args.url:
        key = Path(args.key_file).read_text(encoding="utf-8").strip()
        geschickt = 0
        for i in range(0, len(vorfaelle), 200):
            paket = vorfaelle[i:i + 200]
            daten = json.dumps(paket).encode("utf-8")
            req = urllib.request.Request(
                args.url, data=daten, method="POST",
                headers={"Content-Type": "application/json",
                         "x-nexus-service-key": key})
            try:
                with urllib.request.urlopen(req, timeout=20) as resp:
                    antwort = json.loads(resp.read().decode("utf-8"))
                geschickt += int(antwort.get("stored") or 0)
            except urllib.error.HTTPError as exc:
                print(f"HTTP {exc.code}: {exc.read()[:200]!r}", file=sys.stderr)
                return 1
            except Exception as exc:
                print(f"nicht erreichbar: {exc}", file=sys.stderr)
                return 1
        print(f"{geschickt} Vorfälle an {args.url} gemeldet")
        return 0

    from nexus_memory import events as journal
    geschrieben = journal.write_many(vorfaelle)
    print(f"{geschrieben} Vorfälle ins Journal ({journal.journal_path()})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
