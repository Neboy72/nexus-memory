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

log = logging.getLogger("nexus.events")

# --- Constants ---
COLLECTION = "nexus_events"
QDRANT_URL = os.environ.get("QDRANT_URL", "http://localhost:6333")
VECTOR_SIZE = 1024  # 1024d Cosine — matches nexus_beliefs

EVENT_TYPES = [
    "belief_created",
    "belief_updated",
    "trust_changed",
    "status_changed",
    "belief_split",
    "user_override",
]


class EventType(str, Enum):
    CREATED = "belief_created"
    UPDATED = "belief_updated"
    TRUST_CHANGED = "trust_changed"
    STATUS_CHANGED = "status_changed"
    SPLIT = "belief_split"
    OVERRIDE = "user_override"


# --- Collection Management ---

def ensure_collection() -> bool:
    """Creates nexus_events if not exists (indexes created separately later)."""
    r = requests.get(f"{QDRANT_URL}/collections/{COLLECTION}", timeout=10)
    if r.status_code == 200:
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
    if r.status_code != 200:
        log.error(f"❌ Collection-Anlage fehlgeschlagen: {r.status_code} {r.text[:200]}")
        return False

    # Indizes separat anlegen
    indices = [
        ("event_id", "keyword"),
        ("event_type", "keyword"),
        ("belief_id", "keyword"),
        ("status", "keyword"),
    ]
    for field, idx_type in indices:
        idx_payload = {
            "field_name": field,
            "index_schema": {"type": idx_type},
            "wait": True,
        }
        resp = requests.put(
            f"{QDRANT_URL}/collections/{COLLECTION}/index",
            json=idx_payload,
            timeout=10,
        )
        if resp.status_code not in (200, 201):
            log.warning(f"⚠️ Index '{field}' not created: {resp.status_code}")

    log.info(f"✅ Collection '{COLLECTION}' angelegt (1024d Cosine, {len(indices)} Indizes)")
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
    if r.status_code == 200:
        return event_id
    log.error(f"❌ Event-Speicherung fehlgeschlagen: {r.status_code} {r.text[:200]}")
    return None


def _parse_event(p: dict) -> dict:
    """Extracts event data from a Qdrant point."""
    pl = p["payload"]
    delta = {}
    raw = pl.get("delta", "{}")
    if isinstance(raw, str):
        try:
            delta = json.loads(raw)
        except json.JSONDecodeError:
            log.warning(f"⚠️ Korruptes delta-JSON in Event {pl.get('event_id','')[:8]}")
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
    offset: Optional[str] = None
    
    while True:
        params = {
            "limit": limit if not fetch_all else 200,
            "with_payload": True,
            "filter": {"must": [{"key": "belief_id", "match": {"value": belief_id}}]},
        }
        if offset:
            params["offset"] = offset
        
        r = requests.post(
            f"{QDRANT_URL}/collections/{COLLECTION}/points/scroll",
            json=params,
            timeout=10,
        )
        if r.status_code != 200:
            log.error(f"❌ Event query failed: {r.status_code}")
            break
        
        data = r.json()["result"]
        batch = [_parse_event(p) for p in data["points"]]
        all_events.extend(batch)
        
        next_offset = data.get("next_page_offset")
        if not next_offset or not data["points"]:
            break
        offset = str(next_offset)
        
        if not fetch_all and len(all_events) >= limit:
            all_events = all_events[:limit]
            break
    
    all_events.sort(key=lambda e: e.get("event_time", ""))
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
    offset: Optional[str] = None
    
    while True:
        params = {
            "limit": limit if not all_events else 500,
            "with_payload": True,
            "filter": {"must": filters},
        }
        if offset:
            params["offset"] = offset
        
        r = requests.post(
            f"{QDRANT_URL}/collections/{COLLECTION}/points/scroll",
            json=params,
            timeout=10,
        )
        if r.status_code != 200:
            log.error(f"❌ Event query failed: {r.status_code}")
            break
        
        data = r.json()["result"]
        batch = [_parse_event(p) for p in data["points"]]
        all_events.extend(batch)
        
        next_offset = data.get("next_page_offset")
        if not next_offset or not data["points"]:
            break
        offset = str(next_offset)
        
        if len(all_events) >= limit:
            all_events = all_events[:limit]
            break
    
    return all_events


def get_recent_events(limit: int = 20) -> list[dict]:
    """Holt die neuesten Events (systemweit, absteigend)."""
    r = requests.post(
        f"{QDRANT_URL}/collections/{COLLECTION}/points/scroll",
        json={
            "limit": limit,
            "with_payload": True,
        },
        timeout=10,
    )
    if r.status_code != 200:
        return []
    events = [_parse_event(p) for p in r.json()["result"]["points"]]
    events.sort(key=lambda e: e.get("ingested_at", ""), reverse=True)
    return events


def verify_collection() -> dict:
    """Prüft ob Collection existiert und ggf. Indizes aktiv sind."""
    r = requests.get(f"{QDRANT_URL}/collections/{COLLECTION}", timeout=10)
    if r.status_code != 200:
        return {"exists": False, "points": 0, "indexes": 0}
    data = r.json()["result"]
    points = data.get("points_count", 0)
    indexes = len(data.get("payload_schema", {}))
    return {"exists": True, "points": points, "indexes": indexes}
