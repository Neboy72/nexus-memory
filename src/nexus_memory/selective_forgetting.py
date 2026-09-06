#!/usr/bin/env python3
"""
selective_forgetting.py — score-based memory-aging audit for Nexus Memory (READ-ONLY).

Basiert auf arXiv 2608.28978 ("Selective Forgetting", validiert: Pruning von 9.8%
der Knoten ohne F1-Verlust über recency+access+degree+age).

Nexus-Adaptation (ehrlich dokumentiert):
- recency/age: aus Payload-Zeitstempeln (created_at > updated_at > modified > timestamp > created)
- access frequency: Nexus loggt keine Zugriffe pro Memory -> Komponente entfällt (weight 0)
- degree centrality: Graph-Degree wäre teuer; für v1 entfällt

STANDALONE-REGEL (Nebo, 02.09.2026): Dieses Modul läuft IN-PROCESS als Teil des
HealthAuditor-Daemon-Threads im MCP-Server — auf JEDEM Host, mit JEDEM Harness,
ohne externen Scheduler (kein Agent-Cron, kein LaunchAgent, kein host-cron).
Ein User, der nexus-memory installiert, bekommt dieses Audit automatisch mit.

DESIGN (identisch zu health_audit.py):
  - READ-ONLY. Never deletes or modifies memories. Recommendations only.
  - Failures are logged, never thrown into the MCP loop.
"""
import json
import logging
import os
import math
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

log = logging.getLogger("nexus.selective_forgetting")

# Score-Schwelle: ab hier gilt ein Punkt als Vergessen-Kandidat (Empfehlung!)
CANDIDATE_THRESHOLD = 0.60
# Paper-Referenz: 180 Tage Halbzeit der Sigmoid; 90 Tage Steigung
AGE_MIDPOINT_DAYS = 180
AGE_SCALE_DAYS = 90
# Kategorien, die schneller alternieren (Conversational, nicht kanonisch)
CATEGORY_WEIGHTS = {"session": 1.0, "temp": 1.0}
CATEGORY_DEFAULT_WEIGHT = 0.6
# Harte Schutzzonen — werden NIE als Kandidat gelistet
PROTECTED_LIFECYCLE = {"canonical", "ACTIVE"}
PROTECTED_SOURCE = {"paperless"}
PROTECTED_CATEGORY = {"rule", "procedure"}


def parse_ts(v: Any) -> Optional[float]:
    """Robustes Timestamp-Parsen: unix (s/ms), ISO-8601 (±Z), Datum-only."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        f = float(v)
        return f / 1000 if f > 1e11 else f
    s = str(v).strip()
    if not s or s.lower() == "none":
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        pass
    for fmt in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(s[:10], fmt).replace(tzinfo=timezone.utc).timestamp()
        except Exception:
            continue
    return None


def get_ts(payload: Dict[str, Any]) -> Optional[float]:
    """Erster gültiger Zeitstempel nach Feld-Priorität."""
    for k in ("created_at", "updated_at", "modified", "timestamp", "created"):
        t = parse_ts(payload.get(k))
        if t:
            return t
    return None


_TIMESTAMP_FIELDS = ("created_at", "updated_at", "modified", "timestamp", "created")


def has_timestamp_field(payload: Dict[str, Any]) -> bool:
    """True when the payload carries at least one timestamp field with a
    NON-empty value — used to distinguish 'no timestamp at all' from
    'timestamp present but unparseable' (data-quality issue)."""
    for k in _TIMESTAMP_FIELDS:
        v = payload.get(k)
        if v is not None and str(v).strip() and str(v).strip().lower() != "none":
            return True
    return False


class SelectiveForgettingAuditor:
    """Score-based aging audit. In-process, read-only, recommendations only."""

    def __init__(self, store, collection: str, data_dir: Optional[str] = None) -> None:
        self._store = store
        self._collection = collection
        self._data_dir = data_dir or str(os.path.join(os.path.expanduser("~"), ".nexus-memory", "reports"))
        os.makedirs(self._data_dir, exist_ok=True)

    def score_point(self, payload: Dict[str, Any], now: float) -> Optional[float]:
        """Score in [0..1] oder None wenn geschützt/nicht bewertbar."""
        lc = payload.get("lifecycle_status") or payload.get("status")
        if lc in ("superseded", "deleted"):
            return None
        if lc in PROTECTED_LIFECYCLE:
            return None
        if str(payload.get("source") or "") in PROTECTED_SOURCE:
            return None
        cat = str(payload.get("category") or "?")
        if cat in PROTECTED_CATEGORY:
            return None
        ts = get_ts(payload)
        if ts is None:
            return None
        age_days = (now - ts) / 86400.0
        age_s = 1.0 / (1.0 + math.exp(-(age_days - AGE_MIDPOINT_DAYS) / AGE_SCALE_DAYS))
        return min(1.0, age_s * CATEGORY_WEIGHTS.get(cat, CATEGORY_DEFAULT_WEIGHT))

    def run(self) -> Dict[str, Any]:
        points = []
        offset = None
        invalid_timestamps = 0
        while True:
            batch, offset = self._store.client.scroll(
                self._collection, limit=500, offset=offset,
                with_payload=True, with_vectors=False,
            )
            points.extend(batch)
            if offset is None:
                break

        now = time.time()
        scored = []
        protected: Dict[str, int] = {}
        for p in points:
            payload = p.payload or {}
            if payload.get("lifecycle_status") in ("superseded", "deleted"):
                continue
            score = self.score_point(payload, now)
            if score is None:
                # Grund klassifizieren (nur für den Report)
                lc = payload.get("lifecycle_status") or payload.get("status")
                if lc in PROTECTED_LIFECYCLE:
                    protected["protected_lifecycle"] = protected.get("protected_lifecycle", 0) + 1
                elif str(payload.get("source") or "") in PROTECTED_SOURCE:
                    protected["protected_source"] = protected.get("protected_source", 0) + 1
                elif str(payload.get("category") or "?") in PROTECTED_CATEGORY:
                    protected["protected_category"] = protected.get("protected_category", 0) + 1
                elif has_timestamp_field(payload):
                    # Timestamp fields exist but none parses — a data-quality
                    # issue, counted separately (a single bad timestamp must
                    # not abort the audit, it is skipped + reported).
                    invalid_timestamps += 1
                else:
                    protected["no_timestamp"] = protected.get("no_timestamp", 0) + 1
                continue
            cat = str(payload.get("category") or "?")
            ts = get_ts(payload)
            if ts is None:  # defensive: score_point guarantees ts != None
                invalid_timestamps += 1
                continue
            age_days = (now - ts) / 86400.0
            scored.append({
                "score": round(score, 3),
                # Filtering uses the EXACT score (kept separately from the
                # rounded display value) so a rounded score can never cross
                # the threshold that the exact score does not.
                "age_days": round(age_days),
                "category": cat,
                "id": str(p.id),
                "text": str(payload.get("text") or payload.get("content") or "")[:120],
            })

        scored.sort(key=lambda x: -x["score"])
        # Threshold on the EXACT score, not a display-rounded one.
        candidates = [s for s in scored if s["score"] >= CANDIDATE_THRESHOLD]

        report: Dict[str, Any] = {
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "collection": self._collection,
            "total_points": len(points),
            "scored": len(scored),
            "protected": protected,
            "invalid_timestamps": invalid_timestamps,
            "candidates_score_ge_060": candidates,
            "threshold": CANDIDATE_THRESHOLD,
            "paper_reference": "arXiv 2608.28978: 9.8% Pruning ohne F1-Verlust",
            "note": "READ-ONLY. In-process (HealthAuditor loop). Recommendations only.",
        }
        self._write_report(report)
        return report

    def _write_report(self, report: Dict[str, Any]) -> None:
        try:
            report["report_file"] = os.path.join(
                self._data_dir, f"forget-{report['timestamp'][:10]}.json"
            )
            with open(report["report_file"], "w") as f:
                json.dump(report, f, indent=2, ensure_ascii=False, default=str)
            # Cleanup uses the SAME naming scheme as the writer above
            # (forget-YYYY-MM-DD.json). The old pattern ('forget-audit*')
            # matched nothing, so old reports were never pruned.
            files = sorted(
                f for f in os.listdir(self._data_dir)
                if f.startswith("forget-") and f.endswith(".json")
            )
            for old in sorted(files)[:-12]:
                try:
                    os.unlink(os.path.join(self._data_dir, old))
                except OSError:
                    pass
        except Exception as exc:
            log.warning("Forget-report write failed: %s", exc)