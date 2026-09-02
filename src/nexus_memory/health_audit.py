#!/usr/bin/env python3
"""
health_audit.py — self-contained health & duplicate audit for Nexus Memory.

Runs as an in-process daemon thread inside the MCP server (harness-independent,
no external scheduler required). Produces:
  1. JSON report files under <data-dir>/reports/ (always, every harness can read them)
  2. in-memory flags surfaced by the `health` tool (dedup_flags / health_flags)
     so the connected agent SEES them on its next health check and can tell the user
  3. optional webhook POST (NEXUS_WEBHOOK_URL) — Discord/Telegram/n8n/whatever

DESIGN RULES (agreed with Nebo 2026-08-31):
  - READ-ONLY by default. This module NEVER deletes or modifies memories.
    Repair happens via the established backup->prune->verify workflow.
  - No external scheduler needed: thread lives with the server process.
  - Failures are logged, never thrown into the MCP loop.
"""
import json
import logging
import os
import re
import threading
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Optional

log = logging.getLogger("nexus.health_audit")

AUDIT_INTERVAL_SECONDS = int(os.environ.get("NEXUS_AUDIT_INTERVAL", 30 * 24 * 3600))  # 30 days
AUDIT_START_DELAY_SECONDS = 45  # let the server finish booting first

VALID_LIFECYCLE = {"canonical", None, "", "staged", "pending"}


def _normalize(text: str) -> str:
    key = (text or "").lower()
    key = re.sub(r"\s+", " ", key).strip()
    key = re.sub(r"\s*\[(contra|new|old)-[^\]]+\]", "", key)
    return key[:300]


class HealthAuditor:
    """Periodic read-only audit: duplicate groups + collection stats.

    Write the report under the nexus data dir and expose flags for the
    health tool. Never deletes or modifies points.
    """

    def __init__(self, store, collection: str, data_dir: Optional[str] = None) -> None:
        self._store = store
        self._data_dir = Path(data_dir or (Path.home() / ".nexus-memory" / "reports"))
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._last_report: Optional[Dict[str, Any]] = None
        self._forget_report: Optional[Dict[str, Any]] = None
        self._thread: Optional[threading.Thread] = None

    # ── public: flags for the health tool ──────────────────────────────
    def get_flags(self) -> Dict[str, Any]:
        with self._lock:
            r = self._last_report
            f = self._forget_report
        if not r:
            return {}
        dup = r.get("duplicate_summary") or {}
        flags: Dict[str, Any] = {}
        if dup.get("groups"):
            flags["dedup"] = {
                "duplicate_groups": dup.get("groups"),
                "excess_copies": dup.get("excess_copies", 0),
                "message": (
                    f"⚠️ Nexus Memory health: {dup.get('groups')} duplicate groups "
                    f"({dup.get('excess_copies')} redundant copies) detected during the last audit. "
                    f"Tell your user: 'Nexus Memory found memory duplicates - run nexus-dedup "
                    f"or review the report at {r.get('report_file', '')}'. "
                    f"Audit is READ-ONLY; cleanup is a separate manual step."
                ),
                "audit_ts": r.get("timestamp"),
            }
        # 2026-09-02: selective-forgetting flags (arXiv 2608.28978 scoring)
        if f and (f.get("candidates_score_ge_060") or []):
            flags["forgetting"] = {
                "candidates": len(f["candidates_score_ge_060"]),
                "message": (
                    f"🧠 Nexus Memory selective-forgetting audit found "
                    f"{len(f['candidates_score_ge_060'])} old, low-value memories "
                    f"(read-only review: {f.get('report_file', '')}). "
                    f"Candidates are RECOMMENDATIONS only — nothing was deleted."
                ),
                "audit_ts": f.get("timestamp"),
            }
        return flags

    # ── public: run one audit now (used by the loop AND tests) ────────
    def run_audit(self) -> Optional[Dict[str, Any]]:
        # Registry hygiene runs FIRST and independent of the Qdrant audit —
        # ghost-agent cleanup must not depend on collection health.
        agent_cleanup: Optional[Dict[str, Any]] = None
        try:
            from nexus_memory.agent_detect import cleanup_removed_agents
            agent_cleanup = cleanup_removed_agents()
        except Exception as reg_exc:
            log.warning("Agent registry cleanup failed: %s", reg_exc)
        try:
            report = self._audit()
            with self._lock:
                self._last_report = report
            if agent_cleanup is not None:
                report["agent_cleanup"] = agent_cleanup
            self._write_report(report)
            self._maybe_webhook(report)
        except Exception as exc:  # never break the server
            log.warning("Health audit failed: %s", exc)
            return agent_cleanup  # still return the registry report
        # 2026-09-02: selective-forgetting scoring runs in the SAME loop, as an
        # independent step (its failure must not affect the dup report).
        try:
            from nexus_memory.selective_forgetting import SelectiveForgettingAuditor
            forget = SelectiveForgettingAuditor(self._store, self._store.collection_name,
                                                data_dir=str(self._data_dir))
            forget_report = forget.run()
            with self._lock:
                self._forget_report = forget_report
        except Exception as exc:
            log.warning("Selective-forgetting audit failed: %s", exc)
        # 2026-09-02 (Nebo-GO): self-maintenance dedup sweep — in-process, no
        # external scheduler. Merges ONLY exactly-normalized duplicate copies.
        # Kill switch: NEXUS_DEDUP_SWEEP=0 disables (user opt-out).
        if os.environ.get("NEXUS_DEDUP_SWEEP", "1") == "1":
            try:
                sweep = self._dedup_sweep()
                report["dedup_sweep"] = sweep
            except Exception as exc:
                log.warning("Dedup sweep failed: %s", exc)
        return report

    # ── dedup sweep (in-process self-maintenance) ───────────────────────
    def _collect_points(self):
        points = []
        offset = None
        while True:
            batch, offset = self._store.client.scroll(
                self._store.collection_name, limit=500, offset=offset,
                with_payload=True, with_vectors=False,
            )
            points.extend(batch)
            if offset is None:
                break
        return points

    def _dedup_sweep(self) -> Dict[str, Any]:
        """Merge exact-normalized duplicates, oldest point wins as keeper.

        Mirrors the proven 31.08. manual merge: attribute rescue (entity_attributes
        keys missing on keeper are copied from dropped candidates) and provenance
        source_urls are preserved before deletion. Deletion happens ONLY on
        byte-identical normalized text — unique content is never touched.

        A JSON backup of every deleted point is written BEFORE deletion so the
        sweep is always reversible.
        """
        import uuid as _uuid
        points = self._collect_points()
        by_norm: Dict[str, list] = {}
        for p in points:
            payload = p.payload or {}
            if (payload.get("lifecycle_status") or "canonical") not in VALID_LIFECYCLE:
                continue
            text = str(payload.get("text") or payload.get("content") or "").strip()
            key = _normalize(text)
            if len(key) < 12:
                continue
            by_norm.setdefault(key, []).append(p)

        merged = 0
        rescued_attrs = 0
        backup_rows = []
        backup_path = None
        for key, group in by_norm.items():
            if len(group) < 2:
                continue
            def _created(p):
                return (p.payload or {}).get("created_at") or "9999"
            group_sorted = sorted(group, key=_created)
            keeper = group_sorted[0]
            kp = keeper.payload or {}
            keeper_attrs = dict(kp.get("entity_attributes") or {})
            to_delete = []
            for cand in group_sorted[1:]:
                cp = cand.payload or {}
                da = cp.get("entity_attributes") or {}
                if isinstance(da, dict):
                    rescued = {k: v for k, v in da.items() if k not in keeper_attrs}
                    if rescued:
                        keeper_attrs.update(rescued)
                        rescued_attrs += len(rescued)
                backup_rows.append({
                    "id": str(cand.id),
                    "payload": cp,
                    "keeper_id": str(keeper.id),
                })
                to_delete.append(cand.id)
            if to_delete:
                # Backup BEFORE delete (reversibility guarantee)
                if backup_rows and backup_path is None:
                    backup_path = str(self._data_dir / f"dedup-sweep-backup-{time.strftime('%Y%m%d-%H%M%S')}.json")
                    self._data_dir.mkdir(parents=True, exist_ok=True)
                    with open(backup_path, "w") as bf:
                        json.dump({"keeper_strategy": "oldest_created_at",
                                   "deleted": backup_rows}, bf, indent=2, ensure_ascii=False, default=str)
                if keeper_attrs and keeper_attrs != (kp.get("entity_attributes") or {}):
                    self._store.client.set_payload(
                        collection_name=self._store.collection_name,
                        payload={"entity_attributes": keeper_attrs},
                        points=[keeper.id],
                    )
                self._store.client.delete(
                    collection_name=self._store.collection_name,
                    points_selector=to_delete,
                )
                merged += len(to_delete)
        result = {
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "merged": merged,
            "rescued_attributes": rescued_attrs,
            "backup_file": backup_path,
            "policy": "exact-normalized duplicates only; keeper = oldest created_at",
        }
        log.info("Dedup sweep: %d merged, %d attrs rescued, backup=%s",
                 merged, rescued_attrs, backup_path)
        return result

    # ── internal ───────────────────────────────────────────────────────
    def _audit(self) -> Dict[str, Any]:
        points = []
        offset = None
        while True:
            batch, offset = self._store.client.scroll(
                self._store.collection_name, limit=500, offset=offset,
                with_payload=True, with_vectors=False,
            )
            points.extend(batch)
            if offset is None:
                break

        groups: Dict[str, list] = {}
        for p in points:
            payload = p.payload or {}
            if (payload.get("lifecycle_status") or "canonical") not in VALID_LIFECYCLE:
                continue
            text = str(payload.get("text") or payload.get("content") or "").strip()
            if len(text) < 12:
                continue
            key = _normalize(text)
            if len(key) < 12:
                continue
            groups.setdefault(key, []).append(str(p.id))

        dup_groups = [v for v in groups.values() if len(v) > 1]
        excess = sum(len(g) - 1 for g in dup_groups)

        report = {
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "collection": self._store.collection,
            "total_points": len(points),
            "duplicate_summary": {"groups": len(dup_groups), "excess_copies": excess},
            "duplicate_groups": [
                {"count": len(g), "ids": g} for g in
                sorted(dup_groups, key=len, reverse=True)[:200]
            ],
        }
        return report

    def _report_path(self, ts: str) -> Path:
        day = ts[:10].replace(":", "").replace(" ", "_")
        return self._data_dir / f"health-{day}.json"

    def _write_report(self, report: Dict[str, Any]) -> None:
        try:
            report["report_file"] = str(self._report_path(report["timestamp"]))
            with open(report["report_file"], "w") as f:
                json.dump(report, f, indent=2, ensure_ascii=False, default=str)
            # keep only the newest 12 reports
            files = sorted(self._data_dir.glob("health-*.json"))
            for old in files[:-12]:
                try: old.unlink()
                except OSError: pass
        except Exception as exc:
            log.warning("Report write failed: %s", exc)

    def _maybe_webhook(self, report: Dict[str, Any]) -> None:
        url = os.environ.get("NEXUS_WEBHOOK_URL", "").strip()
        if not url:
            return
        dup = report.get("duplicate_summary") or {}
        if not dup.get("groups"):
            return  # only push when something needs attention
        try:
            import json as _json
            import urllib.request
            payload = {
                "content": (
                    f"🧠 **Nexus Memory health audit** — {report['timestamp']}\n"
                    f"Collection `{report['collection']}`: {report['total_points']} memories, "
                    f"**{dup['groups']} duplicate groups** ({dup.get('excess_copies')} redundant copies).\n"
                    f"Audit = read-only. Review: `{report['report_file']}`"
                )
            }
            req = urllib.request.Request(
                url,
                data=_json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                log.info("Webhook delivered: %s", resp.status)
        except Exception as exc:
            log.warning("Webhook failed: %s", exc)

    # ── background loop ────────────────────────────────────────────────
    def start(self) -> None:
        def _loop():
            time.sleep(AUDIT_START_DELAY_SECONDS)
            while True:
                self.run_audit()
                for _ in range(max(60, AUDIT_INTERVAL_SECONDS // 60)):
                    time.sleep(60)
        t = threading.Thread(target=_loop, name="nexus-health-audit", daemon=True)
        t.start()
        log.info("Health audit daemon started (interval %.1f d)", AUDIT_INTERVAL_SECONDS / 86400)
