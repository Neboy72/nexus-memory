"""SkillGraph schema — Edge dataclass, enums, Qdrant Payload Schema.

v2.2.0: SQLite entfernt. Edges leben direkt in Qdrant-Point-Payloads.
Jeder Fact-Point hat ein ``edges``-Feld (Array von Edge-Objekten).

Design decisions (v2.0.0 review by Miosha, migrated to Qdrant in v2.2.0):
  - relation and status are separate fields (never mixed).
  - edge_id is a UUID primary key.
  - Edges are stored ONCE in the source-fact's payload (not duplicated on target).
  - Incoming edges are found via Qdrant nested-Filter on ``edges[].target_fact_id``.
  - deprecated_at is NULL until the edge is rejected.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional


# ── Enums ──────────────────────────────────────────────────────────────────


class EdgeRelation(str, Enum):
    """Semantic relation between two facts.

    Core: v2.0.0
    Extended: v2.1.0 — added ``references`` (auto-discovered similarity).
    v2.2.0: unchanged.
    """

    SUPERSEDES = "supersedes"        # A replaces B (B is obsolete)
    CONTRADICTS = "contradicts"      # A conflicts with B (semantic opposition)
    SUPPORTS = "supports"            # A reinforces / confirms B
    ALTERNATIVE_TO = "alternative_to"  # A is a viable alternative to B
    DEPENDS_ON = "depends_on"        # A requires B (dependency)
    REFERENCES = "references"        # A is related / similar to B (auto-discovered)


class EdgeStatus(str, Enum):
    """Lifecycle status of an edge.

    Core: active → deprecated | rejected
    Extended: v2.1.0 — ``proposed`` for auto-discovered edges awaiting confirmation.
    v2.2.0: unchanged.
    """
    ACTIVE = "active"
    PROPOSED = "proposed"      # Auto-discovered, needs human confirmation
    DEPRECATED = "deprecated"
    REJECTED = "rejected"


# ── Edge Dataclass ─────────────────────────────────────────────────────────


@dataclass
class Edge:
    """A single directed edge between two facts in the SkillGraph.

    v2.2.0: Stored in Qdrant-Point-Payload as part of the ``edges[]`` array.
    ``metadata`` is a native dict (Qdrant JSON), not a JSON string.
    ``source_fact_id`` is the Qdrant Point ID where this edge lives.
    """

    edge_id: str
    source_fact_id: str               # from-fact (Qdrant Point ID)
    target_fact_id: str               # to-fact
    relation: str                     # EdgeRelation value
    status: str                       # EdgeStatus value
    created_at: str                   # ISO timestamp
    updated_at: str                   # ISO timestamp
    deprecated_at: Optional[str] = None  # set on reject / deprecate
    reason: Optional[str] = None      # why this edge was created or rejected
    metadata: Optional[dict[str, Any]] = None  # native dict (not JSON string)

    @classmethod
    def new(
        cls,
        source_fact_id: str,
        target_fact_id: str,
        relation: str,
        reason: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> "Edge":
        now = datetime.now(timezone.utc).isoformat()
        return cls(
            edge_id=str(uuid.uuid4()),
            source_fact_id=source_fact_id,
            target_fact_id=target_fact_id,
            relation=relation,
            status=EdgeStatus.ACTIVE.value,
            created_at=now,
            updated_at=now,
            reason=reason,
            metadata=metadata,
        )

    def to_payload_entry(self) -> dict[str, Any]:
        """Serialize to a Qdrant-Payload-safe dict (excludes source_fact_id)."""
        return {
            "edge_id": self.edge_id,
            "target_fact_id": self.target_fact_id,
            "relation": self.relation,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "deprecated_at": self.deprecated_at,
            "reason": self.reason,
            "metadata": self.metadata,
        }

    @classmethod
    def from_payload_entry(
        cls,
        entry: dict[str, Any],
        source_fact_id: str,
    ) -> "Edge":
        """Deserialize from a Qdrant-Payload entry."""
        return cls(
            edge_id=entry["edge_id"],
            source_fact_id=source_fact_id,
            target_fact_id=entry["target_fact_id"],
            relation=entry["relation"],
            status=entry.get("status", EdgeStatus.ACTIVE.value),
            created_at=entry.get("created_at", ""),
            updated_at=entry.get("updated_at", ""),
            deprecated_at=entry.get("deprecated_at"),
            reason=entry.get("reason"),
            metadata=entry.get("metadata"),
        )

    # ── Legacy backwards compat ────────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        """Legacy: dict with all fields including source_fact_id."""
        d = self.to_payload_entry()
        d["source_fact_id"] = self.source_fact_id
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Edge":
        """Legacy: reconstruct from dict (for migration / tests)."""
        return cls(
            edge_id=d["edge_id"],
            source_fact_id=d.get("source_fact_id", ""),
            target_fact_id=d["target_fact_id"],
            relation=d["relation"],
            status=d.get("status", EdgeStatus.ACTIVE.value),
            created_at=d.get("created_at", ""),
            updated_at=d.get("updated_at", ""),
            deprecated_at=d.get("deprecated_at"),
            reason=d.get("reason"),
            metadata=d.get("metadata"),
        )


# ── Payload Field Names ────────────────────────────────────────────────────

EDGES_PAYLOAD_KEY = "edges"
"""Qdrant-Payload key under which the edge-array is stored."""
