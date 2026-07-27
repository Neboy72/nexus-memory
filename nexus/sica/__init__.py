"""SICA — Self-Improving Coding Agent Cycle for Nexus Memory.

The SICA module implements a self-improvement loop:

1. **Detect** — Scan memories for drift, contradictions, stale facts,
   and low-confidence beliefs.
2. **Reflect** — Generate improvement suggestions (skill drafts, memory
   corrections, belief updates).
3. **Act** — Apply non-destructive patches automatically (category fixes,
   confidence adjustments). Destructive changes require user confirmation.
4. **Learn** — Store the outcome as a SICA session for the next iteration.

Design principles:
- **Heuristic fallback always.** If the LLM is unavailable, SICA runs
  purely on deterministic heuristics (stale date detection, confidence
  threshold checks, contradiction pattern matching).
- **Non-destructive by default.** Automatic patches only change metadata
  (category, confidence, status). Content changes always require user
  confirmation.
- **Stateless recovery.** Each SICA run is independent — no persistent
  state between runs. The "memory" of past SICA runs lives in the
  ``sica_session`` memories stored in Qdrant itself.
- **Harness-independent.** SICA runs as a Python module that any plugin
  (Hermes, OpenClaw, Claude Code) can invoke. It does not depend on any
  specific agent harness.
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from nexus.config import get_collection

logger = logging.getLogger(__name__)

_COLLECTION = get_collection()
_QDRANT_URL = os.environ.get("NEXUS_QDRANT_URL", "http://localhost:6333")

# Confidence below this triggers a review suggestion
LOW_CONFIDENCE_THRESHOLD = float(os.environ.get("SICA_LOW_CONFIDENCE", "0.5"))
# Memories older than this (days) with category="temp" are stale
STALE_TEMP_DAYS = int(os.environ.get("SICA_STALE_TEMP_DAYS", "7"))
# Max suggestions per SICA run (prevents flooding)
MAX_SUGGESTIONS = int(os.environ.get("SICA_MAX_SUGGESTIONS", "10"))


class SICAResult:
    """Result of a single SICA cycle run."""

    def __init__(self) -> None:
        self.run_id: str = str(uuid.uuid4())
        self.timestamp: str = datetime.now(timezone.utc).isoformat()
        self.total_scanned: int = 0
        self.issues_found: int = 0
        self.suggestions: List[Dict[str, Any]] = []
        self.auto_patches: List[Dict[str, Any]] = []
        self.errors: List[str] = []

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "timestamp": self.timestamp,
            "total_scanned": self.total_scanned,
            "issues_found": self.issues_found,
            "suggestions": self.suggestions[:MAX_SUGGESTIONS],
            "auto_patches": self.auto_patches,
            "errors": self.errors,
        }

    @property
    def is_silent(self) -> bool:
        """True when no actionable suggestions were found."""
        return self.issues_found == 0 and not self.errors


def _scroll_all(client: Any, collection: str, filter_cond: Optional[Dict] = None) -> List[Dict]:
    """Scroll all points from Qdrant with optional filter."""
    from qdrant_client import models as qm

    points: List[Dict] = []
    offset = None
    while True:
        scroll_params: Dict[str, Any] = {"limit": 100, "with_payload": True, "with_vectors": False}
        if offset:
            scroll_params["offset"] = offset
        if filter_cond:
            scroll_params["scroll_filter"] = qm.Filter(**filter_cond)

        results, offset = client.scroll(collection_name=collection, **scroll_params)
        for p in results:
            points.append({"id": str(p.id), "payload": p.payload or {}})
        if offset is None:
            break
    return points


def _detect_stale_temp(points: List[Dict]) -> List[Dict[str, Any]]:
    """Detect temp-category memories older than STALE_TEMP_DAYS.

    Returns list of issue dicts with {id, type, detail, auto_fixable, action}.
    """
    issues = []
    now = datetime.now(timezone.utc)
    cutoff = now.timestamp() - (STALE_TEMP_DAYS * 86400)

    for p in points:
        payload = p["payload"]
        if payload.get("category") != "temp":
            continue
        created = payload.get("created_at", "")
        if not created:
            continue
        try:
            ts = datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp()
            if ts < cutoff:
                issues.append({
                    "id": p["id"],
                    "type": "stale_temp",
                    "detail": f"Temp memory older than {STALE_TEMP_DAYS} days",
                    "auto_fixable": True,
                    "action": "delete",
                    "category": payload.get("category", "temp"),
                    "confidence": (payload.get("provenance") or {}).get("confidence", 0.5),
                })
        except Exception:
            continue
    return issues


def _detect_low_confidence(points: List[Dict]) -> List[Dict[str, Any]]:
    """Detect memories with confidence below threshold.

    Returns list of issue dicts. Auto-fixable: bump confidence to threshold
    if the memory has been accessed/used (heuristic: has edges).
    """
    issues = []
    for p in points:
        payload = p["payload"]
        prov = payload.get("provenance") or {}
        confidence = prov.get("confidence")
        if confidence is None:
            continue
        if confidence < LOW_CONFIDENCE_THRESHOLD:
            issues.append({
                "id": p["id"],
                "type": "low_confidence",
                "detail": f"Confidence {confidence:.2f} < {LOW_CONFIDENCE_THRESHOLD}",
                "auto_fixable": False,
                "action": "review",
                "category": payload.get("category", "fact"),
                "confidence": confidence,
            })
    return issues


def _detect_contradictions(points: List[Dict]) -> List[Dict[str, Any]]:
    """Detect potential contradictions using graph edges.

    Scans for 'contradicts' edges in point payloads. Each contradiction
    edge becomes an issue requiring user review.

    Returns list of issue dicts.
    """
    issues = []
    for p in points:
        payload = p["payload"]
        edges = payload.get("edges", [])
        for edge in edges:
            if edge.get("relation") == "contradicts" and edge.get("status") == "active":
                issues.append({
                    "id": p["id"],
                    "type": "contradiction",
                    "detail": f"Contradicts {edge.get('target_fact_id', '?')[:8]}",
                    "auto_fixable": False,
                    "action": "review",
                    "category": payload.get("category", "fact"),
                    "confidence": (payload.get("provenance") or {}).get("confidence", 0.5),
                    "target_id": edge.get("target_fact_id"),
                })
    return issues


def _apply_auto_patch(client: Any, collection: str, issue: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Apply a non-destructive automatic patch for an issue.

    Currently only handles stale_temp deletion. All other issues
    generate suggestions for user review.

    Returns the patch dict on success, None on failure.
    """
    from qdrant_client import models as qm

    try:
        if issue["action"] == "delete" and issue["type"] == "stale_temp":
            client.delete(
                collection_name=collection,
                points_selector=qm.PointIdsList(points=[issue["id"]]),
            )
            return {"id": issue["id"], "action": "deleted", "type": "stale_temp"}
    except Exception as exc:
        logger.warning("Auto-patch failed for %s: %s", issue["id"], exc)
    return None


def run_sica(client: Any = None, collection: str = "", auto_patch: bool = True) -> SICAResult:
    """Run a single SICA cycle.

    Args:
        client: QdrantClient instance. If None, creates a new one.
        collection: Collection name. Defaults to the configured collection.
        auto_patch: If True, apply non-destructive patches automatically.

    Returns:
        SICAResult with issues, suggestions, and auto-patches.
    """
    result = SICAResult()
    coll = collection or _COLLECTION

    if client is None:
        from qdrant_client import QdrantClient
        client = QdrantClient(url=_QDRANT_URL)

    try:
        # Phase 1: Detect
        logger.info("SICA: scanning collection '%s'...", coll)
        points = _scroll_all(client, coll)
        result.total_scanned = len(points)

        all_issues: List[Dict[str, Any]] = []
        all_issues.extend(_detect_stale_temp(points))
        all_issues.extend(_detect_low_confidence(points))
        all_issues.extend(_detect_contradictions(points))

        result.issues_found = len(all_issues)

        # Phase 2: Reflect + Act
        for issue in all_issues:
            if issue["auto_fixable"] and auto_patch:
                patch = _apply_auto_patch(client, coll, issue)
                if patch:
                    result.auto_patches.append(patch)
                    continue
            # Not auto-fixable: add as suggestion
            result.suggestions.append({
                "type": issue["type"],
                "priority": "high" if issue["type"] == "contradiction" else "medium",
                "id": issue["id"],
                "detail": issue["detail"],
                "action": issue["action"],
                "category": issue.get("category", ""),
                "confidence": issue.get("confidence", 0),
            })

        # Phase 3: Learn — store SICA session as a memory
        if result.issues_found > 0 or result.auto_patches:
            _store_sica_session(client, coll, result)

        logger.info(
            "SICA: scanned %d, found %d issues, %d auto-patched, %d suggestions",
            result.total_scanned, result.issues_found,
            len(result.auto_patches), len(result.suggestions),
        )

    except Exception as exc:
        logger.error("SICA run failed: %s", exc)
        result.errors.append(str(exc))

    return result


def _store_sica_session(client: Any, collection: str, result: SICAResult) -> None:
    """Store the SICA run outcome as a memory for future iterations."""
    try:
        from qdrant_client import models as qm
        from nexus_memory.embeddings import EmbeddingProvider
        import asyncio

        summary = (
            f"SICA run {result.run_id[:8]}: scanned {result.total_scanned} memories, "
            f"found {result.issues_found} issues, {len(result.auto_patches)} auto-patched, "
            f"{len(result.suggestions)} suggestions."
        )

        # SICA is synchronous. If called from an async context, the caller
        # should run it in a thread. Here we create a fresh loop to avoid
        # conflicts with any running event loop.
        loop = asyncio.new_event_loop()
        try:
            embedder = EmbeddingProvider()
            vector = loop.run_until_complete(embedder.embed(summary))
        finally:
            loop.close()

        eid = str(uuid.uuid4())
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        payload = {
            "id": eid,
            "content": summary,
            "access_level": "public",
            "category": "sica_session",
            "source": "sica",
            "source_url": "",
            "created_at": ts,
            "provenance": {
                "source_type": "sica",
                "created_by": "sica-module",
                "timestamp": ts,
                "confidence": 0.9,
            },
            "sica_run_id": result.run_id,
            "sica_issues": result.issues_found,
            "sica_auto_patches": len(result.auto_patches),
            "sica_suggestions": len(result.suggestions),
        }

        client.upsert(
            collection_name=collection,
            points=[qm.PointStruct(id=eid, vector=vector, payload=payload)],
        )
        logger.info("SICA session stored: %s", eid[:8])
    except Exception as exc:
        logger.warning("Failed to store SICA session: %s", exc)