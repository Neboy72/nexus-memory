#!/usr/bin/env python3
"""
health_audit.py — self-contained health & duplicate audit for Nexus Memory.

Runs as an in-process daemon thread inside the MCP server (harness-independent,
no external scheduler required). Produces:
  1. JSON report files under <data-dir>/reports/ (always, every harness can read them)
  2. in-memory flags surfaced by the `health` tool (dedup_flags / health_flags)
     so the connected agent SEES them on its next health check and can tell the user
  3. optional webhook POST (NEXUS_WEBHOOK_URL) — Discord/Telegram/n8n/whatever

DESIGN RULES (agreed with Nebo 2026-08-31, hardened after the 2026-09 review):
  - READ-ONLY by default. This module NEVER deletes or modifies memories.
    Repair happens via the established backup->prune->verify workflow.
  - The in-process dedup sweep is DESTRUCTIVE and therefore OPT-IN ONLY:
    it runs exclusively with NEXUS_DEDUP_SWEEP=1 (default: off). Every
    report, flag and webhook message states the actual behavior — a run
    that deleted points is never announced as "read-only".
  - Deletion decisions compare FULL content losslessly (SHA-256 of the
    complete normalized text, scoped to one security context). Short /
    truncated keys are used only to FIND duplicate candidates, never as
    a deletion proof.
  - All deletion candidates are collected first and backed up completely
    (payload + vector + original id type) in ONE atomically written JSON
    file BEFORE the first deletion; the backup is re-read and verified
    before Qdrant is touched.
  - No external scheduler needed: thread lives with the server process.
  - Failures are logged, never thrown into the MCP loop.
"""
import hashlib
import json
import logging
import os
import re
import threading
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("nexus.health_audit")

AUDIT_INTERVAL_SECONDS = int(os.environ.get("NEXUS_AUDIT_INTERVAL", 30 * 24 * 3600))  # 30 days
AUDIT_START_DELAY_SECONDS = 45  # let the server finish booting first

VALID_LIFECYCLE = {"canonical", None, "", "staged", "pending"}

# Categories that must never be auto-deleted by the dedup sweep. Protection
# rules and procedures are excluded entirely (same protected set the
# selective-forgetting auditor uses).
PROTECTED_CATEGORIES = {"rule", "procedure"}

# Explicit opt-in values for the destructive dedup sweep. Unset or any other
# value keeps the audit read-only.
SWEEP_OPT_IN_VALUES = {"1", "true", "yes", "on"}


def _sweep_enabled() -> bool:
    """True only with an explicit opt-in (NEXUS_DEDUP_SWEEP=1/true/yes/on)."""
    return os.environ.get("NEXUS_DEDUP_SWEEP", "").strip().lower() in SWEEP_OPT_IN_VALUES


def _normalize(text: str) -> str:
    """Lossless canonical form: lowercased, whitespace-collapsed, FULL length.

    Never truncates and never strips content markers. The hash of this value
    is the deletion proof for the dedup sweep.
    """
    key = (text or "").lower()
    key = re.sub(r"\s+", " ", key).strip()
    return key


def _candidate_key(text: str) -> str:
    """Lossy truncated key (max 300 chars) for candidate FINDING only.

    Two memories sharing this key are merely candidates; the actual deletion
    decision always uses the full-content hash, so distinct memories with a
    common prefix are never merged.
    """
    return _normalize(text)[:300]


def _content_hash(text: str) -> str:
    """Lossless full-content identity (SHA-256 of the complete normalized text)."""
    return hashlib.sha256(_normalize(text).encode("utf-8")).hexdigest()


def _security_context(payload: Dict[str, Any]) -> str:
    """Security/tenant identity of a point: category + access level + owner attrs.

    Duplicate candidates may only merge inside ONE identical context, so a
    protected rule is never dropped in favor of a look-alike fact and private
    metadata never merges into a public point.
    """
    prov = payload.get("provenance")
    prov = prov if isinstance(prov, dict) else {}
    return "|".join((
        str(payload.get("category") or "fact"),
        str(payload.get("access_level") or ""),
        str(payload.get("owner_id") or ""),
        str(payload.get("agent_id") or ""),
        str(prov.get("created_by") or ""),
    ))


def _is_audit_entry(payload: Dict[str, Any]) -> bool:
    """Guardrail-override audit entries are excluded from the dedup sweep."""
    return bool(payload.get("guardrail_override"))


def _backup_id(point_id: Any) -> Tuple[Any, str]:
    """Keep the original id value/type for the backup (restore fidelity)."""
    if isinstance(point_id, int):
        return point_id, "int"
    return str(point_id), type(point_id).__name__


def _json_vector(vector: Any) -> Any:
    """Make a qdrant vector JSON-serializable (unnamed list or named dict)."""
    if vector is None:
        return None
    if isinstance(vector, dict):
        return {
            k: (list(v) if isinstance(v, (list, tuple)) else v)
            for k, v in vector.items()
        }
    return list(vector)


class HealthAuditor:
    """Periodic read-only audit: duplicate groups + collection stats.

    Write the report under the nexus data dir and expose flags for the
    health tool. Deletes points ONLY via the explicitly opted-in dedup
    sweep (NEXUS_DEDUP_SWEEP=1), and only after a verified full backup.
    """

    def __init__(self, store, collection: str, data_dir: Optional[str] = None) -> None:
        self._store = store
        self._collection = collection
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
            sweep = r.get("dedup_sweep") or {}
            merged = int(sweep.get("merged") or 0)
            if merged:
                # Truthful reporting: the sweep DID delete points.
                message = (
                    f"⚠️ Nexus Memory health: {dup.get('groups')} duplicate groups "
                    f"({dup.get('excess_copies')} redundant copies) detected during the last audit. "
                    f"The opt-in dedup sweep DELETED {merged} redundant copies "
                    f"(backup: {sweep.get('backup_file') or 'n/a'})."
                )
            else:
                message = (
                    f"⚠️ Nexus Memory health: {dup.get('groups')} duplicate groups "
                    f"({dup.get('excess_copies')} redundant copies) detected during the last audit. "
                    f"Tell your user: 'Nexus Memory found memory duplicates - run nexus-dedup "
                    f"or review the report at {r.get('report_file', '')}'. "
                    f"Audit is READ-ONLY; cleanup is a separate manual step."
                )
            flags["dedup"] = {
                "duplicate_groups": dup.get("groups"),
                "excess_copies": dup.get("excess_copies", 0),
                "message": message,
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
            # 2026-09-02 (Nebo-GO): in-process dedup sweep — DESTRUCTIVE and
            # therefore OPT-IN ONLY (NEXUS_DEDUP_SWEEP=1; default = off, the
            # audit stays read-only). It runs BEFORE the report write and the
            # webhook so the notification reports the actual behavior
            # (deleted vs. not deleted) instead of a blanket "read-only".
            if _sweep_enabled():
                try:
                    report["dedup_sweep"] = self._dedup_sweep()
                except Exception as sweep_exc:
                    log.warning("Dedup sweep failed: %s", sweep_exc)
            self._write_report(report)
            self._maybe_webhook(report)
        except Exception as exc:  # never break the server
            log.warning("Health audit failed: %s", exc)
            return agent_cleanup  # still return the registry report
        # 2026-09-02: selective-forgetting scoring runs in the SAME loop, as an
        # independent step (its failure must not affect the dup report).
        try:
            from nexus_memory.selective_forgetting import SelectiveForgettingAuditor
            forget = SelectiveForgettingAuditor(self._store, self._collection,
                                                data_dir=str(self._data_dir))
            forget_report = forget.run()
            with self._lock:
                self._forget_report = forget_report
        except Exception as exc:
            log.warning("Selective-forgetting audit failed: %s", exc)
        return report

    # ── dedup sweep (in-process self-maintenance, OPT-IN ONLY) ─────────
    def _collect_points(self, with_vectors: bool = False):
        points = []
        offset = None
        while True:
            batch, offset = self._store.client.scroll(
                self._collection, limit=500, offset=offset,
                with_payload=True, with_vectors=with_vectors,
            )
            points.extend(batch)
            if offset is None:
                break
        return points

    def _dedup_sweep(self) -> Dict[str, Any]:
        """Merge full-content-identical duplicates, oldest point wins as keeper.

        Two-phase design:
          Phase 1 (find): candidate groups by the SHORT truncated key. This
          lossy key is used ONLY to narrow down candidates — never as a
          deletion proof.
          Phase 2 (decision): within a candidate group, points are merged only
          when the FULL content hash is identical AND the security context
          (category + access level + owner/agent) matches. Unique content is
          never touched. Metadata (entity_attributes) rescue happens only
          within the same security context. Rules and audit entries are
          excluded entirely.

        ALL deletion candidates are collected up front and written to a single
        atomically created JSON backup (payload + vector + original id type)
        BEFORE the first deletion, so the sweep is always reversible.

        Destructive: requires NEXUS_DEDUP_SWEEP=1 (checked by run_audit).
        """
        points = self._collect_points(with_vectors=True)
        by_key: Dict[str, list] = {}
        skipped_protected = 0
        for p in points:
            payload = p.payload or {}
            if (payload.get("lifecycle_status") or "canonical") not in VALID_LIFECYCLE:
                continue
            if _is_audit_entry(payload):
                skipped_protected += 1
                continue
            if str(payload.get("category") or "fact").lower() in PROTECTED_CATEGORIES:
                skipped_protected += 1
                continue
            text = str(payload.get("text") or payload.get("content") or "").strip()
            ckey = _candidate_key(text)
            if len(ckey) < 12:
                continue
            by_key.setdefault(ckey, []).append(p)

        # Phase 2: build the full deletion plan with lossless proofs.
        plan: List[Tuple[Any, Dict[str, Any], Dict[str, Any], list]] = []
        backup_rows = []
        rescued_attrs = 0
        for ckey, cands in by_key.items():
            if len(cands) < 2:
                continue
            # Deletion proof = (full-content hash, security context) — never
            # the truncated candidate key.
            subgroups: Dict[Tuple[str, str], list] = defaultdict(list)
            for p in cands:
                payload = p.payload or {}
                full_text = str(payload.get("text") or payload.get("content") or "").strip()
                subgroups[(_security_context(payload), _content_hash(full_text))].append(p)
            for (_ctx, _chash), grp in subgroups.items():
                if len(grp) < 2:
                    continue

                def _created(p):
                    return (p.payload or {}).get("created_at") or "9999"

                group_sorted = sorted(grp, key=_created)
                keeper = group_sorted[0]
                kp = keeper.payload or {}
                orig_attrs = dict(kp.get("entity_attributes") or {})
                keeper_attrs = dict(orig_attrs)
                del_ids = []
                # Metadata merge only within this same security context.
                for cand in group_sorted[1:]:
                    cp = cand.payload or {}
                    da = cp.get("entity_attributes") or {}
                    if isinstance(da, dict):
                        for k, v in da.items():
                            if k not in keeper_attrs:
                                keeper_attrs[k] = v
                                rescued_attrs += 1
                    bid, btype = _backup_id(cand.id)
                    backup_rows.append({
                        "id": bid,
                        "id_type": btype,
                        "collection": self._collection,
                        "payload": cp,
                        "vector": _json_vector(getattr(cand, "vector", None)),
                        "keeper_id": str(keeper.id),
                    })
                # collect deletions for this subgroup
                to_delete = [cand.id for cand in group_sorted[1:]]
                plan.append((keeper, keeper_attrs, orig_attrs, to_delete))

        merged = 0
        backup_path = None
        if plan:
            # Backup ALL candidates in ONE atomically written file BEFORE the
            # first deletion — never a partial backup.
            backup_path = str(self._data_dir / f"dedup-sweep-backup-{time.strftime('%Y%m%d-%H%M%S')}.json")
            self._atomic_write_json(backup_path, {
                "keeper_strategy": "oldest_created_at",
                "collection": self._collection,
                "deleted": backup_rows,
            })
            # Verify the backup is complete and readable BEFORE deleting.
            with open(backup_path, "r", encoding="utf-8") as check:
                verified = json.load(check)
            if len(verified.get("deleted") or []) != len(backup_rows):
                raise RuntimeError(
                    "dedup backup verification failed; no point was deleted")
            for keeper, keeper_attrs, orig_attrs, to_delete in plan:
                if keeper_attrs and keeper_attrs != orig_attrs:
                    self._store.client.set_payload(
                        collection_name=self._collection,
                        payload={"entity_attributes": keeper_attrs},
                        points=[keeper.id],
                    )
                self._store.client.delete(
                    collection_name=self._collection,
                    points_selector=to_delete,
                )
                merged += len(to_delete)
        result = {
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "collection": self._collection,
            "merged": merged,
            "rescued_attributes": rescued_attrs,
            "backup_file": backup_path,
            "skipped_protected": skipped_protected,
            "policy": (
                "full-content-hash duplicates within one security context only; "
                "keeper = oldest created_at; rules + audit entries excluded; "
                "single atomic backup before first deletion; "
                "destructive sweep requires NEXUS_DEDUP_SWEEP=1"
            ),
        }
        log.info("Dedup sweep: %d merged, %d attrs rescued, backup=%s",
                 merged, rescued_attrs, backup_path)
        return result

    def _atomic_write_json(self, path: str, data: Dict[str, Any]) -> None:
        """Write JSON atomically: temp file + fsync + os.replace."""
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as bf:
            json.dump(data, bf, indent=2, ensure_ascii=False, default=str)
            bf.flush()
            os.fsync(bf.fileno())
        os.replace(tmp, path)

    # ── internal ───────────────────────────────────────────────────────
    def _audit(self) -> Dict[str, Any]:
        points = []
        offset = None
        while True:
            batch, offset = self._store.client.scroll(
                self._collection, limit=500, offset=offset,
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
            "collection": self._collection,
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
        # Truthful reporting: state whether the run actually deleted points.
        sweep = report.get("dedup_sweep") or {}
        merged = int(sweep.get("merged") or 0)
        if merged:
            mode = "destructive (opt-in dedup sweep ran)"
            action = (
                f"⚠️ DELETED {merged} duplicate copies. "
                f"Backup: `{sweep.get('backup_file') or 'n/a'}`"
            )
        elif sweep:
            mode = "read-only"
            action = "Dedup sweep (opt-in) ran; no safely mergeable duplicates — nothing was deleted."
        else:
            mode = "read-only"
            action = (
                "No memories were deleted (destructive dedup sweep is off "
                "by default; it only runs with NEXUS_DEDUP_SWEEP=1)."
            )
        try:
            import json as _json
            import urllib.request
            payload = {
                "content": (
                    f"🧠 **Nexus Memory health audit** — {report['timestamp']}\n"
                    f"Collection `{report['collection']}`: {report['total_points']} memories, "
                    f"**{dup['groups']} duplicate groups** ({dup.get('excess_copies')} redundant copies).\n"
                    f"Audit = {mode}. {action}\n"
                    f"Review: `{report['report_file']}`"
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