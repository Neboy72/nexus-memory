"""
Nexus Memory Staging — Pending Area + Promote/Deprecate/Rollback.

New facts land in staging (status=pending).  From there they can be:
  - **Promoted** → canonical (live, queryable)
  - **Deprecated** → rejected/outdated (historical)
  - **Rolled back** → reverted to previous canonical

All operations are append-only.  History is never rewritten.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

import requests
from nexus.config import get_collection

from nexus.lifecycle import (
    FactStatus,
    FactVersion,
    CanonicalView,
    DecisionEvent,
    DECISION_PROMOTE,
    DECISION_DEPRECATE,
    DECISION_ROLLBACK,
)

_logger = logging.getLogger(__name__)

# ── Collection Layout (v1.8.0+) ────────────────────────────────────────────
#
# We use TWO Qdrant collections:
#
#   hermes-memory          — All versions (append-only, full history)
#   hermes-memory-canonical — Canonical-only view (fast queries)
#
# Every write goes to hermes-memory.  Canonical promotions are ALSO written
# to hermes-memory-canonical for fast lookups.
#
# The canonical view is rebuilt from hermes-memory on DriftDetector runs.
#
# Collection: hermes-memory (v1.8+)
#   payload schema:
#     fact_id: str           # UUID, stable across versions
#     version_id: str        # UUID, unique per revision
#     content: dict           # Payload (text + metadata)
#     content_hash: str       # SHA-256 of content
#     status: str             # pending | canonical | deprecated | rolled_back
#     supersedes: Optional[str]  # version_id
#     decision_event: Optional[dict]
#     ttl: Optional[int]
#     created_at: str
#     updated_at: str


# ── Lazy collection resolution ──────────────────────────────────────────────
# Module-level constants would crash at import time if no default collection is
# configured (DEFAULT_COLLECTION = None). Instead we resolve lazily on first use.
_COLLECTION_ALL_CACHE: Optional[str] = None
_COLLECTION_CANONICAL_CACHE: Optional[str] = None


def _collection_all() -> str:
    global _COLLECTION_ALL_CACHE
    if _COLLECTION_ALL_CACHE is None:
        _COLLECTION_ALL_CACHE = get_collection()
    return _COLLECTION_ALL_CACHE


def _collection_canonical() -> str:
    global _COLLECTION_CANONICAL_CACHE
    if _COLLECTION_CANONICAL_CACHE is None:
        _COLLECTION_CANONICAL_CACHE = _collection_all() + "-canonical"
    return _COLLECTION_CANONICAL_CACHE


# ── Collection Bootstrap (lazy, called on first write) ─────────────────────
_collections_ensured: bool = False


def ensure_collections(
    host: str = "localhost",
    port: int = 6333,
    vector_size: int = 512,
    distance: str = "Cosine",
) -> dict[str, bool]:
    """Ensure required Qdrant collections exist.

    Creates them if missing.  Safe to call repeatedly
    (idempotent — Qdrant returns 200 if already there).

    Must be called at least once before promote() or any
    canonical-collection write.  Auto-called on first create_pending()
    and promote() via ``_auto_ensure_collections()``.
    """
    results: dict[str, bool] = {}
    for name in (_collection_all(), _collection_canonical()):
        # Check if already exists
        url = f"{_qdrant_url(host, port, name)}"
        try:
            r = requests.get(url, timeout=10)
            if r.status_code == 200:
                results[name] = True
                continue
        except requests.RequestException:
            pass

        # Create collection with correct dimension
        create_url = f"http://{host}:{port}/collections/{name}"
        create_data: dict[str, Any] = {
            "vectors": {
                "size": vector_size,
                "distance": distance,
            }
        }
        try:
            r = requests.put(create_url, json=create_data, timeout=30)
            if r.status_code in (200, 201):
                _logger.info("Created Qdrant collection: %s (512D, %s)", name, distance)
                results[name] = True
            else:
                _logger.error(
                    "Failed to create collection %s: %d %s",
                    name, r.status_code, r.text,
                )
                results[name] = False
        except requests.RequestException as exc:
            _logger.error("Failed to create collection %s: %s", name, exc)
            results[name] = False

    return results


def _auto_ensure_collections(
    host: str = "localhost",
    port: int = 6333,
) -> None:
    """Lazy one-shot collection bootstrap.

    Called automatically on first create_pending() / promote().
    Subsequent calls are no-ops.
    """
    global _collections_ensured
    if _collections_ensured:
        return
    results = ensure_collections(host, port)
    if not all(results.values()):
        missing = [k for k, v in results.items() if not v]
        raise RuntimeError(
            f"Failed to ensure Qdrant collections: {missing}. "
            f"Check Qdrant is running on {host}:{port}."
        )
    _collections_ensured = True


def _qdrant_url(host: str, port: int, collection: str) -> str:
    return f"http://{host}:{port}/collections/{collection}"


def _upsert_point(
    version: FactVersion,
    host: str = "localhost",
    port: int = 6333,
) -> str:
    """Write a FactVersion to the append-only collection (hermes-memory).

    Returns the version_id.
    """
    url = f"{_qdrant_url(host, port, _collection_all())}/points?wait=true"
    payload = version.to_dict()
    data = {
        "points": [{
            "id": version.version_id,
            "vector": [0.0] * 512,   # Placeholder - real vectors from embedder
            "payload": payload,
        }]
    }
    r = requests.put(url, json=data, timeout=10)
    r.raise_for_status()
    return version.version_id


def _write_canonical(
    version: FactVersion,
    host: str = "localhost",
    port: int = 6333,
) -> None:
    """Write/overwrite a canonical version in the fast-lookup collection.

    This is a point UPSERT — if the fact_id already exists in the
    canonical collection, it gets overwritten with the new canonical
    version.  This is the ONLY mutable operation in the entire system.
    """
    url = f"{_qdrant_url(host, port, _collection_canonical())}/points?wait=true"
    payload = version.to_dict()
    # Use a minimal placeholder vector (collection requires one)
    data = {
        "points": [{
            "id": version.fact_id,
            "vector": [0.0] * 512,
            "payload": payload,
        }]
    }
    r = requests.put(url, json=data, timeout=10)
    r.raise_for_status()


def _remove_from_canonical(
    fact_id: str,
    host: str = "localhost",
    port: int = 6333,
) -> None:
    """Remove a fact from the canonical collection (during deprecate/rollback)."""
    url = f"{_qdrant_url(host, port, _collection_canonical())}/points/delete"
    data = {
        "filter": {
            "must": [{"key": "fact_id", "match": {"value": fact_id}}]
        }
    }
    r = requests.post(url, json=data, timeout=10)
    r.raise_for_status()


# ── Public API ─────────────────────────────────────────────────────────────


def create_pending(
    content: dict[str, Any],
    fact_id: Optional[str] = None,
    supersedes: Optional[str] = None,
    ttl: Optional[int] = None,
    host: str = "localhost",
    port: int = 6333,
) -> FactVersion:
    """Create a new pending fact in staging.

    The fact is NOT queryable until promoted.  Use ``promote()``
    to move it to canonical.

    Args:
        content: The payload — must contain at least ``content`` key
            (the text) and may include ``category``, ``metadata``, etc.
        fact_id: Stable identity for this fact.  If omitted, auto-generated.
        supersedes: version_id this new version replaces (if any).
        ttl: Time-to-live in days (only relevant after promote).
        host: Qdrant host.
        port: Qdrant port.

    Returns:
        The created FactVersion (with pending status).
    """
    # Auto-ensure collections exist (lazy one-shot)
    _auto_ensure_collections(host, port)

    version = FactVersion.new_pending(
        content=content,
        fact_id=fact_id,
        supersedes=supersedes,
        ttl=ttl,
    )
    version_id = _upsert_point(version, host, port)
    _logger.info("Created pending fact %s (version %s)", version.fact_id, version_id)
    return version


def promote(
    pending: FactVersion,
    reason: str = "Verified and promoted to canonical",
    triggered_by: str = "manual",
    host: str = "localhost",
    port: int = 6333,
) -> FactVersion:
    """Promote a pending fact to canonical (live, queryable).

    Also writes to the canonical-fast-lookup collection.
    The pending version stays in hermes-memory as-is (append-only).

    Args:
        pending: The PENDING FactVersion to promote.
        reason: Why this promotion happened.
        triggered_by: Source of the decision (manual | drift_detector).
        host: Qdrant host.
        port: Qdrant port.

    Returns:
        The new canonical FactVersion.
    """
    # Auto-ensure collections exist (lazy one-shot)
    _auto_ensure_collections(host, port)

    # Fetch current canonical for this fact_id to set supersedes chain
    current_canonical = _get_current_canonical(pending.fact_id, host, port)

    # ── Concurrency Guard ──────────────────────────────────────────────────
    # If pending was built as an UPDATE (has supersedes), verify the
    # canonical hasn't changed since staging.  If it's a NEW fact, verify
    # no canonical exists yet.  This prevents lost-update / fork scenarios.
    if pending.supersedes:
        # This is an update to an existing fact
        if current_canonical is None:
            raise ValueError(
                f"Cannot promote update for fact {pending.fact_id}: "
                f"pending supersedes {pending.supersedes} but no canonical exists"
            )
        if pending.supersedes != current_canonical.version_id:
            raise ValueError(
                f"Concurrency conflict promoting fact {pending.fact_id}: "
                f"pending was built against canonical {pending.supersedes}, "
                f"but current canonical is {current_canonical.version_id}. "
                f"Rebase the pending version on the current canonical and retry."
            )
    else:
        # This is a new fact
        if current_canonical is not None:
            raise ValueError(
                f"Concurrency conflict promoting fact {pending.fact_id}: "
                f"a canonical version already exists ({current_canonical.version_id}). "
                f"Create the pending as an update with supersedes instead."
            )

    canonical = FactVersion.promote(
        pending_version=pending,
        reason=reason,
        triggered_by=triggered_by,
        supersedes=current_canonical.version_id if current_canonical else pending.version_id,
    )

    # Write to both collections
    _upsert_point(canonical, host, port)
    _write_canonical(canonical, host, port)

    _logger.info(
        "Promoted fact %s: %s → canonical (version %s)",
        pending.fact_id, pending.version_id, canonical.version_id,
    )
    return canonical


def deprecate(
    fact_id: str,
    reason: str = "Fact is no longer accurate",
    triggered_by: str = "manual",
    host: str = "localhost",
    port: int = 6333,
) -> Optional[FactVersion]:
    """Deprecate the current canonical version of a fact.

    The deprecated version stays in the full-history collection.
    It is removed from the canonical-fast-lookup collection.
    A new DEPRECATED version is appended to hermes-memory.

    Args:
        fact_id: The fact to deprecate.
        reason: Why this fact is deprecated.
        triggered_by: Source of the decision.
        host: Qdrant host.
        port: Qdrant port.

    Returns:
        The new deprecated FactVersion, or None if fact not found/not canonical.
    """
    current = _get_current_canonical(fact_id, host, port)
    if current is None:
        _logger.warning("Cannot deprecate fact %s: not found or not canonical", fact_id)
        return None

    deprecated_version = FactVersion.deprecate(
        previous_version=current,
        reason=reason,
        triggered_by=triggered_by,
    )

    _upsert_point(deprecated_version, host, port)
    _remove_from_canonical(fact_id, host, port)

    _logger.info("Deprecated fact %s: %s → deprecated", fact_id, current.version_id)
    return deprecated_version


def rollback(
    fact_id: str,
    reason: str = "Rolled back due to erroneous content",
    triggered_by: str = "manual",
    host: str = "localhost",
    port: int = 6333,
) -> Optional[tuple[FactVersion, FactVersion]]:
    """Rollback a fact to its previous canonical version.

    Creates a rolled_back version for the bad fact and promotes the
    previous version back to canonical.

    Args:
        fact_id: The fact to rollback.
        reason: Why the rollback happened.
        triggered_by: Source of the decision.
        host: Qdrant host.
        port: Qdrant port.

    Returns:
        (rolled_back_version, restored_canonical_version) tuple,
        or None if rollback is not possible.
    """
    # Get current canonical (the "bad" version)
    bad_version = _get_current_canonical(fact_id, host, port)
    if bad_version is None:
        _logger.warning("Cannot rollback fact %s: not canonical", fact_id)
        return None

    # Get the previous canonical by following supersedes chain
    previous_version = _get_version(
        bad_version.supersedes, host, port
    ) if bad_version.supersedes else None

    if previous_version is None:
        _logger.warning(
            "Cannot rollback fact %s: no previous version (supersedes=%s)",
            fact_id, bad_version.supersedes,
        )
        return None

    # Find the last canonical version from the chain
    restore_target = _find_last_canonical(previous_version, host, port)

    rolled_back, restored = FactVersion.rollback(
        bad_version=bad_version,
        restore_version=restore_target,
        reason=reason,
        triggered_by=triggered_by,
    )

    # Write all three: rolled_back marker, restored canonical (both collections)
    _upsert_point(rolled_back, host, port)
    _upsert_point(restored, host, port)
    _write_canonical(restored, host, port)

    _logger.info(
        "Rolled back fact %s: %s → rolled_back, restored to version %s",
        fact_id, bad_version.version_id, restored.version_id,
    )
    return rolled_back, restored


# ── Query Helpers ──────────────────────────────────────────────────────────


def _get_current_canonical(
    fact_id: str,
    host: str = "localhost",
    port: int = 6333,
) -> Optional[FactVersion]:
    """Get the latest CANONICAL version of a fact from the fast-lookup collection."""
    url = f"{_qdrant_url(host, port, _collection_canonical())}/points/{fact_id}"
    try:
        r = requests.get(url, timeout=10)
        if r.status_code == 200:
            result = r.json().get("result")
            if result:
                payload = result.get("payload", {})
                if payload.get("status") == FactStatus.CANONICAL.value:
                    return FactVersion.from_dict(payload)
    except requests.RequestException:
        pass
    return None


def _get_version(
    version_id: str,
    host: str = "localhost",
    port: int = 6333,
) -> Optional[FactVersion]:
    """Get a specific version by ID from the full-history collection."""
    url = f"{_qdrant_url(host, port, _collection_all())}/points/{version_id}"
    try:
        r = requests.get(url, timeout=10)
        if r.status_code == 200:
            result = r.json().get("result")
            if result:
                payload = result.get("payload", {})
                return FactVersion.from_dict(payload)
    except requests.RequestException:
        pass
    return None


def _find_last_canonical(
    start: FactVersion,
    host: str = "localhost",
    port: int = 6333,
) -> FactVersion:
    """Walk the supersedes chain backwards to find the last CANONICAL version."""
    current = start
    max_depth = 50
    depth = 0
    while current.status != FactStatus.CANONICAL.value and current.supersedes and depth < max_depth:
        next_version = _get_version(current.supersedes, host, port)
        if next_version is None:
            break
        current = next_version
        depth += 1
    return current


# ── Pending / Staging Queries ──────────────────────────────────────────────


def _get_canonical_supersedes_set(
    host: str = "localhost",
    port: int = 6333,
) -> set[str]:
    """Build a set of version_ids that are superseded by a canonical version.

    Any pending whose version_id appears in this set has already been
    promoted and should be excluded from pending-review queries.
    """
    url = f"{_qdrant_url(host, port, _collection_all())}/points/scroll"
    payload = {
        "limit": 5000,
        "with_payload": True,
        "filter": {
            "must": [{"key": "status", "match": {"value": FactStatus.CANONICAL.value}}]
        },
    }
    try:
        r = requests.post(url, json=payload, timeout=10)
        results = r.json().get("result", {}).get("points", [])
    except Exception:
        return set()

    superseded: set[str] = set()
    for p in results:
        supersedes = p.get("payload", {}).get("supersedes")
        if supersedes:
            superseded.add(supersedes)
    return superseded


def list_pending(
    host: str = "localhost",
    port: int = 6333,
    limit: int = 50,
) -> list[FactVersion]:
    """List all pending facts awaiting review.

    Returns only the LATEST pending version per fact_id.
    Already-promoted pending entries (those whose version_id is
    superseded by a canonical version) are excluded.

    Update-drafts (pending versions WITH a supersedes field) ARE
    included — they represent replacement candidates that need review.
    """
    url = f"{_qdrant_url(host, port, _collection_all())}/points/scroll"
    payload = {
        "limit": limit * 3,  # Fetch extra for dedup
        "with_payload": True,
        "filter": {
            "must": [{"key": "status", "match": {"value": FactStatus.PENDING.value}}]
        },
    }
    r = requests.post(url, json=payload, timeout=10)
    results = r.json().get("result", {}).get("points", [])

    # Get the set of version_ids that have been superseded by a canonical
    already_promoted = _get_canonical_supersedes_set(host, port)

    # Dedup: keep only the LATEST pending version per fact_id
    latest_pending: dict[str, FactVersion] = {}
    for p in results:
        pl = p.get("payload", {})
        fid = pl.get("fact_id", "")
        ver_id = pl.get("version_id", "")
        if not fid or not ver_id:
            continue

        # Skip if this pending has ALREADY been promoted (canonical points to it)
        if ver_id in already_promoted:
            continue

        version = FactVersion.from_dict(pl)
        existing = latest_pending.get(fid)
        if existing is None or version.created_at > existing.created_at:
            latest_pending[fid] = version

    return list(latest_pending.values())


def list_deprecated(
    host: str = "localhost",
    port: int = 6333,
    limit: int = 50,
) -> list[FactVersion]:
    """List all deprecated facts."""
    url = f"{_qdrant_url(host, port, _collection_all())}/points/scroll"
    payload = {
        "limit": limit,
        "with_payload": True,
        "filter": {
            "must": [{"key": "status", "match": {"value": FactStatus.DEPRECATED.value}}]
        },
    }
    r = requests.post(url, json=payload, timeout=10)
    results = r.json().get("result", {}).get("points", [])
    return [FactVersion.from_dict(p["payload"]) for p in results]


def get_fact_history(
    fact_id: str,
    host: str = "localhost",
    port: int = 6333,
    limit: int = 20,
) -> list[FactVersion]:
    """Get all versions of a fact, ordered by recency (newest first)."""
    url = f"{_qdrant_url(host, port, _collection_all())}/points/scroll"
    payload = {
        "limit": limit,
        "with_payload": True,
        "filter": {
            "must": [{"key": "fact_id", "match": {"value": fact_id}}]
        },
    }
    r = requests.post(url, json=payload, timeout=10)
    results = r.json().get("result", {}).get("points", [])
    versions = [FactVersion.from_dict(p["payload"]) for p in results]
    versions.sort(key=lambda v: v.updated_at, reverse=True)
    return versions
