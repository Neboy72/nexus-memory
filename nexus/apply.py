"""Nexus Memory - Apply-API + Trust-Recompute (v2.7)

Belief operations:
  - resolve:     Find or create a belief
  - apply:       Apply delta with automatic event creation
  - override:    User sets explicit value (immune to recompute)
  - recompute:   Recompute trust from evidence
  - govern:      Agent contests, user confirms
"""

import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import requests

from nexus.config import is_success
from nexus.events import create_event, ensure_collection as ensure_events_collection

log = logging.getLogger("nexus.apply")

# --- Constants ---
BELIEFS_COLLECTION = "nexus_beliefs"
EVENTS_COLLECTION = "nexus_events"
QDRANT_URL = os.environ.get("QDRANT_URL", "http://localhost:6333")

# Status-Enum als Konstanten
STATUS_ACTIVE = "ACTIVE"
STATUS_CONTESTED = "CONTESTED"
STATUS_RETRACTED = "RETRACTED"
STATUS_SUPERSEDED = "SUPERSEDED"
STATUS_HISTORICAL = "HISTORICAL"

VALID_STATUSES = {STATUS_ACTIVE, STATUS_CONTESTED, STATUS_RETRACTED, STATUS_SUPERSEDED, STATUS_HISTORICAL}

# Single source of truth for trust-recompute comparison epsilon.
# Skip writes when |new - old| < this threshold — avoids noisy re-writes
# from floating-point drift. Used by both recompute_trust() and recompute_all().
TRUST_EPSILON = 0.01


# --- Collection Management ---

def ensure_beliefs_collection() -> bool:
    """Creates nexus_beliefs if not exists (with payload schema)."""
    r = requests.get(f"{QDRANT_URL}/collections/{BELIEFS_COLLECTION}", timeout=10)
    if is_success(r.status_code):
        return True

    payload = {
        "name": BELIEFS_COLLECTION,
        "vectors": {"size": 1024, "distance": "Cosine"},
        "payload_indices": [
            {"field_name": "belief_id", "type": "keyword"},
            {"field_name": "status", "type": "keyword"},
            {"field_name": "source", "type": "keyword"},
            {"field_name": "trust", "type": "float"},
            {"field_name": "valid_from", "type": "datetime"},
            {"field_name": "valid_until", "type": "datetime"},
        ],
    }
    r = requests.put(f"{QDRANT_URL}/collections/{BELIEFS_COLLECTION}", json=payload, timeout=10)
    if is_success(r.status_code):
        log.info(f"✅ Collection '{BELIEFS_COLLECTION}' angelegt")
        return True
    log.error(f"❌ Anlage fehlgeschlagen: {r.status_code}")
    return False


# --- Belief CRUD ---

def _gen_id(fact: str) -> str:
    """Deterministic UUID v5 from fact string - same fact always yields same ID."""
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, fact))


def resolve_belief(
    fact: str,
    source: str = "manual",
    rationale: str = "",
    trust: float = 0.5,
    status: str = STATUS_ACTIVE,
) -> dict:
    """Finds belief by fact string or creates a new one.

    Returns:
        dict mit {belief_id, status, created}
    """
    # Existiert bereits?
    existing = _find_by_fact(fact)
    if existing:
        return {
            "belief_id": existing["belief_id"],
            "status": existing.get("status", STATUS_ACTIVE),
            "created": False,
            "trust": existing.get("trust", trust),
        }

    # Neu anlegen
    belief_id = _gen_id(fact)
    now = datetime.now(timezone.utc).isoformat()
    point = {
        "id": belief_id,
        "vector": [0.0] * 1024,
        "payload": {
            "belief_id": belief_id,
            "content": fact,
            "status": status,
            "trust": trust,
            "source": source,
            "rationale": rationale,
            "valid_from": now,
            "valid_until": None,
            "evidences": [],
            "provenance_trail": [],
            "explicitly_set": False,
        },
    }
    r = requests.put(
        f"{QDRANT_URL}/collections/{BELIEFS_COLLECTION}/points",
        json={"points": [point]},
        timeout=10,
    )
    if not is_success(r.status_code):
        log.error(f"❌ Belief-Erstellung fehlgeschlagen: {r.status_code}")
        return {"error": True, "belief_id": None}

    # Event erzeugen
    create_event(
        belief_id=belief_id,
        event_type="belief_created",
        delta={"fact": fact, "source": source, "trust": trust, "status": status},
        status=status,
    )
    log.info(f"✅ Belief erstellt: {belief_id[:8]} - {fact[:50]}")
    return {"belief_id": belief_id, "status": status, "created": True, "trust": trust}


def apply_delta(belief_id: str, delta: dict) -> dict:
    """Applies changes to a belief and creates an event.

    delta can contain: fact, status, trust, source, rationale
    Respects explicitly_set=True - those fields are not overwritten."""
    # Belief laden
    belief = _get_belief(belief_id)
    if not belief:
        return {"error": True, "message": f"Belief {belief_id[:8]} not found"}

    payload = belief["payload"]
    changed = {}
    overrides = []

    for key, value in delta.items():
        # Override-Schutz
        if payload.get("explicitly_set") and key in ("trust", "status"):
            overrides.append(key)
            continue
        if key not in ("fact", "status", "trust", "source", "rationale"):
            continue
        if payload.get(key) != value:
            payload[key] = value
            changed[key] = value

    if not changed:
        return {"belief_id": belief_id, "changed": False, "overrides": overrides}

    # Update in Qdrant
    r = requests.put(
        f"{QDRANT_URL}/collections/{BELIEFS_COLLECTION}/points",
        json={"points": [belief]},
        timeout=10,
    )
    if not is_success(r.status_code):
        return {"error": True, "message": "Update fehlgeschlagen"}

    # Event-Typ bestimmen
    event_type = "belief_updated"
    if "status" in changed:
        event_type = "status_changed"
    if "trust" in changed and "status" not in changed:
        event_type = "trust_changed"

    create_event(
        belief_id=belief_id,
        event_type=event_type,
        delta=changed,
        status=payload.get("status"),
    )
    log.info(f"✅ Belief {belief_id[:8]} aktualisiert: {list(changed.keys())}")
    return {
        "belief_id": belief_id,
        "changed": True,
        "fields": list(changed.keys()),
        "delta": changed,
        "overrides": overrides,
    }


def user_override(belief_id: str, field: str, value: Any) -> dict:
    """User explicitly sets a value - immune to recompute."""
    belief = _get_belief(belief_id)
    if not belief:
        return {"error": True, "message": "Belief not found"}

    payload = belief["payload"]
    old_value = payload.get(field)
    payload[field] = value
    payload["explicitly_set"] = True

    # In provenance_trail festhalten
    trail = payload.get("provenance_trail", [])
    trail.append(f"user_override:{field}:{value}")
    payload["provenance_trail"] = trail

    # Update
    r = requests.put(
        f"{QDRANT_URL}/collections/{BELIEFS_COLLECTION}/points",
        json={"points": [belief]},
        timeout=10,
    )
    if not is_success(r.status_code):
        return {"error": True, "message": "Override fehlgeschlagen"}

    create_event(
        belief_id=belief_id,
        event_type="user_override",
        delta={field: {"from": old_value, "to": value}},
        status=payload.get("status"),
    )
    log.info(f"🔒 User-Override {belief_id[:8]}: {field} = {value}")
    return {"belief_id": belief_id, "field": field, "old": old_value, "new": value}


# --- Trust-Recompute ---

def recompute_trust(belief_id: str) -> dict:
    """Recomputes trust from evidence.trust_contribution (max-aggregation).

    Respects explicitly_set=True - does not overwrite locked fields.
    """
    belief = _get_belief(belief_id)
    # TODO: Pagination for >100 beliefs
    if not belief:
        return {"error": True, "message": "Belief not found"}

    payload = belief["payload"]

    # Override-Schutz
    if payload.get("explicitly_set"):
        return {
            "belief_id": belief_id,
            "trust": payload.get("trust"),
            "skipped": True,
            "reason": "user_override",
        }

    evidences = payload.get("evidences", [])
    if not evidences:
        return {"belief_id": belief_id, "trust": payload.get("trust"), "skipped": True, "reason": "no_evidence"}

    # max-Aggregation
    contributions = [e.get("trust_contribution", 0.5) for e in evidences if isinstance(e, dict)]
    new_trust = max(contributions) if contributions else payload.get("trust", 0.5)

    old_trust = payload.get("trust", 0.5)
    if abs(new_trust - old_trust) < TRUST_EPSILON:
        return {"belief_id": belief_id, "trust": old_trust, "changed": False}

    payload["trust"] = new_trust

    # Update
    r = requests.put(
        f"{QDRANT_URL}/collections/{BELIEFS_COLLECTION}/points",
        json={"points": [belief]},
        timeout=10,
    )
    if not is_success(r.status_code):
        return {"error": True}

    create_event(
        belief_id=belief_id,
        event_type="trust_changed",
        delta={"trust": {"from": old_trust, "to": new_trust}},
        status=payload.get("status"),
    )
    log.info(f"📊 Trust recompute {belief_id[:8]}: {old_trust} → {new_trust}")
    return {"belief_id": belief_id, "trust": new_trust, "from": old_trust, "changed": True}


def recompute_all() -> dict:
    """Full-scan: recomputes trust for ALL beliefs (batched per page).

    Returns:
        dict with total, changed, skipped, overrides
    """
    stats = {"total": 0, "changed": 0, "skipped": 0, "overrides": 0, "errors": 0}
    limit = 100
    page_offset = None

    while True:
        scroll_params = {"limit": limit, "with_payload": True}
        if page_offset is not None:
            scroll_params["offset"] = page_offset

        r = requests.post(
            f"{QDRANT_URL}/collections/{BELIEFS_COLLECTION}/points/scroll",
            json=scroll_params,
            timeout=30,
        )
        if not is_success(r.status_code):
            log.error(f"❌ Scroll failed: {r.status_code}")
            stats["errors"] += 1
            break

        result = r.json()["result"]
        points = result.get("points", [])
        if not points:
            break

        # Batch-update: collect all points needing changes, then one PUT per page
        batch_updates = []
        for p in points:
            stats["total"] += 1
            bid = p["payload"].get("belief_id", "")
            payload = p["payload"]

            # Check override protection first (avoids N+1 recompute calls)
            if payload.get("explicitly_set"):
                stats["skipped"] += 1
                stats["overrides"] += 1
                continue

            # Recompute trust locally from evidence
            evidences = payload.get("evidences", [])
            if not evidences:
                stats["skipped"] += 1
                continue

            new_trust = max(e.get("trust_contribution", 0.0) for e in evidences)
            old_trust = payload.get("trust", 0.0)

            if abs(new_trust - old_trust) > TRUST_EPSILON:
                batch_updates.append({
                    "id": p["id"],
                    "payload": {"trust": new_trust},
                })
                stats["changed"] += 1

        # Batch PUT — one request per page instead of N individual requests
        if batch_updates:
            r2 = requests.put(
                f"{QDRANT_URL}/collections/{BELIEFS_COLLECTION}/points",
                json={"points": batch_updates},
                timeout=30,
            )
            if r2.status_code not in (200, 201):
                log.error(f"❌ Batch-update failed: {r2.status_code}")
                stats["errors"] += len(batch_updates)

        page_offset = result.get("next_page_offset")
        if page_offset is None:
            break

    log.info(f"📊 Recompute scan done: {stats}")
    return stats


# --- Interne Hilfen ---

def _find_by_fact(fact: str) -> Optional[dict]:
    """Sucht ersten Belief mit passendem fact-String."""
    r = requests.post(
        f"{QDRANT_URL}/collections/{BELIEFS_COLLECTION}/points/scroll",
        json={
            "limit": 5,
            "with_payload": True,
            "filter": {
                "must": [{"key": "content", "match": {"value": fact}}],
            },
        },
        timeout=10,
    )
    if not is_success(r.status_code):
        return None
    points = r.json()["result"]["points"]
    if not points:
        return None
    p = points[0]["payload"]
    return {
        "belief_id": p.get("belief_id"),
        "content": p.get("content"),
        "status": p.get("status"),
        "trust": p.get("trust"),
    }


def _get_belief(belief_id: str) -> Optional[dict]:
    """Loads a full point from nexus_beliefs."""
    r = requests.post(
        f"{QDRANT_URL}/collections/{BELIEFS_COLLECTION}/points/scroll",
        json={
            "limit": 1,
            "with_payload": True,
            "with_vector": True,
            "filter": {
                "must": [{"key": "belief_id", "match": {"value": belief_id}}],
            },
        },
        timeout=10,
    )
    if not is_success(r.status_code):
        return None
    points = r.json()["result"]["points"]
    return points[0] if points else None
