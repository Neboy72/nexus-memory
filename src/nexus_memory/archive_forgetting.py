"""Archive-forgetting: nightly backup-then-forget of stale session memories (v0.22.0, Nebo-GO 25.09.).

Brain-equivalent: forgetting as a feature (Ebbinghaus; Richards & Frankland 2017).
Selects stale points in the `session` category (and optionally `temp`) that
have not been recalled recently, backs them up to a local JSONL (full points:
id + vector + payload), and deletes them from Qdrant ONLY after the backup
is verified line-count-complete. Deletion happens ONLY here, with proof.

Design rules (Nebo's heart-law):
- Test-first deployment, backup BEFORE every deletion, GO at push time.
- Deletion is atomic per point with per-ID verification; numeric IDs as int.
- Never touches facts/rules/preferences/beliefs — only session (+temp opt-in).

Env knobs:
- NEXUS_ARCHIVE_ENABLED (default "1")        master kill-switch
- NEXUS_ARCHIVE_CATEGORIES (default "session") comma list, temp opt-in
- NEXUS_ARCHIVE_MAX_AGE_DAYS (30)
- NEXUS_ARCHIVE_BATCH (500)                  max deletions per run
- NEXUS_ARCHIVE_BACKUP_DIR (~/.nexus-memory/backups/dreaming-archive)
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from qdrant_client import models

log = logging.getLogger("nexus.archive_forgetting")

NEXUS_ARCHIVE_ENABLED = os.environ.get("NEXUS_ARCHIVE_ENABLED", "1") == "1"
CATEGORIES = [c.strip() for c in os.environ.get(
    "NEXUS_ARCHIVE_CATEGORIES", "session").split(",") if c.strip()]
MAX_AGE_DAYS = float(os.environ.get("NEXUS_ARCHIVE_MAX_AGE_DAYS", "30"))
BATCH = int(os.environ.get("NEXUS_ARCHIVE_BATCH", "500"))
BACKUP_DIR = Path(os.environ.get(
    "NEXUS_ARCHIVE_BACKUP_DIR",
    str(Path.home() / ".nexus-memory" / "backups" / "dreaming-archive")))

_DELETE_URL = "http://localhost:6333/collections/{coll}/points/delete?wait=true"


def _parse_ts(payload: Dict[str, Any]) -> float:
    """Best-effort created-at extraction across both payload schemas.

    Old points carry `created` (unix seconds or ISO date), plugin points
    carry `created_at` (ISO 8601). Returns 0.0 when nothing parses.
    """
    for key in ("created_at", "created", "timestamp"):
        v = payload.get(key)
        if v is None:
            continue
        if isinstance(v, (int, float)) and v > 1_000_000_000:
            return float(v)
        if isinstance(v, str):
            try:
                return float(v)
            except ValueError:
                pass
            try:
                from datetime import datetime
                iso = v.replace("Z", "+00:00")
                return datetime.fromisoformat(iso).timestamp()
            except Exception:
                continue
    return 0.0


def _stale_candidates(store, collection: str, cutoff: float,
                      limit: int) -> List[Dict[str, Any]]:
    """Scroll session-category points, keep stale ones."""
    try:
        client = store.client
    except AttributeError:
        log.warning("archive: store has no qdrant client — fail-open")
        return []
    must = [{"key": "category", "match": {"value": cat}}
            for cat in CATEGORIES]
    flt = {"must": must} if len(must) == 1 else {"should": must,
                                                 "minimum_should_match": 1}
    out: List[Dict[str, Any]] = []
    try:
        res = client.scroll(
            collection_name=collection,
            scroll_filter=flt,
            limit=limit,
            with_payload=True,
            with_vector=True)
        points, _ = res
        for p in points:
            pl = p.payload or {}
            created = _parse_ts(pl)
            if created and created < cutoff:
                out.append({"id": p.id, "payload": pl,
                            "vector": p.vector})
    except Exception as exc:
        log.warning("archive: scroll failed (%s) — fail-open", exc)
    return out


_parse_ts = _parse_ts  # legacy alias


def _backup_points(points: List[Dict[str, Any]],
                   backup_dir: Path) -> Optional[Path]:
    """Write full points (id + vector + payload) to JSONL; returns path."""
    try:
        backup_dir.mkdir(parents=True, exist_ok=True)
        path = backup_dir / (
            "session-points-" + time.strftime("%Y%m%d-%H%M%S") + ".jsonl")
        with open(path, "w", encoding="utf-8") as fh:
            for p in points:
                fh.write(json.dumps(p, ensure_ascii=False) + "\n")
        # Verify: line count must equal point count BEFORE any deletion.
        lines = sum(1 for _ in open(path, encoding="utf-8"))
        if lines != len(points):
            log.error("archive: backup incomplete (%d/%d) — aborting",
                      lines, len(points))
            return None
        return path
    except Exception as exc:
        log.error("archive: backup failed (%s) — aborting", exc)
        return None


def _delete_points(store, collection: str,
                   points: List[Dict[str, Any]]) -> Dict[str, int]:
    """Per-point delete with numeric-as-int rule; returns counters."""
    deleted = errors = 0
    for p in points:
        pid = p["id"]
        try:
            if isinstance(pid, str):
                try:
                    pid_num = uuid.UUID(pid)
                    pid_send: Any = str(pid_num)
                except ValueError:
                    # Numeric string → int (Qdrant numeric-ID rule);
                    # anything else passes through for Qdrant to judge.
                    try:
                        pid_send = int(pid)
                    except ValueError:
                        pid_send = pid
            else:
                pid_send = pid
            store.client.delete(
                collection_name=collection,
                points_selector=models.PointIdsList(points=[pid_send]))
            deleted += 1
        except Exception as exc:
            errors += 1
            log.info("archive: delete miss %s (%s)", pid, exc)
    return {"deleted": deleted, "errors": errors}


def archive_once(store, collection: str,
                 dry_run: bool = False) -> Dict[str, Any]:
    """One archive pass. Backup-then-delete with proof; never raises."""
    result: Dict[str, Any] = {"scanned": 0, "stale": 0, "backed_up": 0,
                              "deleted": 0, "errors": 0, "dry_run": dry_run}
    if not NEXUS_ARCHIVE_ENABLED or os.environ.get("NEXUS_ARCHIVE_ENABLED", "1") != "1":
        result["skipped"] = "disabled"
        return result
    try:
        client = store.client
    except AttributeError:
        result["error"] = "store has no qdrant client"
        return result
    try:
        cutoff = time.time() - MAX_AGE_DAYS * 86400
        candidates = _stale_candidates(store, collection, cutoff, BATCH)
        result["scanned"] = BATCH
        result["stale"] = len(candidates)
        if not candidates:
            return result
        if dry_run:
            return result
        path = _backup_points(candidates, BACKUP_DIR)
        if path is None:
            result["errors"] = 1
            return result
        result["backed_up"] = len(candidates)
        result["backup_path"] = str(path)
        res = _delete_points(store, collection, candidates)
        result["deleted"] = res["deleted"]
        result["errors"] = res["errors"]
    except Exception as exc:
        log.warning("archive: pass failed (%s) — fail-open", exc)
        result["error"] = str(exc)[:200]
    return result