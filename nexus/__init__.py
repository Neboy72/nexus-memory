from __future__ import annotations

"""Nexus Memory — Persistent vector memory for Hermes Agent.

Three layers of intelligence:
- Core: Semantic vector search via Qdrant + multiple embedding backends
- Retrieval: Hybrid BM25 + Vector + Reciprocal Rank Fusion (anti-poisoning)
- Health: Belief drift detection (anti-staleness)

v1.8.0+: Lifecycle Management
- Append-only Fact Versioning with State Machine
- Staging: Pending -> Promote/Deprecate/Rollback
- Canonical Fast-Lookup Collection
- Every status change requires a DecisionEvent

v2.1.0+: Auto-Discovery + Graph Analytics
- AutoDiscovery: Automatic relation detection between canonical facts
  (Qdrant-native O(n·k) Similarity + Heuristic Classification, no LLM)
- GraphAnalytics: Hub scores, isolation scores, knowledge gaps, clusters
- Graph Boost: Fact connectivity boosts Hybrid Search rankings
- REFERENCES relation + PROPOSED edge status
"""

import logging
from datetime import date, datetime
from typing import Any, Optional

from nexus.health import DriftDetector, DriftReport
from nexus.retrieval import HybridRetriever
from nexus.provenance import (
    attach_source,
    find_corroboration,
    corroborate_entry,
    add_dependency,
    build_dependency_graph,
    format_source,
    SOURCE_TYPES,
)

from enum import Enum

class MemoryCategory(str, Enum):
    """Memory scopes — entspricht State-Prefixing aus Agentic Design Patterns (Ch8)."""
    FACT = "fact"          # Dauerhafte, verifizierte Fakten (Default)
    BELIEF = "belief"      # Veränderliche Annahmen (Drift-Detection-Kandidaten)
    SESSION = "session"    # Session-spezifisch (Episodic Memory)
    RULE = "rule"          # Betriebsregeln
    PREFERENCE = "preference"  # User-Präferenzen
    PROCEDURE = "procedure"    # Workflow/prozedurale Memory mit Schritt-Reihenfolge
    TEMP = "temp"          # Temporär, verfällt nach TTL

# -- Lifecycle API (v1.8.0+) --
from nexus.lifecycle import (
    FactVersion,
    FactStatus,
    CanonicalView,
    DecisionEvent,
)
from nexus.staging import (
    create_pending,
    promote,
    deprecate,
    rollback,
    list_pending,
    list_deprecated,
    get_fact_history,
)

# ── Skill Export API (v1.9.0+) ─────────────────────────────────────────────
from nexus.export import (
    export_skill,
    search_knowledge,
    list_topics,
)

# ── SkillGraph Discovery + Analytics API (v2.1.0+) ────────────────────────
from nexus.discovery import AutoDiscovery
from nexus.analytics import GraphAnalytics
from nexus.graph.schema import EdgeRelation, EdgeStatus

from nexus.config import get_collection, is_success

__version__ = "0.4.2"

_logger = logging.getLogger(__name__)


# ── Convenience: update an existing memory in-place ──────────────────────


def nexus_update(
    point_id: str,
    new_content: str | None = None,
    new_metadata: dict | None = None,
    modified_by: str | None = None,
    qdrant_host: str = "localhost",
    qdrant_port: int = 6333,
    collection_name: Optional[str] = None,
) -> dict:
    """Update an existing memory point without losing metadata.

    Unlike forget + remember, this preserves all existing metadata (category,
    source_tier, timestamps, etc.) and only overwrites the fields you specify.

    Automatically tracks ``modified_at`` and ``modified_by`` in the
    provenance dict (Level 3 — Bi-temporal modification tracking).

    Args:
        point_id: The Qdrant point ID to update.
        new_content: New content text (None = keep existing).
        new_metadata: Dict of metadata fields to merge/update (None = keep existing).
        modified_by: Who made this modification (e.g. "Kiosha", "Miosha", "Nebo").
        qdrant_host: Qdrant host.
        qdrant_port: Qdrant port.
        collection_name: Qdrant collection name.

    Returns:
        dict with updated point info.
    """
    import requests as _req

    url = f"http://{qdrant_host}:{qdrant_port}/collections/{collection_name}/points/scroll"
    r = _req.post(url, json={"limit": 1, "with_payload": True,
                              "filter": {"must": [{"key": "id", "match": {"value": point_id}}]}}
                   if isinstance(point_id, str) and len(point_id) > 20
                   else {"limit": 100, "with_payload": True},
                   timeout=10)

    # Find the point
    points = r.json().get("result", {}).get("points", [])
    target = None
    for p in points:
        if str(p.get("id", "")) == str(point_id):
            target = p
            break

    if not target:
        # Try direct point lookup
        r2 = _req.get(
            f"http://{qdrant_host}:{qdrant_port}/collections/{collection_name}/points/{point_id}",
            timeout=10,
        )
        target = r2.json().get("result", None)

    if not target:
        return {"error": f"Point {point_id} not found"}

    payload = target.get("payload", {})
    vector = target.get("vector", None)

    # Merge updates
    if new_content:
        payload["content"] = new_content
    if new_metadata:
        payload.update(new_metadata)

    # Update provenance modification tracking (Level 3)
    now_iso = datetime.now().isoformat()
    prov = payload.get("provenance")
    if prov is None:
        # Legacy entry — create basic provenance
        prov = {
            "source": {"source_type": "manual", "created_by": "System", "timestamp": now_iso},
            "corroborated_by": [],
            "confidence": 0.7,
            "modified_at": now_iso,
            "modified_by": modified_by or "System",
            "depends_on": [],
            "dependents": [],
            "grounded": True,
        }
        payload["provenance"] = prov
    else:
        prov["modified_at"] = now_iso
        if modified_by:
            prov["modified_by"] = modified_by

    # Override point with merged payload
    update_url = f"http://{qdrant_host}:{qdrant_port}/collections/{collection_name}/points"
    update_data = {
        "points": [{
            "id": target["id"],
            "vector": vector if vector else [],
            "payload": payload,
        }]
    }
    r3 = _req.put(update_url, json=update_data, timeout=10)
    return r3.json()


# ── Bi-temporal Metadata ─────────────────────────────────────────────────


def _today_iso() -> str:
    """Return today's date as ISO-8601 string."""
    return date.today().isoformat()


# ── Convenience: store a new memory with bi-temporal metadata ──────────


def nexus_remember(
    content: str,
    category: str = MemoryCategory.FACT.value,
    metadata: dict | None = None,
    valid_from: str | None = None,
    provenance: dict | None = None,
    created_by: str = "System",
    session_id: str | None = None,
    source_type: str = "chat",
    source_url: str | None = None,
    confidence: float | None = None,
    tier: int | str | None = None,
    qdrant_host: str = "localhost",
    qdrant_port: int = 6333,
    collection_name: Optional[str] = None,
    **kwargs: Any,
) -> dict:
    """Store a new memory with bi-temporal metadata and optional provenance.

    Automatically sets ``valid_from`` to today if not provided.  Pass
    ``valid_until`` inside *metadata* (or via keyword) for expiry-aware
    storage.

    If *provenance* is not provided, one is auto-built from *created_by*,
    *session_id*, and *source_type* (Level 1 — Source).  Pass an explicit
    ``provenance`` dict to override (Level 2-4 fields).

    Args:
        content: The memory content text.
        category: Category/scope tag. One of MemoryCategory values.
                  Default ``"fact"``. Use ``"belief"`` for drift-prone
                  assumptions, ``"session"`` for episodic, ``"rule"`` for
                  operating rules, ``"preference"`` for user preferences,
                  ``"temp"`` for ephemeral entries. (State-Prefixing Pattern, Ch8)
        metadata: Additional metadata dict to merge.
        valid_from: ISO-8601 date string. Defaults to today if omitted.
        provenance: Full provenance dict. If None, auto-built from args.
        created_by: Who created this fact (used for auto-provenance).
        session_id: Hermes session ID (used for auto-provenance).
        source_type: Source type hint (used for auto-provenance).
        source_url: Source URL for the memory. Recommended for "ingest"
                    and "cron" source types. (Provenance Pattern, Ch14)
        confidence: Confidence score 0.0–1.0. Overrides the auto-computed
                    confidence from provenance. (Provenance Pattern, Ch14)
        qdrant_host: Qdrant host.
        qdrant_port: Qdrant port.
        collection_name: Qdrant collection name.
        **kwargs: Extra keyword arguments forwarded as metadata fields.

    Returns:
        dict with the Qdrant API upsert response.

    Raises:
        ImportError: If ``requests`` is not available.
        ConnectionError: If Qdrant is unreachable.
    """
    import requests as _req
    from nexus.provenance import attach_source

    # Build payload (category wird nach Validierung korrigiert, siehe unten)
    payload: dict[str, Any] = {
        "content": content,
        "category": category,
        "source_url": source_url,
        "timestamp": datetime.now().isoformat(),
        "valid_from": valid_from or _today_iso(),
        "valid_until": None,
    }
    # Validate category (State-Prefixing Pattern, Ch8)
    if category not in MemoryCategory._value2member_map_:
        _logger.warning("Unknown category '%s' — coercing to 'fact'", category)
        category = MemoryCategory.FACT.value
        payload["category"] = category  # Payload nach Coercion aktualisieren

    # ── Guardrails (Ch18) — Input Validation ───────────────────────────
    if len(content) > 5000:
        _logger.warning(
            "Memory content exceeds 5000 chars (%d chars) — consider splitting",
            len(content),
        )
    # PII-Hinweis: E-Mail oder Telefonnummer im Content?
    import re as _re
    if _re.search(r'[\w.+-]+@[\w-]+\.[\w.-]+', content) and source_type != "chat":
        _logger.info(
            "Potential email address in memory (source=%s) — review if intended: %.40s",
            source_type, content,
        )
    if _re.search(r'\+?\d[\d\s\-().]{7,}', content) and source_type not in ("chat", "session"):
        _logger.info(
            "Potential phone number in memory (source=%s) — review if intended: %.40s",
            source_type, content,
        )

    if not source_url and source_type not in ("chat", "session"):
        _logger.warning(
            "Missing source_url for source_type '%s' — content: %.60s",
            source_type, content,
        )
    if metadata:
        payload.update(metadata)
    for k, v in kwargs.items():
        payload[k] = v
    # Ensure valid_from is always set
    if payload.get("valid_from") is None:
        payload["valid_from"] = _today_iso()

    # Attach provenance (Level 1 — Source)
    if provenance is not None:
        payload["provenance"] = provenance
    elif "provenance" not in payload:
        payload["provenance"] = attach_source(
            session_id=session_id,
            source_type=source_type,
            created_by=created_by,
            content=content,
        )

    # Merge explicit confidence into provenance (if provided)
    if confidence is not None:
        prov = payload.get("provenance", {})
        if isinstance(prov, dict):
            prov["confidence"] = confidence
            payload["provenance"] = prov

    # ── Tiered Enrichment ──────────────────────────────────────────────
    from nexus.enrich import decide_tier, enrich, EnrichmentTier

    resolved_tier = (
        EnrichmentTier.from_str(tier)
        if tier is not None
        else decide_tier(content, category)
    )
    enrich(resolved_tier, payload)

    # Build vector (empty — Qdrant will fail if no vector; caller
    # is expected to have set up an auto-embedding pipeline, or
    # embed beforehand and pass via ``vector`` kwarg).
    vector = payload.pop("vector", None) or []

    # Ensure point has a valid ID (UUID or integer)
    point_id = payload.pop("id", None)
    if point_id is None:
        import uuid
        point_id = str(uuid.uuid4())

    url = f"http://{qdrant_host}:{qdrant_port}/collections/{collection_name}/points"
    data = {"points": [{"id": point_id, "vector": vector, "payload": payload}]}
    r = _req.put(url, json=data, timeout=10)
    return r.json()


# ── Auto-Fix / Consolidation ────────────────────────────────────────────


def nexus_consolidate(
    contradiction_pairs: list[dict],
    dry_run: bool = True,
    qdrant_host: str = "localhost",
    qdrant_port: int = 6333,
    collection_name: Optional[str] = None,
) -> list[dict]:
    """Resolve detected contradictions by marking older entries as historical.

    For each contradiction pair, the **older** entry (determined by
    ``created_at`` / ``timestamp`` in payload) is marked:
      - ``valid_until`` → today's date
      - ``status`` → ``"HISTORICAL"``

    The **newer** entry gets:
      - ``valid_from`` → today's date (if not already set)

    Works via Qdrant HTTP API — same pattern as :func:`nexus_update`.

    Args:
        contradiction_pairs: List of dicts as returned by
            :meth:`DriftDetector.detect_contradictions`.  Each pair must
            contain ``id_a`` and ``id_b`` keys.
        dry_run: If ``True`` (default), simulate the actions without
            modifying Qdrant.
        qdrant_host: Qdrant host.
        qdrant_port: Qdrant port.
        collection_name: Qdrant collection name.

    Returns:
        List of action dicts, for example::

            [
                {
                    "action": "mark_historical",
                    "id": "abc-123",
                    "reason": "Older entry in contradiction pair (id_b=def-456)",
                },
                {
                    "action": "set_valid_from",
                    "id": "def-456",
                    "reason": "Newer entry in contradiction pair — valid_from set to today",
                },
            ]

    Raises:
        ImportError: If ``requests`` is not available.
    """
    import requests as _req

    today = _today_iso()
    actions: list[dict] = []

    for pair in contradiction_pairs:
        id_a = pair.get("id_a", "")
        id_b = pair.get("id_b", "")
        if not id_a or not id_b:
            _logger.warning("Skipping contradiction pair missing id_a/id_b: %s", pair)
            continue

        # Fetch both points to determine timestamps
        def _fetch_point(pid: str) -> dict | None:
            base = f"http://{qdrant_host}:{qdrant_port}"
            # Direct point lookup
            try:
                r = _req.get(
                    f"{base}/collections/{collection_name}/points/{pid}",
                    timeout=10,
                )
                if is_success(r.status_code):
                    result = r.json().get("result")
                    if result:
                        return result
            except Exception:
                pass
            # Fallback: scroll filter
            try:
                r = _req.post(
                    f"{base}/collections/{collection_name}/points/scroll",
                    json={
                        "limit": 1,
                        "with_payload": True,
                        "filter": {
                            "must": [{"key": "id", "match": {"value": pid}}]
                        },
                    },
                    timeout=10,
                )
                points = r.json().get("result", {}).get("points", [])
                return points[0] if points else None
            except Exception:
                return None

        point_a = _fetch_point(id_a)
        point_b = _fetch_point(id_b)

        if not point_a or not point_b:
            _logger.warning(
                "Could not fetch one or both points for contradiction pair: %s, %s",
                id_a,
                id_b,
            )
            continue

        payload_a = point_a.get("payload", {})
        payload_b = point_b.get("payload", {})

        # Determine older vs newer by timestamp
        ts_a = payload_a.get("timestamp", payload_a.get("created_at", ""))
        ts_b = payload_b.get("timestamp", payload_b.get("created_at", ""))

        # If timestamps cannot be resolved, use id ordering as fallback
        if ts_a and ts_b:
            older_id, newer_id = (id_a, id_b) if ts_a < ts_b else (id_b, id_a)
            older_payload, newer_payload = (
                (payload_a, payload_b)
                if ts_a < ts_b
                else (payload_b, payload_a)
            )
        else:
            # Fallback: treat id_a as older (as returned by detection)
            older_id, newer_id = id_a, id_b
            older_payload, newer_payload = payload_a, payload_b

        # ── Action 1: Mark older entry as historical ─────────────────────
        action_older = {
            "action": "mark_historical",
            "id": older_id,
            "reason": (
                f"Older entry in contradiction pair (id_b={newer_id}, "
                f"type={pair.get('type', 'contradiction')})"
            ),
        }
        actions.append(action_older)

        # ── Action 2: Set valid_from on newer entry ─────────────────────
        action_newer = {
            "action": "set_valid_from",
            "id": newer_id,
            "reason": (
                f"Newer entry in contradiction pair (id_a={older_id}) — "
                f"valid_from set to {today}"
            ),
        }
        actions.append(action_newer)

        if not dry_run:
            # Apply older entry changes
            _apply_consolidation(
                older_id,
                {"valid_until": today, "status": "HISTORICAL"},
                qdrant_host,
                qdrant_port,
                collection_name,
            )
            # Apply newer entry changes
            if not newer_payload.get("valid_from"):
                _apply_consolidation(
                    newer_id,
                    {"valid_from": today},
                    qdrant_host,
                    qdrant_port,
                    collection_name,
                )

    return actions


def _apply_consolidation(
    point_id: str,
    metadata_updates: dict,
    qdrant_host: str,
    qdrant_port: int,
    collection_name: str,
) -> dict:
    """Apply metadata updates to a Qdrant point (internal helper).

    Uses the same HTTP API pattern as :func:`nexus_update`.
    """
    import requests as _req

    base = f"http://{qdrant_host}:{qdrant_port}"
    url = f"{base}/collections/{collection_name}/points"

    # Fetch existing point
    point = None
    try:
        r = _req.get(f"{url}/{point_id}", timeout=10)
        if is_success(r.status_code):
            point = r.json().get("result")
    except Exception:
        pass

    if not point:
        # Fallback scroll
        try:
            r = _req.post(
                f"{base}/collections/{collection_name}/points/scroll",
                json={
                    "limit": 1,
                    "with_payload": True,
                    "filter": {
                        "must": [{"key": "id", "match": {"value": point_id}}]
                    },
                },
                timeout=10,
            )
            points = r.json().get("result", {}).get("points", [])
            point = points[0] if points else None
        except Exception:
            pass

    if not point:
        return {"error": f"Point {point_id} not found"}

    payload = dict(point.get("payload", {}))
    payload.update(metadata_updates)
    vector = point.get("vector", [])

    r = _req.put(
        url,
        json={
            "points": [
                {"id": point["id"], "vector": vector, "payload": payload}
            ]
        },
        timeout=10,
    )
    return r.json()


# ── Temporal Querying ──────────────────────────────────────────────────


def nexus_query_valid(
    query: str,
    at_date: str | None = None,
    qdrant_host: str = "localhost",
    qdrant_port: int = 6333,
    collection_name: Optional[str] = None,
    limit: int = 10,
) -> list[dict]:
    """Query memories that are valid at a specific date.

    Filters results to only those whose temporal validity interval
    (``valid_from`` … ``valid_until``) covers the given date.

    Args:
        query: The search query text (used via Qdrant scroll — for
            proper vector search, embed first and use the Qdrant
            search API directly).
        at_date: ISO-8601 date string (e.g. ``"2026-06-01"``).
            Defaults to today.
        qdrant_host: Qdrant host.
        qdrant_port: Qdrant port.
        collection_name: Qdrant collection name.
        limit: Maximum number of results to return.

    Returns:
        List of point dicts with payload that are valid at *at_date*.

    Raises:
        ImportError: If ``requests`` is not available.
    """
    import requests as _req

    target_date = at_date or _today_iso()

    base = f"http://{qdrant_host}:{qdrant_port}"
    all_points = []

    # Scroll all points (simple approach — no vector search)
    offset = None
    while True:
        body: dict = {"limit": 100, "with_payload": True}
        if offset:
            body["offset"] = offset
        r = _req.post(
            f"{base}/collections/{collection_name}/points/scroll",
            json=body,
            timeout=10,
        )
        data = r.json().get("result", {})
        batch = data.get("points", [])
        if not batch:
            break
        all_points.extend(batch)
        offset = data.get("next_page_offset")
        if not offset:
            break

    # Filter by temporal validity
    valid = []
    for p in all_points:
        payload = p.get("payload", {})
        vf = payload.get("valid_from")
        vu = payload.get("valid_until")

        if vf and target_date < vf:
            continue
        if vu and target_date > vu:
            continue

        valid.append(p)
        if len(valid) >= limit:
            break

    return valid


# ── Authority Chain Resolution (v1.5) ────────────────────────────────────


AUTHORITY_CHAIN = [
    (1, "direct_instruction", "Direct instruction — user said it now"),
    (2, "canonical_policy", "Canonical policy — AGENTS.md, Skills, Rules"),
    (3, "project_decision", "Recent project decision — latest memory entry"),
    (4, "long_term_memory", "Long-term memory with source attribution"),
    (5, "retrieval_summary", "Retrieval summary — hybrid search result"),
    (6, "compressed_summary", "Compressed summary — lowest trust"),
]


def _resolve_authority_level(payload: dict) -> int:
    """Determine the authority level (1=highest, 6=lowest) for a memory entry.

    Uses provenance data if available:
    - ``source_type == "direct"`` or category ``instruction`` → 1
    - Category ``policy`` or ``rule`` → 2
    - Recent ``decision`` (source_type == "chat", <7 days) → 3
    - Has provenance with source → 4
    - Has provenance without source → 5
    - No provenance → 6

    Falls back to content and category heuristics.
    """
    prov = payload.get("provenance", {})
    source = prov.get("source", {})
    source_type = source.get("source_type", payload.get("source_type", ""))
    category = payload.get("category", "")
    content = payload.get("content", "")

    # Level 1: Direct instruction
    if source_type == "direct" or category == "instruction":
        return 1

    # Level 2: Canonical policy
    if category in ("policy", "rule", "canonical"):
        return 2
    content_lower = content.lower() if content else ""
    if any(kw in content_lower for kw in ("agentes.md", "skill.md", "canonical", "rule")):
        return 2

    # Level 3: Recent project decision (chat, <7 days)
    if source_type == "chat" and category == "decision":
        ts = source.get("timestamp", payload.get("timestamp", ""))
        if ts:
            from datetime import datetime as dt
            try:
                entry_time = dt.fromisoformat(ts)
                age = (datetime.now() - entry_time).days
                if age < 7:
                    return 3
            except (ValueError, TypeError):
                pass

    # Level 4: Has provenance with source attribution
    if prov and source.get("source_type") and source.get("created_by"):
        return 4

    # Level 5: Has provenance but no clear source
    if prov:
        return 5

    # Level 6: No provenance at all
    return 6


def resolve_authority(
    facts: list[dict],
    prefer_recent: bool = True,
) -> dict:
    """Resolve conflicting facts by authority chain.

    Given a list of fact payloads (from nexus_search, contradiction detection,
    or manual lookup), returns the most authoritative one.

    The authority chain (1=highest, 6=lowest):
        1. direct_instruction
        2. canonical_policy
        3. project_decision
        4. long_term_memory
        5. retrieval_summary
        6. compressed_summary

    Args:
        facts: List of payload dicts (each must have at least ``content``).
        prefer_recent: If True, among equal authority levels, picks the
                       newer entry by timestamp (default True).

    Returns:
        The winning payload dict with ``_authority_level`` and
        ``_authority_reason`` added.
    """
    if not facts:
        return {"content": "", "_authority_level": 0, "_authority_reason": "No facts"}

    scored = []
    for f in facts:
        level = _resolve_authority_level(f)
        ts = f.get("timestamp", f.get("provenance", {}).get("source", {}).get("timestamp", ""))
        scored.append((level, ts, f))

    # Sort: lowest level number = highest authority
    # If same level and prefer_recent: newer timestamp wins
    if prefer_recent:
        # Stable sort: first by timestamp descending (newest first)
        # then ascending by level (stable → same level keeps timestamp order)
        scored.sort(key=lambda x: x[1] if x[1] else "", reverse=True)
        scored.sort(key=lambda x: x[0])
    else:
        scored.sort(key=lambda x: x[0])

    winner = dict(scored[0][2])
    winner["_authority_level"] = scored[0][0]
    level_lookup = {k: v for k, _, v in AUTHORITY_CHAIN}
    level_name = level_lookup.get(scored[0][0], "unknown")
    winner["_authority_reason"] = (
        f"Wins by authority level {scored[0][0]} ({level_name})"
    )

    return winner


def nexus_resolve_conflict(
    facts: list[dict],
    prefer_recent: bool = True,
) -> dict:
    """High-level conflict resolver for two or more conflicting memories.

    Wraps :func:`resolve_authority` with a user-friendly result.

    Args:
        facts: List of payload dicts (at least 2 to resolve).
        prefer_recent: Prefer newer entry at same authority level.

    Returns:
        Dict with winner, runner_up, authority_reason.

    Example::

        >>> nexus_resolve_conflict([fact_a, fact_b])
        {
            "winner": {"content": "Use Flash for routine", ...},
            "runner_up": {"content": "Use Pro for everything", ...},
            "authority_reason": "Wins by authority level 3 (project_decision)",
            "resolved": True,
        }
    """
    if len(facts) < 1:
        return {"resolved": False, "error": "Need at least 1 fact"}

    if len(facts) == 1:
        winner = dict(facts[0])
        winner["_authority_level"] = _resolve_authority_level(facts[0])
        return {"winner": winner, "runner_up": None,
                "authority_reason": "Single fact — no conflict to resolve",
                "resolved": True}

    winner = resolve_authority(facts, prefer_recent=prefer_recent)
    runners = [f for f in facts if f.get("content") != winner.get("content")]
    runner_up = runners[0] if runners else None

    return {
        "winner": winner,
        "runner_up": runner_up,
        "authority_reason": winner.get("_authority_reason", ""),
        "resolved": True,
    }


# ── Hybrid Search (BM25 + Vector + RRF + Tier-Boost) ───────────────────────


def nexus_search_hybrid(
    query: str,
    embed_provider: str | None = None,
    top_k: int = 10,
    qdrant_host: str = "localhost",
    qdrant_port: int = 6333,
    collection_name: Optional[str] = None,
) -> list[dict]:
    """Hybrid search across all memories: BM25 + Vector + RRF + Tier-Boost.

    Supports all 3 embedding backends (auto-detected from config or overridden):

    - ``"voyage"`` (cloud) — uses VOYAGE_API_KEY, best quality
    - ``"sentence-transformers"`` (local) — ``all-MiniLM-L6-v2``, 384d, free
    - ``"ollama"`` (local) — ``nomic-embed-text``, needs Ollama running

    When *embed_provider* is ``None`` (default), tries to detect from
    Hermes config (``config.yaml nexus-memory.embed_provider``), then
    falls back to BM25-only (no vector component).

    Returns a list of dicts ordered by hybrid relevance (RRF score,
    descending). Each result has keys: id, rrf_score, tier, methods,
    text.

    Usage::

        from nexus import nexus_search_hybrid

        results = nexus_search_hybrid(
            "send-gate encoding fix",
            embed_provider="voyage",
            top_k=5,
        )
        for r in results:
            print(f"[{r['tier']}] {r['methods']} — {r['text'][:80]}")
    """
    from nexus.retrieval import HybridRetriever

    retriever = HybridRetriever(
        qdrant_host=qdrant_host,
        qdrant_port=qdrant_port,
        collection_name=collection_name,
    )
    retriever.index_memories()

    # Embed query if a provider is specified or can be detected
    query_vector = None
    resolved_provider = embed_provider
    if resolved_provider is None:
        # Try detecting from Hermes config
        try:
            import os, yaml
            cfg_path = os.path.expanduser("~/.hermes/config.yaml")
            if os.path.exists(cfg_path):
                with open(cfg_path) as f:
                    cfg = yaml.safe_load(f)
                resolved_provider = (
                    cfg.get("nexus-memory", {})
                    .get("embed_provider")
                )
        except Exception:
            pass

    if resolved_provider:
        query_vector = _embed_query(query, resolved_provider)

    return retriever.search_hybrid(query, query_vector=query_vector, top_k=top_k)


def _embed_query(query: str, provider: str) -> list[float] | None:
    """Embed a query string using the specified provider.

    Returns a flat list of floats, or ``None`` if embedding fails.
    """
    provider = provider.strip().lower()

    if provider == "voyage":
        return _embed_voyage(query)
    elif provider == "sentence-transformers":
        return _embed_sentence_transformers(query)
    elif provider == "ollama":
        return _embed_ollama(query)
    return None


def _embed_voyage(query: str) -> list[float] | None:
    """Embed via Voyage AI API."""
    import os, requests

    api_key = os.environ.get("VOYAGE_API_KEY")
    if not api_key:
        # Try .env
        env_path = os.path.expanduser("~/.hermes/.env")
        if os.path.exists(env_path):
            with open(env_path) as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("VOYAGE_API_KEY="):
                        api_key = line.split("=", 1)[1].strip().strip("\"'")
                        break
    if not api_key:
        return None

    try:
        r = requests.post(
            "https://api.voyageai.com/v1/embeddings",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={"input": [query], "model": "voyage-3-large"},
            timeout=10,
        )
        data = r.json()
        return data["data"][0]["embedding"]
    except Exception:
        return None


def _embed_sentence_transformers(query: str) -> list[float] | None:
    """Embed locally via sentence-transformers (all-MiniLM-L6-v2, 384d)."""
    try:
        from sentence_transformers import SentenceTransformer

        model = SentenceTransformer("all-MiniLM-L6-v2")
        vec = model.encode(query)
        return vec.tolist()
    except Exception:
        return None


def _embed_ollama(query: str) -> list[float] | None:
    """Embed via local Ollama (nomic-embed-text)."""
    import requests

    try:
        r = requests.post(
            "http://localhost:11434/api/embeddings",
            json={"model": "nomic-embed-text", "prompt": query},
            timeout=10,
        )
        data = r.json()
        return data.get("embedding")
    except Exception:
        return None


# ── v2.1.0 Convenience Tools ───────────────────────────────────────────────


def nexus_discover(
    categories: list[str] | None = None,
    qdrant_url: str = "http://localhost:6333",
    collection_name: Optional[str] = None,
) -> dict:
    """Run Auto-Discovery: scan facts → find relations → store edges.

    Convenience wrapper around ``AutoDiscovery.discover_all()``.
    EdgeStore is initialized automatically.

    v2.2.0: Qdrant-Payload backing (was SQLite). No more sqlite_path.

    Args:
        categories: Optional — only discover within these categories.
        qdrant_url: Qdrant HTTP URL.
        collection_name: Qdrant collection name.

    Returns:
        Summary dict with stats (total_facts_scanned, candidates_found,
        inserted_active, inserted_proposed, ...).
    """
    from nexus.discovery import AutoDiscovery
    from nexus.graph.store import EdgeStore

    store = EdgeStore(qdrant_url=qdrant_url, collection=collection_name)
    ad = AutoDiscovery(
        store=store,
        qdrant_url=qdrant_url,
        collection=collection_name,
    )
    ad.initialize()
    return ad.discover_all(categories=categories)


def nexus_graph_report(
    qdrant_url: str | None = None,
    collection: str | None = None,
    as_text: bool = False,
) -> dict | str:
    """Generate a comprehensive SkillGraph analytics report.

    Convenience wrapper around ``GraphAnalytics.full_report()``.

    v2.2.0: Qdrant-Payload (was SQLite). No more sqlite_path.

    Args:
        qdrant_url: Qdrant HTTP URL.
        collection: Qdrant collection name.
        as_text: If True, return formatted text instead of dict.

    Returns:
        Dict or formatted text with graph stats, top hubs,
        relation distribution, clusters, knowledge gaps.
    """
    from nexus.graph.graph import SkillGraph
    from nexus.analytics import GraphAnalytics

    sg = SkillGraph(qdrant_url=qdrant_url, collection=collection)
    sg.initialize()
    analytics = GraphAnalytics(sg)
    report = analytics.full_report()

    if as_text:
        return analytics.report_text(report)
    return report


__all__ = [
    "HybridRetriever",
    "DriftDetector",
    "DriftReport",
    "nexus_update",
    "nexus_remember",
    "nexus_consolidate",
    "nexus_query_valid",
    "nexus_resolve_conflict",
    "resolve_authority",
    "AUTHORITY_CHAIN",
    "nexus_search_hybrid",
    "nexus_discover",
    "nexus_graph_report",
]
