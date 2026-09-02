#!/usr/bin/env python3
"""
trust_service.py — in-process Belief-Trust-Recompute service for Nexus Memory.

Portiert aus scripts/trust_recompute.py (Kiosha, 2026) als in-process daemon
(HealthAuditor-Pattern): lebt im MCP-Server, kein externer Scheduler nötig.

Nebo-Grundentscheidung (02.09.2026): 'Nexus Memory ist unabhängig — es bringt
alles mit was es braucht.' Wartung = daemon-threads im Server, nie externe Scheduler.

WAS DIESER DIENST TUT (und nur er darf schreiben):
  - Aggregiert trust = max(trust_level) über alle Events eines Beliefs
  - Governance: retraction > user-override > user-confirm > agent-contest
    → setzt status (ACTIVE/CONTESTED/RETRACTED)
  - Ändert NUR payload-Felder trust/status/updated_at von Belief-Punkten

Env:
  NEXUS_TRUST_SWEEP=0        → Daemon deaktiviert (Kill-Switch)
  NEXUS_TRUST_INTERVAL_SEC   → Intervall (default 86400 = 24h)
"""
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger("nexus.trust")

STATUS_ACTIVE = "ACTIVE"
STATUS_CONTESTED = "CONTESTED"
STATUS_RETRACTED = "RETRACTED"

TRUST_START_DELAY_SECONDS = int(os.environ.get("NEXUS_TRUST_START_DELAY", 90))
TRUST_INTERVAL_SECONDS = int(os.environ.get("NEXUS_TRUST_INTERVAL_SEC", 24 * 3600))


def compute_trust(events: list[dict], belief: dict) -> tuple[float, str, str]:
    """Compute new trust value and status for a belief based on its events.

    1:1 Port aus scripts/trust_recompute.py (Stand 02.09.2026) — Semantik
    bewusst unverändert, damit Verhalten identisch zum etablierten Cron bleibt.

    Returns: (new_trust, new_status, reason)
    """
    payload = belief.get("payload", {})
    current_status = payload.get("status", STATUS_ACTIVE)

    trust_values = []
    for evt in events:
        ep = evt.get("payload", {})
        tl = ep.get("trust_level", 0.0)
        if tl is not None:
            try:
                trust_values.append(float(tl))
            except (TypeError, ValueError):
                continue

    new_trust = max(trust_values) if trust_values else payload.get("trust", 0.3)

    has_agent_contest = any(
        e.get("payload", {}).get("assertion") == "contest"
        and e.get("payload", {}).get("actor", {}).get("type") == "agent"
        for e in events
    )
    has_user_confirm = any(
        e.get("payload", {}).get("assertion") == "confirm"
        and e.get("payload", {}).get("actor", {}).get("type") == "user"
        for e in events
    )
    has_user_override = any(
        e.get("payload", {}).get("assertion") == "override"
        and e.get("payload", {}).get("actor", {}).get("type") == "user"
        for e in events
    )
    has_retraction = any(
        e.get("payload", {}).get("assertion") == "retract" for e in events
    )

    if has_retraction:
        new_status = STATUS_RETRACTED
        reason = "Retracted by event"
    elif has_user_override:
        new_status = STATUS_ACTIVE
        reason = "User override confirmed"
    elif has_user_confirm:
        new_status = STATUS_ACTIVE
        reason = "User confirmed belief"
    elif has_agent_contest and not has_user_confirm:
        new_status = STATUS_CONTESTED
        reason = "Contested by agent, awaiting user confirmation"
    elif current_status == STATUS_CONTESTED and not has_user_confirm:
        new_status = STATUS_CONTESTED
        reason = "Still contested, no user confirmation yet"
    else:
        new_status = STATUS_ACTIVE
        reason = "Trust recomputed, no contestation"

    return new_trust, new_status, reason


class TrustService:
    """Periodic in-process trust recompute for all beliefs.

    Liest Belief-Punkte + Events aus der Collection, rechnet trust/status neu
    und schreibt Änderungen NUR bei tatsächlicher Abweichung zurück.
    """

    def __init__(self, store, collection: str, data_dir: Optional[str] = None) -> None:
        self._store = store
        self._collection = collection
        self._data_dir = Path(data_dir or (Path.home() / ".nexus-memory" / "reports"))
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._last_report: Dict[str, Any] = {}
        self._lock = threading.Lock()

    # ── flags für health-Tool ─────────────────────────────────────────
    def get_flags(self) -> Dict[str, Any]:
        with self._lock:
            r = dict(self._last_report)
        if not r:
            return {}
        if r.get("contested_open"):
            return {
                "trust": {
                    "contested_open": r["contested_open"],
                    "message": (
                        f"⚖️ Nexus Memory trust service: {r['contested_open']} belief(s) "
                        f"CONTESTED awaiting user confirmation. Last recompute {r['timestamp']}."
                    ),
                }
            }
        return {}

    # ── one pass (used by loop AND tests) ─────────────────────────────
    def run(self, dry_run: bool = False) -> Dict[str, Any]:
        now_iso = time.strftime("%Y-%m-%d %H:%M:%S")
        beliefs = self._scroll_all()
        updated = 0
        contested_open = 0

        for p in beliefs:
            payload = p.payload or {}
            if payload.get("category") != "belief":
                continue
            if payload.get("lifecycle_status") in ("superseded", "deleted"):
                continue

            belief_id = payload.get("belief_id") or payload.get("fact_id") or str(p.id)
            events = self._events_for(belief_id)
            new_trust, new_status, reason = compute_trust(
                [{"payload": e} for e in events],
                {"payload": payload},
            )
            old_trust = payload.get("trust", 0.3)
            old_status = payload.get("status", STATUS_ACTIVE)

            if new_status == STATUS_CONTESTED:
                contested_open += 1

            if not dry_run and (new_trust != old_trust or new_status != old_status):
                self._store.client.set_payload(
                    collection_name=self._store.collection_name,
                    payload={"trust": new_trust, "status": new_status},
                    points=[p.id],
                )
                updated += 1
                log.info("Trust update %s: %.2f→%.2f, %s → %s (%s)",
                         str(p.id)[:8], float(old_trust or 0), new_trust, new_status, reason)

        report = {
            "timestamp": now_iso,
            "beliefs_scanned": len(beliefs),
            "updated": updated,
            "contested_open": contested_open,
            "dry_run": dry_run,
        }
        with self._lock:
            self._last_report = report
        self._write_report(report)
        return report

    # ── internals ─────────────────────────────────────────────────────
    def _scroll_all(self, limit: int = 500):
        points, offset = [], None
        while True:
            batch, offset = self._store.client.scroll(
                self._store.collection_name, limit=limit, offset=offset,
                with_payload=True, with_vectors=False,
            )
            points.extend(batch)
            if offset is None:
                break
        return points

    def _events_for(self, belief_id: str) -> list[dict]:
        """Alle Event-Punkte für einen Belief (payload.event_type == 'belief_event')."""
        events, offset = [], None
        while True:
            batch, offset = self._store.client.scroll(
                self._store.collection_name, limit=500, offset=offset,
                with_payload=True, with_vectors=False,
            )
            events.extend(batch)
            if offset is None:
                break
        out = []
        for p in events:
            pl = p.payload or {}
            if pl.get("event_type") == "belief_event" and pl.get("belief_id") == belief_id:
                out.append(pl)
        return out

    def _write_report(self, report: Dict[str, Any]) -> None:
        try:
            path = self._data_dir / f"trust-{report['timestamp'][:10].replace(':', '')}.json"
            with open(path, "w") as f:
                json.dump(report, f, indent=2, ensure_ascii=False, default=str)
            files = sorted(self._data_dir.glob("trust-*.json"))
            for old in files[:-12]:
                try:
                    old.unlink()
                except OSError:
                    pass
        except Exception as exc:
            log.warning("Trust report write failed: %s", exc)

    # ── daemon loop ───────────────────────────────────────────────────
    def start(self) -> None:
        def _loop():
            time.sleep(TRUST_START_DELAY_SECONDS)
            while True:
                try:
                    self.run()
                except Exception as exc:
                    log.warning("Trust recompute pass failed: %s", exc)
                for _ in range(max(60, TRUST_INTERVAL_SECONDS // 60)):
                    time.sleep(60)

        t = threading.Thread(target=_loop, name="nexus-trust", daemon=True)
        t.start()
        log.info("Trust service daemon started (interval %.1f h)",
                 TRUST_INTERVAL_SECONDS / 3600)