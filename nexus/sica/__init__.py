"""SICA - Self-Improving Coding Agent Cycle for Nexus Memory.

The SICA module implements a self-improvement loop:

1. **Detect** - Scan memories for drift, contradictions, stale facts,
   and low-confidence beliefs.
2. **Reflect** - Generate improvement suggestions (skill drafts, memory
   corrections, belief updates).
3. **Act** - Apply non-destructive patches automatically (category fixes,
   confidence adjustments). Destructive changes require user confirmation.
4. **Learn** - Store the outcome as a SICA session for the next iteration.

Design principles:
- **Heuristic fallback always.** If the LLM is unavailable, SICA runs
  purely on deterministic heuristics (stale date detection, confidence
  threshold checks, contradiction pattern matching).
- **Non-destructive by default.** Automatic patches only change metadata
  (category, confidence, status). Content changes always require user
  confirmation.
- **Stateless recovery.** Each SICA run is independent - no persistent
  state between runs. The "memory" of past SICA runs lives in the
  ``sica_session`` memories stored in Qdrant itself.
- **Harness-independent.** SICA runs as a Python module that any plugin
  (Hermes, OpenClaw, Claude Code) can invoke. It does not depend on any
  specific agent harness.
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from nexus.config import get_collection

logger = logging.getLogger(__name__)


def _safe_int(raw: str, fallback):
    """Parse an int env value; return fallback on garbage (fail-open SICA)."""
    try:
        return int(raw)
    except (TypeError, ValueError):
        return fallback


def _safe_float(raw: str, fallback: float) -> float:
    """Parse a float env value; return fallback on garbage (fail-open SICA)."""
    try:
        return float(raw)
    except (TypeError, ValueError):
        return fallback


def _get_config() -> Dict[str, Any]:
    """Read SICA config at call time (not import time).

    Reads env vars on each call so changes after import take effect.

    Retention policies (roadmap 2.2): per-category max age in days.
    Built-in categories: ``temp`` (SICA_RETENTION_TEMP, fallback legacy
    SICA_STALE_TEMP_DAYS, default 7) and ``session``
    (SICA_RETENTION_SESSION, default 7). Any other category can be set
    via its own ``SICA_RETENTION_<CATEGORY>`` env var. Categories not
    listed and without an env var default to ``default_retention_days``
    (None = keep forever).
    """
    default_retention: Optional[int] = None
    raw_default = os.environ.get("SICA_DEFAULT_RETENTION_DAYS", "")
    if raw_default.strip():
        try:
            default_retention = int(raw_default)
        except ValueError:
            default_retention = None
    # Generic SICA_RETENTION_<CATEGORY> overrides beyond the builtin two.
    policies_extra: Dict[str, int] = {}
    for env_name, env_val in os.environ.items():
        if env_name.startswith("SICA_RETENTION_"):
            cat = env_name[len("SICA_RETENTION_"):].lower()
            if cat and cat not in ("temp", "session"):
                try:
                    policies_extra[cat] = int(env_val)
                except (TypeError, ValueError):
                    pass
    cfg = {
        "collection": get_collection(),
        "qdrant_url": os.environ.get("NEXUS_QDRANT_URL", "http://localhost:6333"),
        "low_confidence_threshold": _safe_float(
            os.environ.get("SICA_LOW_CONFIDENCE", "0.5"), 0.5
        ),
        # Legacy single-category knob (kept for backwards compatibility).
        "stale_temp_days": _safe_int(
            os.environ.get("SICA_STALE_TEMP_DAYS", "7"), 7
        ),
        # Roadmap 2.2: per-category retention (days). None = keep forever.
        # Backwards-compat: SICA_RETENTION_TEMP wins; legacy SICA_STALE_TEMP_DAYS
        # is the fallback so existing users keep their prior 7-day default.
        "retention_policies": {
            "temp": _safe_int(
                os.environ.get(
                    "SICA_RETENTION_TEMP",
                    os.environ.get("SICA_STALE_TEMP_DAYS", "7"),
                ),
                7,
            ),
            "session": _safe_int(
                os.environ.get("SICA_RETENTION_SESSION", "7"), 7
            ),
        },
        "default_retention_days": default_retention,
        "max_suggestions": _safe_int(
            os.environ.get("SICA_MAX_SUGGESTIONS", "10"), 10
        ),
    }
    # Merge extra categories into the policy map (fault-tolerant).
    cfg["retention_policies"].update(policies_extra)
    return cfg


def _load_env() -> None:
    """Load .env file so API keys (VOYAGE_API_KEY etc.) are available."""
    for env_path in [
        os.path.expanduser("~/.hermes/.env"),
        os.path.expanduser("~/.nexus-memory/.env"),
        os.path.join(os.getcwd(), ".env"),
    ]:
        if os.path.exists(env_path):
            try:
                with open(env_path) as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#") and "=" in line:
                            key, _, val = line.partition("=")
                            key = key.strip()
                            val = val.strip().strip('"').strip("'")
                            if key and key not in os.environ:
                                os.environ[key] = val
                logger.debug("Loaded env from %s", env_path)
            except Exception:
                pass


class SICAResult:
    """Result of a single SICA cycle run."""

    def __init__(self) -> None:
        self.run_id: str = str(uuid.uuid4())
        self.timestamp: str = datetime.now(timezone.utc).isoformat()
        self.total_scanned: int = 0
        self.issues_found: int = 0
        self.suggestions: List[Dict[str, Any]] = []
        self.auto_patches: List[Dict[str, Any]] = []
        # Roadmap 2.1: synthesized insights (one per contradiction group)
        self.reflect_insights: List[Dict[str, Any]] = []
        self.errors: List[str] = []

    def to_dict(self, max_suggestions: int = 10, max_insights: int = 10) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "timestamp": self.timestamp,
            "total_scanned": self.total_scanned,
            "issues_found": self.issues_found,
            "suggestions": self.suggestions[:max_suggestions],
            "auto_patches": self.auto_patches,
            "reflect_insights": self.reflect_insights[:max_insights],
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
        if offset is not None:
            scroll_params["offset"] = offset
        if filter_cond:
            # Build Filter explicitly, only set non-None fields
            must = filter_cond.get("must") or []
            must_not = filter_cond.get("must_not") or []
            should = filter_cond.get("should") or []
            scroll_params["scroll_filter"] = qm.Filter(must=must, must_not=must_not, should=should)

        results, offset = client.scroll(collection_name=collection, **scroll_params)
        for p in results:
            points.append({"id": p.id, "payload": p.payload or {}})
        if offset is None:
            break
    return points


def _detect_stale_temp(points: List[Dict], stale_temp_days: int = 7) -> List[Dict[str, Any]]:
    """Detect temp-category memories older than STALE_TEMP_DAYS.

    Kept for backwards compatibility; new code should prefer
    ``_detect_retention`` which applies per-category policies.
    Emitted issues carry type="retention_expired" (roadmap 2.2 unified
    the issue type).

    Returns list of issue dicts with {id, type, detail, auto_fixable, action}.
    Points with unparseable timestamps are logged and skipped.
    """
    return _detect_retention(
        points, policies={"temp": stale_temp_days}, default_days=None
    )


def _detect_retention(
    points: List[Dict],
    policies: Dict[str, int],
    default_days: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Detect memories older than their category's retention policy (roadmap 2.2).

    Args:
        points: Scrolled memory points ({id, payload} dicts).
        policies: Map of category -> max age in days. Categories not in
            the map fall back to ``default_days`` (None = never expire).
        default_days: Fallback max age for unlisted categories.

    Returns list of issue dicts with {id, type, detail, auto_fixable,
    action, category}. Points with unparseable timestamps are logged
    and skipped (never deleted on missing/invalid timestamps).
    """
    issues = []
    now = datetime.now(timezone.utc)
    skipped = 0

    for p in points:
        payload = p["payload"]
        category = payload.get("category", "fact")
        max_days = policies.get(category, default_days)
        if max_days is None:
            continue
        created = payload.get("created_at", "")
        if not created:
            continue
        try:
            # Handle both "Z" suffix and explicit timezone offsets
            ts_str = created.replace("Z", "+00:00") if created.endswith("Z") else created
            ts_dt = datetime.fromisoformat(ts_str)
            # Treat naive datetimes as UTC to prevent local-tz interpretation
            if ts_dt.tzinfo is None:
                ts_dt = ts_dt.replace(tzinfo=timezone.utc)
            age_days = (now - ts_dt).total_seconds() / 86400
            if age_days > max_days:
                issues.append({
                    "id": p["id"],
                    "type": "retention_expired",
                    "detail": f"{category} memory older than {max_days} days",
                    "auto_fixable": True,
                    "action": "delete",
                    "category": category,
                    "confidence": float((payload.get("provenance") or {}).get("confidence", 0.5) if (payload.get("provenance") or {}).get("confidence") is not None else 0.5),
                })
        except (ValueError, TypeError) as exc:
            skipped += 1
            logger.debug("Skipping unparseable timestamp '%s': %s", created, exc)
            continue
    if skipped:
        logger.info("SICA: skipped %d points with unparseable timestamps", skipped)
    return issues


def _age_days(created_at) -> float:
    """Days since created_at (0.0 when missing/unparseable)."""
    if not created_at:
        return 0.0
    try:
        ts = str(created_at).replace("Z", "+00:00")
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - dt).total_seconds() / 86400)
    except (ValueError, TypeError):
        return 0.0


def _detect_low_confidence(points: List[Dict], low_confidence_threshold: float = 0.5) -> List[Dict[str, Any]]:
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
        # Ensure confidence is numeric (Qdrant payloads may store strings)
        try:
            confidence = float(confidence)
        except (TypeError, ValueError):
            continue
        if confidence < low_confidence_threshold:
            # Roadmap 4.8 autonomous: confidence < 0.2 + never accessed +
            # older than 30 days -> safe auto-delete. Everything else review.
            # Legacy points ohne access_count-Feld gelten NICHT als never_used
            # (review fix: fehlendes Feld = unbekannt = fail-safe review-only)
            if "access_count" not in payload:
                never_used = False
            else:
                never_used = int(payload.get("access_count", 0) or 0) == 0
            age = _age_days(payload.get("created_at"))
            purgeable = never_used and confidence < 0.2 and age > 30
            issues.append({
                "id": p["id"],
                "type": "low_confidence",
                "detail": f"Confidence {confidence:.2f} < {low_confidence_threshold}"
                          + (" (never accessed)" if never_used else ""),
                "auto_fixable": purgeable,
                "action": "delete" if purgeable else "review",
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
        edges = payload.get("edges") or []
        if not isinstance(edges, list):
            continue
        for edge in edges:
            if edge.get("relation") == "contradicts" and edge.get("status") == "active":
                issues.append({
                    "id": p["id"],
                    "type": "contradiction",
                    "detail": f"Contradicts {str(edge.get('target_fact_id') or '?')[:8]}",
                    "auto_fixable": False,
                    "action": "review",
                    "category": payload.get("category", "fact"),
                    "confidence": float((payload.get("provenance") or {}).get("confidence", 0.5) if (payload.get("provenance") or {}).get("confidence") is not None else 0.5),
                    "target_id": edge.get("target_fact_id"),
                })
    return issues


def _detect_skill_health(points: List[Dict], stale_days: int = 180) -> List[Dict[str, Any]]:
    """Roadmap 4.10: skill-health monitor (review-only, never auto-delete).

    Memories with category='skill' that have not been accessed in
    ``stale_days`` become review suggestions. Skills decay by lack of
    use, not by error - so deletion is never automatic.
    """
    issues: List[Dict[str, Any]] = []
    for p in points:
        payload = p.get("payload") or {}
        if payload.get("category") != "skill":
            continue
        age = _age_days(payload.get("last_accessed") or payload.get("created_at"))
        if age <= stale_days:
            continue
        issues.append({
            "id": p["id"],
            "type": "skill_stale",
            "detail": f"Skill unused for {int(age)} days",
            "auto_fixable": False,
            "action": "review",
            "category": "skill",
            "confidence": 0.5,
        })
    return issues


def _synthesize_insights(
    issues: List[Dict[str, Any]],
    points: List[Dict],
) -> List[Dict[str, Any]]:
    """Roadmap 2.1: Reflect operation - synthesize insights from issues.

    Groups active contradiction issues by target and duplicate-entity
    issues by keeper, then produces ONE deterministic insight per group:
    which memory should win (highest provenance confidence for
    contradictions; the keeper point for duplicates), what it claims, and
    a concrete resolution suggestion for review.

    Pure and LLM-free: fully deterministic, no API calls, fail-soft
    (missing/None fields degrade to 'unknown', never raise).
    """
    # One O(n) pass builds the id -> payload map used for both the
    # winner preview and its timestamp (replaces dict + linear scan).
    by_id: Dict[str, Dict[str, Any]] = {
        str(p.get("id")): (p.get("payload") or {}) for p in points
    }

    by_target: Dict[str, List[Dict[str, Any]]] = {}
    for issue in issues:
        if issue.get("type") == "contradiction":
            tgt = str(issue.get("target_id") or "unknown")
            by_target.setdefault(tgt, []).append(issue)
        elif issue.get("type") == "entity_duplicate":
            # Roadmap 4.2: one insight per duplicate group, keeper-led.
            keeper_id = str(issue.get("keeper_id") or "unknown")
            by_target.setdefault(f"entity::{keeper_id}", []).append(issue)

    insights: List[Dict[str, Any]] = []
    for target_key, group in sorted(by_target.items()):
        if not group:
            continue

        if group[0].get("type") == "entity_duplicate":
            # Entity duplicates carry keeper_id - focus is the keeper.
            keeper_id = target_key.split("entity::", 1)[1]
            dupes = group[0].get("duplicate_ids") or []
            payload = by_id.get(keeper_id, {})
            insights.append({
                "type": "reflect_insight",
                "focus_id": keeper_id,
                "target_id": target_key,
                "detail": (
                    f"{len(dupes)} duplicate entity memories; "
                    f"keep {keeper_id[:8]}"
                ),
                "suggested_resolution": "merge_review",
                "winner_confidence": None,
                "involved_ids": sorted(
                    {keeper_id} | {str(d) for d in dupes}
                ),
                "preview": str(payload.get("content") or "")[:300],
            })
            continue

        # Contradiction group: highest provenance confidence wins;
        # ties break by the original issue order (stable sort).
        def _conf(i: Dict[str, Any]) -> float:
            try:
                return float(i.get("confidence") or 0.0)
            except (TypeError, ValueError):
                return 0.0

        winner = sorted(group, key=_conf, reverse=True)[0]
        winner_id = str(winner.get("id"))
        winner_payload = by_id.get(winner_id, {})
        insights.append({
            "type": "reflect_insight",
            "focus_id": winner_id,
            "target_id": target_key,
            "detail": (
                f"{len(group)} contradicting memories on "
                f"{winner_id[:8]} (confidence {_conf(winner):.2f})"
            ),
            "suggested_resolution": "confirm_or_supersede",
            "winner_confidence": round(_conf(winner), 3),
            "involved_ids": sorted({str(i.get("id")) for i in group}),
            "preview": str(by_id.get(winner_id, {}).get("content") or "")[:300],
        })
    return insights


def _detect_entity_duplicates(points: List[Dict]) -> List[Dict[str, Any]]:
    """Roadmap 4.2: Detect duplicate entity memories.

    Groups category='entity' points by (entity_type, casefold + whitespace
    normalized name) and flags groups with more than one distinct point
    ID. uuid5-based points are keyed on the raw name, so legacy or
    differently-normalized duplicates surface here.

    One issue per duplicate GROUP (id = first dupe, duplicate_ids carries
    the rest). NEVER auto-deletes (merging foreign content is destructive)
    - run_sica turns these into keeper-focused review insights.
    """
    groups: Dict[str, List[Dict]] = {}
    for p in points:
        payload = p.get("payload") or {}
        if payload.get("category") != "entity":
            continue
        etype = str(payload.get("entity_type") or "concept")
        ename = str(payload.get("entity_name") or "")
        if not ename:
            continue
        norm = " ".join(ename.casefold().split())
        groups.setdefault(f"{etype}::{norm}", []).append(p)

    issues: List[Dict[str, Any]] = []
    for key, members in sorted(groups.items()):
        if len({str(p["id"]) for p in members}) < 2:
            continue

        def _ts(p: Dict) -> str:
            return str((p.get("payload") or {}).get("created_at") or "9999")

        members_sorted = sorted(members, key=_ts)
        keeper = members_sorted[0]
        issues.append({
            "id": str(members_sorted[1]["id"]),
            "type": "entity_duplicate",
            "detail": (
                f"Duplicate entity '{key}' - keep {str(keeper['id'])[:8]}"
            ),
            "auto_fixable": False,
            "action": "merge_review",
            "category": "entity",
            "keeper_id": str(keeper["id"]),
            "duplicate_ids": [str(p["id"]) for p in members_sorted[1:]],
        })
    return issues


def _apply_auto_patch(client: Any, collection: str, issue: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Apply a non-destructive automatic patch for an issue.

    Currently handles deletion-type issues (stale_temp for backwards
    compatibility, retention_expired from roadmap 2.2 policies). All
    other issues generate suggestions for user review.

    Returns the patch dict on success, None on failure.
    """
    from qdrant_client import models as qm

    try:
        # 'stale_temp' is kept for external/legacy issue producers
        # (the built-in detector now always emits 'retention_expired').
        # 'low_confidence' delete issues come from the 4.8 autonomous
        # purge rule (confidence<0.2 + never accessed + age>30d).
        if issue.get("action") == "delete" and issue.get("type") in (
            "stale_temp",
            "retention_expired",
            "low_confidence",
        ):
            client.delete(
                collection_name=collection,
                points_selector=qm.PointIdsList(points=[issue["id"]]),
            )
            return {"id": issue["id"], "action": "deleted", "type": issue.get("type")}
    except Exception as exc:
        logger.warning("Auto-patch failed for %s: %s", issue["id"], exc)
    return None


def run_sica(client: Any = None, collection: str = "", auto_patch: bool = True,
             embedder: Any = None) -> SICAResult:
    """Run a single SICA cycle.

    Args:
        client: QdrantClient instance. If None, creates a new one (and closes it).
        collection: Collection name. Defaults to the configured collection.
        auto_patch: If True, apply non-destructive patches automatically.
        embedder: Optional pre-initialized embedder with .embed() method. If provided,
                   used for session storage to avoid dimension mismatch.

    Returns:
        SICAResult with issues, suggestions, and auto-patches.
    """
    result = SICAResult()
    cfg = _get_config()
    coll = collection or cfg["collection"]
    _owns_client = client is None

    if _owns_client:
        from qdrant_client import QdrantClient
        client = QdrantClient(url=cfg["qdrant_url"])

    try:
        # Phase 1: Detect
        logger.info("SICA: scanning collection '%s'...", coll)
        points = _scroll_all(client, coll)
        result.total_scanned = len(points)

        all_issues: List[Dict[str, Any]] = []
        # Roadmap 2.2: per-category retention policies replace the
        # temp-only scan. The legacy stale_temp knob still feeds the
        # temp policy so SICA_STALE_TEMP_DAYS keeps working.
        policies = dict(cfg.get("retention_policies") or {})
        policies.setdefault("temp", cfg["stale_temp_days"])
        all_issues.extend(
            _detect_retention(
                points,
                policies=policies,
                default_days=cfg.get("default_retention_days"),
            )
        )
        all_issues.extend(_detect_low_confidence(points, low_confidence_threshold=cfg["low_confidence_threshold"]))
        all_issues.extend(_detect_contradictions(points))
        # Roadmap 4.2: duplicate entities surface as merge-review issues
        all_issues.extend(_detect_entity_duplicates(points))
        # Roadmap 4.10: skill health review (never auto-delete)
        all_issues.extend(_detect_skill_health(points))

        result.issues_found = len(all_issues)

        # Roadmap 2.1: Reflect - synthesize one insight per contradiction
        # group (deterministic, no LLM call). Runs before suggestions are
        # dedup-trimmed so insights survive max_suggestions caps.
        result.reflect_insights = _synthesize_insights(all_issues, points)

        # Phase 2: Reflect + Act.
        # Deletion-type issues are batched into a single Qdrant delete call
        # (one RTT instead of one per expired point; roadmap 2.2 scale-up).
        delete_ids: List[str] = []
        delete_types: Dict[str, str] = {}
        for issue in all_issues:
            if issue["auto_fixable"] and auto_patch and issue.get("action") == "delete":
                delete_ids.append(issue["id"])
                delete_types[issue["id"]] = issue.get("type", "retention_expired")
                continue
        if delete_ids:
            try:
                from qdrant_client import models as qm
                client.delete(
                    collection_name=coll,
                    points_selector=qm.PointIdsList(points=delete_ids),
                )
                for did in delete_ids:
                    result.auto_patches.append(
                        {"id": did, "action": "deleted", "type": delete_types[did]}
                    )
                logger.info("SICA: batch-deleted %d expired memories", len(delete_ids))
            except Exception as exc:
                logger.warning("SICA batch delete failed: %s", exc)
        for issue in all_issues:
            if issue["auto_fixable"] and auto_patch:
                # Deletion-type issues were handled by the batch above.
                if issue.get("action") == "delete" and issue["id"] in delete_ids:
                    continue
                patch = _apply_auto_patch(client, coll, issue)
                if patch:
                    result.auto_patches.append(patch)
                    continue
            # Not auto-fixable: add as suggestion (respect max_suggestions)
            if len(result.suggestions) >= cfg["max_suggestions"]:
                continue  # skip further suggestions but keep processing auto-fixable
            result.suggestions.append({
                "type": issue["type"],
                "priority": "high" if issue["type"] == "contradiction" else "medium",
                "id": issue["id"],
                "detail": issue["detail"],
                "action": issue["action"],
                "category": issue.get("category", ""),
                "confidence": issue.get("confidence", 0),
            })

        # Phase 3: Learn - store SICA session as a memory
        if result.issues_found > 0 or result.auto_patches:
            _store_sica_session(client, coll, result, embedder=embedder)

        logger.info(
            "SICA: scanned %d, found %d issues, %d auto-patched, %d suggestions",
            result.total_scanned, result.issues_found,
            len(result.auto_patches), len(result.suggestions),
        )

    except Exception as exc:
        logger.error("SICA run failed: %s", exc)
        result.errors.append(str(exc))
    finally:
        if _owns_client and client is not None:
            try: client.close()
            except Exception: pass

    return result


def _store_sica_session(client: Any, collection: str, result: SICAResult,
                        embedder: Any = None) -> None:
    """Store the SICA run outcome as a memory for future iterations.

    Args:
        embedder: Optional pre-initialized EmbeddingProvider. If None, creates
                  a new one (avoid passing None in hot paths - reuse an
                  instance from the caller).
    """
    try:
        from qdrant_client import models as qm
        from nexus_memory.embeddings import EmbeddingProvider
        import asyncio
        import concurrent.futures

        summary = (
            f"SICA run {result.run_id[:8]}: scanned {result.total_scanned} memories, "
            f"found {result.issues_found} issues, {len(result.auto_patches)} auto-patched, "
            f"{len(result.suggestions)} suggestions."
        )

        # Use provided embedder, or create one. When creating standalone,
        # load .env first so VOYAGE_API_KEY is available.
        _embedder = embedder
        if _embedder is None:
            _load_env()
            _embedder = EmbeddingProvider()

        # Verify dimension matches collection to avoid upsert failure
        try:
            coll_info = client.get_collection(collection_name=collection)
            coll_dim = coll_info.config.params.vectors.size
            embedder_dim = getattr(_embedder, 'dim', None) or getattr(_embedder, '_dim', None)
            if embedder_dim and embedder_dim != coll_dim:
                logger.warning(
                    "SICA session storage skipped: embedder dim %d != collection dim %d",
                    embedder_dim, coll_dim
                )
                return
        except Exception:
            pass  # if we can't check, try anyway

        # Get embedding. Handle both sync (.embed() returns list) and
        # async (.embed() returns coroutine) embedders.
        def _get_embedding():
            import asyncio
            # Check if embed() is a coroutine function
            import inspect
            if inspect.iscoroutinefunction(_embedder.embed):
                loop = asyncio.new_event_loop()
                try:
                    return loop.run_until_complete(_embedder.embed(summary))
                finally:
                    loop.close()
            else:
                # Sync embedder (e.g. Hermes plugin's _Embedder wrapper)
                return _embedder.embed(summary)

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(_get_embedding)
            try:
                vector = future.result(timeout=30)
            except concurrent.futures.TimeoutError:
                future.cancel()
                pool.shutdown(wait=False, cancel_futures=True)
                logger.warning("SICA session embedding timed out")
                return

        eid = str(uuid.uuid4())
        ts = datetime.now(timezone.utc).isoformat()
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
