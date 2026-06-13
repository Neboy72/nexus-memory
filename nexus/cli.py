#!/usr/bin/env python3
"""Nexus Memory — Unified CLI (v2.7)

Verwendung:
  nexus resolve <fact>                    Belief suchen/erstellen
  nexus events <belief-id>                Event-History anzeigen
  nexus events --since <iso>              Events seit Zeitpunkt
  nexus ingest <file>                     Batch-Beliefs aus JSON
  nexus scan                              Full-Scan + Trust-Recompute
  nexus override <belief-id> <field> <val> User-Override setzen
  nexus verify                            Collection-Status prüfen
"""

import argparse
import json
import sys
import os

from nexus.config import is_success


def main():
    parser = argparse.ArgumentParser(
        description="Nexus Memory CLI v2.7",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # resolve
    p_resolve = sub.add_parser("resolve", help="Belief suchen/erstellen")
    p_resolve.add_argument("fact", type=str, help="Fakt-String")
    p_resolve.add_argument("--source", default="cli", help="Quelle")
    p_resolve.add_argument("--rationale", default="", help="Grund")
    p_resolve.add_argument("--trust", type=float, default=0.5, help="Trust 0-1")

    # events
    p_events = sub.add_parser("events", help="Event-History")
    p_events.add_argument("belief_id", nargs="?", type=str, default=None, help="Belief-ID (optional)")
    p_events.add_argument("--since", type=str, default=None, help="Events seit ISO-Datum")

    # ingest
    p_ingest = sub.add_parser("ingest", help="Batch-Beliefs aus JSON")
    p_ingest.add_argument("file", type=str, help="JSON-Datei mit Beliefs-Array")

    # scan
    p_scan = sub.add_parser("scan", help="Full-Scan + Trust-Recompute")
    p_scan.add_argument("--recompute", action="store_true", help="Trust neu berechnen")

    # override
    p_override = sub.add_parser("override", help="User-Override setzen")
    p_override.add_argument("belief_id", type=str)
    p_override.add_argument("field", type=str)
    p_override.add_argument("value", type=str)

    # verify
    p_verify = sub.add_parser("verify", help="Collection-Status")

    args = parser.parse_args()

    # Dynamisch importieren (Repo-Pfad automatisch finden)
    _ensure_path()

    if args.command == "resolve":
        cmd_resolve(args)
    elif args.command == "events":
        cmd_events(args)
    elif args.command == "ingest":
        cmd_ingest(args)
    elif args.command == "scan":
        cmd_scan(args)
    elif args.command == "override":
        cmd_override(args)
    elif args.command == "verify":
        cmd_verify()


def cmd_resolve(args):
    from nexus.apply import resolve_belief
    result = resolve_belief(
        fact=args.fact,
        source=args.source,
        rationale=args.rationale,
        trust=args.trust,
    )
    if result.get("error"):
        print(f"❌ {result.get('message', 'Fehler')}")
        sys.exit(1)

    status = "✅ Neu erstellt" if result["created"] else "🔁 Bereits vorhanden"
    print(f"{status}")
    print(f"  Belief-ID: {result['belief_id']}")
    print(f"  Status:    {result['status']}")
    print(f"  Trust:     {result['trust']:.2f}")


def cmd_events(args):
    from nexus.events import get_events, get_events_since

    if args.since:
        events = get_events_since(args.since)
        print(f"📋 {len(events)} Events seit {args.since}:")
    elif args.belief_id:
        events = get_events(args.belief_id)
        print(f"📋 {len(events)} Events für Belief {args.belief_id[:12]}...")
    else:
        print("❌ Please specify belief_id or --since")
        sys.exit(1)

    for e in events:
        delta = e.get("delta", {})
        print(f"  {e['event_type']:20s} | {e.get('status','?'):12s} | {str(delta)[:60]}")
        print(f"  {'':20s}   Zeit: {e.get('event_time','')[:19]}")


def cmd_ingest(args):
    from nexus.apply import resolve_belief

    if not os.path.exists(args.file):
        print(f"❌ File not found: {args.file}")
        sys.exit(1)

    with open(args.file) as f:
        beliefs = json.load(f)

    if not isinstance(beliefs, list):
        beliefs = [beliefs]

    created = 0
    exists = 0
    errors = 0

    for b in beliefs:
        try:
            r = resolve_belief(
                fact=b.get("fact", ""),
                source=b.get("source", "ingest"),
                rationale=b.get("rationale", ""),
                trust=b.get("trust", 0.5),
            )
            if r.get("error"):
                errors += 1
            elif r.get("created"):
                created += 1
            else:
                exists += 1
        except Exception as e:
            errors += 1
            print(f"  ❌ Fehler: {e}")

    print(f"✅ Ingest abgeschlossen: {created} neu, {exists} vorhanden, {errors} Fehler")


def cmd_scan(args):
    from nexus.apply import recompute_all

    print("🔍 Full-Scan gestartet...")
    stats = recompute_all()
    print(f"\n📊 Ergebnis:")
    print(f"  Gesamt:  {stats['total']}")
    print(f"  Geändert: {stats['changed']}")
    print(f"  Skipped: {stats['skipped']} (including {stats['overrides']} overrides)")
    if stats['errors']:
        print(f"  ⚠️ Fehler: {stats['errors']}")


def cmd_override(args):
    from nexus.apply import user_override

    value = args.value
    # Try numeric conversion
    try:
        if "." in value:
            value = float(value)
        else:
            value = int(value)
    except ValueError:
        pass  # Keep as string

    result = user_override(args.belief_id, args.field, value)
    if result.get("error"):
        print(f"❌ {result.get('message', 'Fehler')}")
        sys.exit(1)

    print(f"🔒 Override gesetzt:")
    print(f"  Feld: {result['field']}")
    print(f"  Alt:  {result['old']}")
    print(f"  Neu:  {result['new']}")


def cmd_verify():
    from nexus.events import verify_collection as verify_events
    from nexus.apply import ensure_beliefs_collection

    # nexus_events
    ev = verify_events()
    print(f"📦 nexus_events ({'✅' if ev['exists'] else '❌'}):")
    print(f"  Points:  {ev['points']}")
    print(f"  Indizes: {ev['indexes']}")

    # nexus_beliefs
    ok = ensure_beliefs_collection()
    import requests
    r = requests.get("http://localhost:6333/collections/nexus_beliefs", timeout=5)
    if is_success(r.status_code):
        d = r.json()["result"]
        print(f"\n📦 nexus_beliefs (✅):")
        print(f"  Points:  {d['points_count']}")
        print(f"  Vector:  {d['config']['params']['vectors']['size']}d")
    else:
        print(f"\n📦 nexus_beliefs (❌): not found")


def _ensure_path():
    """Ensures nexus modules are importable."""
    repo_paths = [
        os.path.expanduser("~/hermes-nexus-memory"),
        os.path.expanduser("~/.hermes/hermes-nexus-memory"),
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    ]
    for p in repo_paths:
        if os.path.isdir(os.path.join(p, "nexus")):
            if p not in sys.path:
                sys.path.insert(0, p)
            return
    print("❌ Nexus Memory repo not found")
    sys.exit(1)


if __name__ == "__main__":
    main()
