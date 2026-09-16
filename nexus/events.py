"""Nexus Memory — Event-API (v2.7)

Bi-temporal event system for belief changes.
Every change generates an event — full audit trail traceability.

6 event types:
  - belief_created:  initial creation of a belief
  - belief_updated:  fields changed (fact, source, rationale)
  - trust_changed:   trust score recomputed
  - status_changed:  status changed (ACTIVE→CONTESTED etc.)
  - belief_split:    belief split into two (new ID)
  - user_override:   user explicitly set a value (immune to recompute)
"""

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

import requests

from nexus.config import is_success

log = logging.getLogger("nexus.events")

# --- Constants ---
COLLECTION = "nexus_events"
QDRANT_URL = os.environ.get("QDRANT_URL", "http://localhost:6333")
VECTOR_SIZE = 1024  # 1024d Cosine — matches nexus_beliefs

class EventType(str, Enum):
    CREATED = "belief_created"
    UPDATED = "belief_updated"
    TRUST_CHANGED = "trust_changed"
    STATUS_CHANGED = "status_changed"
    SPLIT = "belief_split"
    OVERRIDE = "user_override"


# Derived once from the enum — single source of truth.
EVENT_TYPES = [e.value for e in EventType]


# --- Collection Management ---

# Payload indexes for nexus_events (field, schema type).
_INDEX_FIELDS = [
    ("event_id", "keyword"),
    ("event_type", "keyword"),
    ("belief_id", "keyword"),
    ("status", "keyword"),
    # get_events_since() range-filters on ingested_at — without this index
    # the query degrades to a full collection scan (review #45).
    ("ingested_at", "datetime"),
    ("event_time", "datetime"),
]


def _ensure_indexes() -> None:
    """Creates the payload indexes for nexus_events.

    W28-2 (H21): extracted from the create path so the already-exists path can
    run it too — a collection created by an older version had no indexes at
    all, and get_events_since()/get_recent_events() need the ingested_at
    datetime index. Qdrant's PUT /index is idempotent for identical schemas,
    so re-running it is safe.
    """
    for field, idx_type in _INDEX_FIELDS:
        idx_payload = {
            "field_name": field,
            "field_schema": {"type": idx_type},
            "wait": True,
        }
        resp = requests.put(
            f"{QDRANT_URL}/collections/{COLLECTION}/index",
            json=idx_payload,
            timeout=10,
        )
        # 200/201 = created or already present. Anything else is worth a
        # look, but "index already exists" is answered with 200/201 — so a
        # non-success reply is logged at debug level, not as a warning.
        if resp.status_code not in (200, 201):
            log.debug("Index '%s' not created: %s", field, resp.status_code)


def ensure_collection() -> bool:
    """Creates nexus_events if not exists and ensures its payload indexes."""
    r = requests.get(f"{QDRANT_URL}/collections/{COLLECTION}", timeout=10)
    if is_success(r.status_code):
        # W28-2 (H21): an existing collection (e.g. from an older version) may
        # be missing the ingested_at datetime index that get_events_since()
        # and get_recent_events() rely on — ensure them here as well.
        _ensure_indexes()
        return True

    # Create collection without indexes first (Qdrant ignores payload_schema in PUT body)
    payload = {
        "name": COLLECTION,
        "vectors": {
            "size": VECTOR_SIZE,
            "distance": "Cosine",
        },
    }
    r = requests.put(f"{QDRANT_URL}/collections/{COLLECTION}", json=payload, timeout=10)
    if not is_success(r.status_code):
        log.error("❌ Collection-Anlage fehlgeschlagen: %s %s", r.status_code, r.text[:200])
        return False

    # Indizes separat anlegen
    _ensure_indexes()

    log.info(
        "✅ Collection '%s' angelegt (1024d Cosine, %d Indizes)",
        COLLECTION, len(_INDEX_FIELDS),
    )
    return True


# --- Event CRUD ---

def create_event(
    belief_id: str,
    event_type: str,
    delta: Optional[dict] = None,
    status: Optional[str] = None,
    event_time: Optional[str] = None,
) -> Optional[str]:
    """Erzeugt ein Event und speichert es in Qdrant.

    Args:
        belief_id: ID des betroffenen Beliefs
        event_type: Einer der 6 Event-Typen
        delta: Dict mit den geänderten Feldern (z.B. {"trust": 0.8, "status": "ACTIVE"})
        status: Neuer Status des Beliefs nach dem Event
        event_time: ISO-Timestamp des Events (default: jetzt)

    Returns:
        event_id (str) oder None bei Fehler
    """
    # H214: enforce the EVENT_TYPES contract. Previously any string was stored
    # verbatim, silently entering the audit trail and then being invisible to
    # the `event_type` filters in get_events_since() — corrupting the audit
    # guarantee from the module docstring.
    if event_type not in EVENT_TYPES:
        log.error(
            "❌ Ungültiger Event-Typ '%s' (erlaubt: %s) — Event verworfen",
            event_type, ", ".join(EVENT_TYPES),
        )
        return None

    event_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    point = {
        "id": event_id,
        "vector": [0.0] * VECTOR_SIZE,  # Zero-Vector — events have no semantic meaning
        "payload": {
            "event_id": event_id,
            "event_type": event_type,
            "belief_id": belief_id,
            "delta": json.dumps(delta or {}),
            "status": status or "",
            "ingested_at": now,
            "event_time": event_time or now,
        },
    }

    r = requests.put(
        f"{QDRANT_URL}/collections/{COLLECTION}/points",
        json={"points": [point]},
        timeout=10,
    )
    if is_success(r.status_code):
        return event_id
    log.error("❌ Event-Speicherung fehlgeschlagen: %s %s", r.status_code, r.text[:200])
    return None


def _parse_event(p: dict) -> dict:
    """Extracts event data from a Qdrant point.

    Defensive: a point without a ``payload`` key yields an all-``None`` event
    instead of crashing the whole scroll (same spirit as the delta handling
    below and H216).
    """
    pl = p.get("payload") or {}
    delta = {}
    raw = pl.get("delta", "{}")
    if isinstance(raw, str):
        try:
            delta = json.loads(raw)
        except json.JSONDecodeError:
            log.warning("⚠️ Korruptes delta-JSON in Event %s", pl.get('event_id', '')[:8])
            delta = {}
    elif isinstance(raw, dict):
        delta = raw
    return {
        "event_id": pl.get("event_id"),
        "event_type": pl.get("event_type"),
        "belief_id": pl.get("belief_id"),
        "delta": delta,
        "status": pl.get("status"),
        "ingested_at": pl.get("ingested_at"),
        "event_time": pl.get("event_time"),
    }


def get_events(
    belief_id: str,
    limit: int = 50,
    fetch_all: bool = False,
) -> list[dict]:
    """Fetches all events for a belief (chronological order)."""
    all_events: list[dict] = []
    # H215: with order_by the scroll offset is an integer, otherwise a point id.
    offset: Optional[Any] = None

    while True:
        params = {
            "limit": limit if not fetch_all else 200,
            "with_payload": True,
            "filter": {"must": [{"key": "belief_id", "match": {"value": belief_id}}]},
            # H215: Qdrant scroll order is not chronological (events use random
            # UUID point ids). Without a server-side order, the non-fetch_all
            # path truncated the first `limit` raw-scroll points — an arbitrary
            # slice of the belief's history, not the earliest `limit` events.
            "order_by": {"key": "event_time", "direction": "asc"},
        }
        # `is not None`: with order_by the offset is an integer, and 0 is valid.
        if offset is not None:
            params["offset"] = offset

        r = requests.post(
            f"{QDRANT_URL}/collections/{COLLECTION}/points/scroll",
            json=params,
            timeout=10,
        )
        if not is_success(r.status_code):
            log.error("❌ Event query failed: %s", r.status_code)
            break

        data = r.json()["result"]
        batch = [_parse_event(p) for p in data["points"]]
        all_events.extend(batch)

        next_offset = data.get("next_page_offset")
        if next_offset is None or not data["points"]:
            break
        # H215: keep the native type — with order_by Qdrant returns an integer
        # offset (str() would send it back as a string and break pagination).
        offset = next_offset

        if not fetch_all and len(all_events) >= limit:
            all_events = all_events[:limit]
            break
    
    # H216: `or ""` — _parse_event uses pl.get("event_time"), so a payload
    # holding the key with a null value yields None; sorting None against str
    # raised TypeError. Missing keys already defaulted to "".
    all_events.sort(key=lambda e: e.get("event_time") or "")
    return all_events


def get_events_since(
    since: str,
    event_type: Optional[str] = None,
    limit: int = 200,
) -> list[dict]:
    """Fetches all events since a given timestamp (optionally filtered by type).
    Automatically scrolls through all pages for complete results."""
    filters = [{"key": "ingested_at", "range": {"gte": since}}]
    if event_type:
        filters.append({"key": "event_type", "match": {"value": event_type}})
    
    all_events: list[dict] = []
    # H215: with order_by the scroll offset is an integer, otherwise a point id.
    offset: Optional[Any] = None

    # W28-1 (H20): the page size is DECOUPLED from `limit`. When both were the
    # same value, the very first page already satisfied `len(all_events) >=
    # limit`, so the loop stopped after one page even though newer events were
    # still waiting on later pages — the cap must not double as the page size.
    page_size = max(limit, 100)

    while True:
        params = {
            "limit": page_size,
            "with_payload": True,
            "filter": {"must": filters},
            # W28-1 (H65): without order_by Qdrant paginates in raw point-id
            # order (events use random UUIDs), so the `limit` cap truncated an
            # ARBITRARY subset of the matching history. desc on ingested_at
            # (index created in ensure_collection) makes the events kept by
            # the cap the newest ones. Same style as get_recent_events (H217).
            "order_by": {"key": "ingested_at", "direction": "desc"},
        }
        # `is not None`: with order_by the offset is an integer, and 0 is valid.
        if offset is not None:
            params["offset"] = offset

        r = requests.post(
            f"{QDRANT_URL}/collections/{COLLECTION}/points/scroll",
            json=params,
            timeout=10,
        )
        if not is_success(r.status_code):
            log.error("❌ Event query failed: %s", r.status_code)
            break

        data = r.json()["result"]
        batch = [_parse_event(p) for p in data["points"]]
        all_events.extend(batch)

        # W28-1: enforce the cap on EVERY iteration, including the last page —
        # checked before the "no more pages" break, so a final page that pushes
        # us past `limit` is still truncated instead of being returned whole.
        # desc order → the survivors are the newest events.
        if len(all_events) >= limit:
            all_events = all_events[:limit]
            break

        next_offset = data.get("next_page_offset")
        if next_offset is None or not data["points"]:
            break
        # H215: keep the native type — with order_by Qdrant returns an integer
        # offset (str() would send it back as a string and break pagination).
        offset = next_offset

    return all_events


def get_recent_events(limit: int = 20) -> list[dict]:
    """Holt die neuesten Events (systemweit, absteigend)."""
    r = requests.post(
        f"{QDRANT_URL}/collections/{COLLECTION}/points/scroll",
        json={
            "limit": limit,
            "with_payload": True,
            # H217: without order_by the scroll returns `limit` points in
            # arbitrary id order and the result was merely that arbitrary
            # subset sorted — not the `limit` most recent events. The
            # ingested_at datetime index is created in ensure_collection().
            "order_by": {"key": "ingested_at", "direction": "desc"},
        },
        timeout=10,
    )
    if not is_success(r.status_code):
        return []
    events = [_parse_event(p) for p in r.json()["result"]["points"]]
    # H216: `or ""` guards a null ingested_at (payload key present, value None).
    events.sort(key=lambda e: e.get("ingested_at") or "", reverse=True)
    return events


def verify_collection() -> dict:
    """Prüft ob Collection existiert und ggf. Indizes aktiv sind."""
    r = requests.get(f"{QDRANT_URL}/collections/{COLLECTION}", timeout=10)
    if not is_success(r.status_code):
        return {"exists": False, "points": 0, "indexes": 0}
    data = r.json()["result"]
    points = data.get("points_count", 0)
    indexes = len(data.get("payload_schema", {}))
    return {"exists": True, "points": points, "indexes": indexes}
