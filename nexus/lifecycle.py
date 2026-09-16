"""
Nexus Memory Lifecycle — Append-only Fact Versioning + State Machine.

Core data model for v1.8.0+.

Every fact lives through a state machine:
    pending → canonical | deprecated | rolled_back

Key design decisions (verified by Miosha 26.05.2026):
  - supersedes on version_id (not fact_id) — precise chains for replay/audit
  - content_hash locks payload at creation — no silent drift between staging/promote
  - decision_event is mandatory — every status change must have a reason
  - rolled_back creates a NEW revision (never relabels the old one)
  - Append-only: no edits, no deletes, only new versions
  - TTL excluded for deprecated/rolled_back — they survive as history
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional


# ── Status Machine ─────────────────────────────────────────────────────────


class FactStatus(str, Enum):
    """Lifecycle states for a FactVersion."""

    PENDING = "pending"          # In staging, not yet visible in canonical queries
    CANONICAL = "canonical"      # Active, queryable fact
    DEPRECATED = "deprecated"    # No longer trusted, but kept for history
    ROLLED_BACK = "rolled_back"  # Explicitly reverted; creates new canonical version

    @classmethod
    def valid_transitions(cls, current: str, target: str) -> bool:
        """Validate state transitions.

        Rules:
            pending → canonical | deprecated | rolled_back
            canonical → deprecated | rolled_back
            deprecated → (terminal, no transitions)
            rolled_back → (terminal, no transitions)
        """
        valid = {
            cls.PENDING.value: {cls.CANONICAL.value, cls.DEPRECATED.value, cls.ROLLED_BACK.value},
            cls.CANONICAL.value: {cls.DEPRECATED.value, cls.ROLLED_BACK.value},
            cls.DEPRECATED.value: set(),
            cls.ROLLED_BACK.value: set(),
        }
        return target in valid.get(current, set())


# ── Decision Event Types ──────────────────────────────────────────────────


DECISION_PROMOTE = "promote"
DECISION_DEPRECATE = "deprecate"
DECISION_ROLLBACK = "rollback"


# ── Core Data Model ───────────────────────────────────────────────────────


@dataclass
class DecisionEvent:
    """Why this version has the status it has.

    Every status change REQUIRES a decision_event.  No silent transitions.
    """

    type: str                          # promote | deprecate | rollback
    reason: str                        # Human-readable explanation
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    triggered_by: str = "manual"       # manual | drift_detector | ttl_expiry

    def to_dict(self) -> dict:
        return {
            "type": self.type,
            "reason": self.reason,
            "timestamp": self.timestamp,
            "triggered_by": self.triggered_by,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "DecisionEvent":
        return cls(
            type=d.get("type", ""),
            reason=d.get("reason", ""),
            timestamp=d.get("timestamp", ""),
            triggered_by=d.get("triggered_by", "manual"),
        )


@dataclass
class FactVersion:
    """A single version of a fact.

    ``fact_id`` stays constant across the fact's life.
    ``version_id`` is unique per revision — every status change or content
    change creates a new version.
    """

    fact_id: str
    version_id: str
    content: dict[str, Any]
    content_hash: str
    status: str                               # FactStatus value
    supersedes: Optional[str]                 # version_id (NOT fact_id!)
    decision_event: Optional[DecisionEvent]
    ttl: Optional[int] = None                 # days; only for canonical/pending
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    # version_id of the PENDING version this canonical was promoted from.
    # Distinct from ``supersedes`` for UPDATE-promotions (where supersedes
    # points at the previous canonical, not at the staging draft).
    promoted_from: Optional[str] = None

    # ── Factories ──────────────────────────────────────────────────────────

    @classmethod
    def new_pending(
        cls,
        content: dict[str, Any],
        fact_id: Optional[str] = None,
        supersedes: Optional[str] = None,
        ttl: Optional[int] = None,
    ) -> "FactVersion":
        """Create a new pending FactVersion.

        Args:
            content: The payload (content text, metadata, etc.)
            fact_id: Stable identity. Generated if omitted.
            supersedes: version_id this version replaces.
            ttl: Time-to-live in days.
        """
        now = datetime.now(timezone.utc).isoformat()
        content_str = json.dumps(content, sort_keys=True, default=str)
        content_hash = hashlib.sha256(content_str.encode()).hexdigest()

        return cls(
            fact_id=fact_id or str(uuid.uuid4()),
            version_id=str(uuid.uuid4()),
            content=content,
            content_hash=content_hash,
            status=FactStatus.PENDING.value,
            supersedes=supersedes,
            decision_event=None,
            ttl=ttl,
            created_at=now,
            updated_at=now,
        )

    @classmethod
    def promote(
        cls,
        pending_version: "FactVersion",
        reason: str = "Promoted from staging after verification",
        triggered_by: str = "manual",
        supersedes: Optional[str] = None,
    ) -> "FactVersion":
        """Create a CANONICAL version from a PENDING version.

        The content/content_hash are FROZEN from the pending version.
        You cannot change content during promote — content_hash must match.
        """
        if pending_version.status != FactStatus.PENDING.value:
            raise RuntimeError(
                f"Can only promote PENDING versions, got {pending_version.status}"
            )
        if pending_version.content_hash != hashlib.sha256(
            json.dumps(pending_version.content, sort_keys=True, default=str).encode()
        ).hexdigest():
            raise RuntimeError("Content hash mismatch — payload drifted since staging")

        now = datetime.now(timezone.utc).isoformat()
        return cls(
            fact_id=pending_version.fact_id,
            version_id=str(uuid.uuid4()),
            content=pending_version.content,       # Frozen from staging
            content_hash=pending_version.content_hash,
            status=FactStatus.CANONICAL.value,
            supersedes=supersedes or pending_version.version_id,
            decision_event=DecisionEvent(
                type=DECISION_PROMOTE,
                reason=reason,
                timestamp=now,
                triggered_by=triggered_by,
            ),
            ttl=pending_version.ttl,
            created_at=pending_version.created_at,
            updated_at=now,
            promoted_from=pending_version.version_id,
        )

    @classmethod
    def deprecate(
        cls,
        previous_version: "FactVersion",
        reason: str = "Fact is no longer accurate",
        triggered_by: str = "manual",
    ) -> "FactVersion":
        """Mark a CANONICAL or PENDING version as DEPRECATED.

        Creates a new version with status=deprecated. The old version
        is NOT modified — it remains in its current state as historical
        evidence.
        """
        if previous_version.status not in (
            FactStatus.CANONICAL.value, FactStatus.PENDING.value
        ):
            raise RuntimeError(
                f"Can only deprecate CANONICAL or PENDING, got {previous_version.status}"
            )

        now = datetime.now(timezone.utc).isoformat()
        return cls(
            fact_id=previous_version.fact_id,
            version_id=str(uuid.uuid4()),
            content=previous_version.content,
            content_hash=previous_version.content_hash,
            status=FactStatus.DEPRECATED.value,
            supersedes=previous_version.version_id,
            decision_event=DecisionEvent(
                type=DECISION_DEPRECATE,
                reason=reason,
                timestamp=now,
                triggered_by=triggered_by,
            ),
            ttl=None,             # No TTL — must survive as history
            created_at=previous_version.created_at,
            updated_at=now,
        )

    @classmethod
    def rollback(
        cls,
        bad_version: "FactVersion",
        restore_version: "FactVersion",
        reason: str = "Rolled back from erroneous version",
        triggered_by: str = "manual",
    ) -> tuple["FactVersion", "FactVersion"]:
        """Rollback: undo a bad version by restoring a previous good one.

        Creates TWO new versions:
          1. bad_version → rolled_back (new version, status=rolled_back)
          2. restore_version → canonical (new version with restored content)

        Neither the bad_version nor the restore_version are modified
        in-place — it's all append-only.

        Returns:
            (rolled_back_version, restored_canonical_version)

        Raises:
            ValueError: If the two versions belong to different facts. A
                rollback may only revert a version *of the same fact* it
                restores — otherwise the bad fact keeps no canonical entry
                while foreign content is written under the wrong identity
                (silent integrity break in the append-only store).
            ValueError: If either version is already ``rolled_back`` — see
                W32-1 below.
        """
        # H231: identity guard. Without it a caller could pass a version of
        # another fact and the restored canonical would be rebuilt from
        # ``restore_version.fact_id`` — leaving ``bad_version`` without a
        # canonical entry and mislabelling unrelated content.
        if bad_version.fact_id != restore_version.fact_id:
            raise ValueError(
                "rollback requires both versions to belong to the same fact: "
                f"bad_version.fact_id={bad_version.fact_id!r} != "
                f"restore_version.fact_id={restore_version.fact_id!r}"
            )

        # W32-1: status validation. ``promote`` (rejects non-PENDING) and
        # ``deprecate`` (rejects terminal states) both guard their source
        # version; ``rollback`` checked no status at all. An already
        # rolled_back bad_version could therefore be "rolled back" again —
        # emitting a second rolled_back marker for the same version and
        # corrupting the supersedes chain with duplicate decision events —
        # and a rolled_back version could be resurrected as the canonical.
        # Both are terminal, so refuse them here in the same style as the
        # sibling factories.
        if bad_version.status == FactStatus.ROLLED_BACK.value:
            raise ValueError(
                "rollback refuses an already rolled_back bad_version "
                f"(status={bad_version.status!r}): rolled_back is terminal"
            )
        if restore_version.status == FactStatus.ROLLED_BACK.value:
            raise ValueError(
                "rollback refuses to restore a rolled_back restore_version "
                f"(status={restore_version.status!r}) as canonical: "
                "rolled_back is terminal"
            )

        now = datetime.now(timezone.utc).isoformat()

        # 1. Create rolled_back marker for bad version
        rolled_back = cls(
            fact_id=bad_version.fact_id,
            version_id=str(uuid.uuid4()),
            content=bad_version.content,
            content_hash=bad_version.content_hash,
            status=FactStatus.ROLLED_BACK.value,
            supersedes=bad_version.version_id,
            decision_event=DecisionEvent(
                type=DECISION_ROLLBACK,
                reason=reason,
                timestamp=now,
                triggered_by=triggered_by,
            ),
            ttl=None,
            created_at=bad_version.created_at,
            updated_at=now,
        )

        # 2. Restored canonical (points back to restore_version as supersedes)
        restored = cls(
            fact_id=restore_version.fact_id,
            version_id=str(uuid.uuid4()),
            content=restore_version.content,
            content_hash=restore_version.content_hash,
            status=FactStatus.CANONICAL.value,
            supersedes=restore_version.version_id,
            decision_event=DecisionEvent(
                type=DECISION_ROLLBACK,
                reason=f"Restored after rollback: {reason}",
                timestamp=now,
                triggered_by=triggered_by,
            ),
            ttl=restore_version.ttl,
            created_at=restore_version.created_at,
            updated_at=now,
        )

        return rolled_back, restored

    # ── Serialization ──────────────────────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        return {
            "fact_id": self.fact_id,
            "version_id": self.version_id,
            "content": self.content,
            "content_hash": self.content_hash,
            "status": self.status,
            "supersedes": self.supersedes,
            "decision_event": self.decision_event.to_dict() if self.decision_event else None,
            "ttl": self.ttl,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "promoted_from": self.promoted_from,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "FactVersion":
        """Rebuild a FactVersion from its ``to_dict`` form.

        ``fact_id`` and ``version_id`` are the IDENTITY of the object: a
        version without them cannot be addressed, superseded or restored and
        is therefore useless. They must be present — unlike every other field,
        which is optional and degrades to a sane default.

        H232: these were the only two keys read via ``d[...]``, so a legacy or
        partially-written payload (e.g. read back via
        ``staging._get_current_canonical``) aborted with an opaque ``KeyError``
        instead of a diagnosable error.

        Raises:
            ValueError: If ``fact_id`` or ``version_id`` is missing (or None),
                naming the offending field(s). There is deliberately no
                ``.get(...)`` fallback for identity fields.
        """
        fact_id = d.get("fact_id")
        version_id = d.get("version_id")
        missing = [
            name
            for name, value in (("fact_id", fact_id), ("version_id", version_id))
            if value is None
        ]
        if missing:
            raise ValueError(
                "FactVersion.from_dict: missing required identity field(s): "
                + ", ".join(missing)
            )
        return cls(
            fact_id=fact_id,
            version_id=version_id,
            content=d.get("content", {}),
            content_hash=d.get("content_hash", ""),
            status=d.get("status", FactStatus.PENDING.value),
            supersedes=d.get("supersedes"),
            decision_event=DecisionEvent.from_dict(d["decision_event"]) if d.get("decision_event") else None,
            ttl=d.get("ttl"),
            created_at=d.get("created_at", ""),
            updated_at=d.get("updated_at", ""),
            promoted_from=d.get("promoted_from"),
        )

    # ── Helpers ────────────────────────────────────────────────────────────

    def is_queryable(self) -> bool:
        """Canonical facts are the default query target."""
        return self.status == FactStatus.CANONICAL.value

    def is_history(self) -> bool:
        """Deprecated and rolled_back are historical, not queryable by default."""
        return self.status in (FactStatus.DEPRECATED.value, FactStatus.ROLLED_BACK.value)


# ── Canonical View — Fast Access to Current Truth ──────────────────────────


class CanonicalView:
    """In-memory cache of canonical versions per fact_id.

    Keeps the latest canonical version_id + supersedes chain for each
    fact_id, so queries against "what is true right now" stay fast
    without scanning all versions.
    """

    def __init__(self) -> None:
        # fact_id → latest canonical FactVersion
        self._canonical: dict[str, FactVersion] = {}
        # fact_id → list of version_ids (supersedes chain, newest first)
        self._chains: dict[str, list[str]] = {}
        # fact_id → status of the latest version seen. Mirrors the newest-first
        # ordering of _chains; H230: fact_ids_with_status() reads this instead
        # of _canonical, which only ever holds CANONICAL versions.
        self._latest_status: dict[str, str] = {}

    def set(self, version: FactVersion) -> None:
        """Register a version in the canonical view.

        Only CANONICAL versions are stored. PENDING/DEPRECATED/ROLLED_BACK
        are tracked in the supersedes chain. Only HISTORY versions
        (deprecated/rolled_back) that supersede the live canonical evict it;
        a PENDING version leaves the canonical entry untouched.
        """
        fid = version.fact_id
        # H230: track the latest status per fact (same "last set() wins"
        # ordering assumption as the _chains insert below).
        self._latest_status[fid] = version.status
        if version.is_queryable():
            self._canonical[fid] = version
        elif (
            version.is_history()
            and fid in self._canonical
            and self._canonical[fid].version_id == version.supersedes
        ):
            # If this HISTORY version (deprecated/rolled_back) supersedes the
            # current canonical, remove it from the canonical set. A PENDING
            # version must NOT evict the live canonical — it only supersedes
            # once promoted. The caller promotes a new canonical separately.
            del self._canonical[fid]

        # Track supersedes chain
        if fid not in self._chains:
            self._chains[fid] = []
        self._chains[fid].insert(0, version.version_id)

    def get(self, fact_id: str) -> Optional[FactVersion]:
        """Get the current canonical version for a fact."""
        return self._canonical.get(fact_id)

    def chain(self, fact_id: str) -> list[str]:
        """Get the full version chain for a fact (newest first)."""
        return list(self._chains.get(fact_id, []))

    def all_canonical(self) -> list[FactVersion]:
        """Get all current canonical versions."""
        return list(self._canonical.values())

    def fact_ids_with_status(self, status: str) -> list[str]:
        """Get all fact_ids whose latest version has a given status.

        H230: reads ``_latest_status`` (maintained by :meth:`set`) instead of
        ``_canonical``. The old implementation could only match a CANONICAL
        latest version — the ``else`` branch did nothing — so
        ``fact_ids_with_status("deprecated")`` always returned ``[]`` and the
        documented contract was silently violated. It also skipped a fact
        whose cached canonical was followed by a pending revision.
        """
        return [
            fid for fid, st in self._latest_status.items() if st == status
        ]
