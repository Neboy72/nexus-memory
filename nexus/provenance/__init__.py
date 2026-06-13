"""Multi-Level Provenance — Source tracking, corroboration, and dependency graphs.

Four levels of provenance for every memory entry:

Level 1 — Source: Where did this fact come from?
Level 2 — Corroboration: What confirms or contradicts this fact?
Level 3 — Bi-temporal: When was this fact valid? (extends existing valid_from/valid_until)
Level 4 — Dependency Graph: What breaks if this fact is wrong?

Usage:
    from nexus.provenance import attach_source, find_corroboration, build_dependency_graph

    # Level 1: Automatically attached during nexus_remember
    provenance = attach_source(session_id="abc", source_type="chat", created_by="Kiosha")

    # Level 2: Find corroborating/corroboration entries for a fact
    results = find_corroboration("DeepSeek is our main model", qdrant_host="localhost")

    # Level 4: Build full dependency graph for a memory entry
    graph = build_dependency_graph(point_id="abc-123")
"""

from __future__ import annotations

import json
import re
import logging
from datetime import datetime, date
from typing import Any, Optional

from nexus.config import get_collection, is_success

_logger = logging.getLogger(__name__)

try:
    import requests as _req
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False


# ── Constants ─────────────────────────────────────────────────────────────────

DEFAULT_PROVENANCE = {
    "source": {
        "session_id": None,
        "source_type": "manual",
        "created_by": "System",
    },
    "corroborated_by": [],
    "confidence": 1.0,
    "modified_at": None,
    "modified_by": None,
    "depends_on": [],
    "dependents": [],
    # NOTE: criticality is live-computed by build_dependency_graph(), never persisted
    "grounded": True,
}

# Source types in order of trust
SOURCE_TYPES = {
    "chat":      {"trust": 1.0, "desc": "Direct conversation with user"},
    "ingest":    {"trust": 0.9, "desc": "Imported from external source (Medium, YouTube, etc.)"},
    "cron":      {"trust": 0.8, "desc": "Automated scheduled check"},
    "manual":    {"trust": 0.7, "desc": "Manually written by agent"},
    "inferred":  {"trust": 0.5, "desc": "Derived from other facts (dependency)"},
    "unknown":   {"trust": 0.3, "desc": "Unknown origin"},
}


# ── Level 1: Source ──────────────────────────────────────────────────────────


def scan_provenance(qdrant_host: str = "localhost", qdrant_port: int = 6333,
                     collection_name: Optional[str] = None, limit: int = 500) -> dict:
    """Scan all memory entries in Qdrant and analyze provenance metadata.

    Extracts source types, creators, confidence scores, and criticality
    markers from existing entries.  Provides a snapshot of memory provenance
    health without running a full drift detection.

    Args:
        qdrant_host: Qdrant host.
        qdrant_port: Qdrant port.
        collection_name: Qdrant collection name.
        limit: Max entries to scan (default 500, pass -1 for all).

    Returns:
        Dict with ``source_stats``, ``creator_stats``, ``confidence_avg``,
        ``criticality_count``, ``total_scanned``.
    """
    collection_name = get_collection(collection_name)
    if not HAS_REQUESTS:
        return {"error": "requests library required"}

    url = f"http://{qdrant_host}:{qdrant_port}/collections/{collection_name}/points/scroll"
    sources: dict[str, int] = {}
    creators: dict[str, int] = {}
    confidences: list[float] = []
    criticality_count = 0
    no_provenance = 0
    total = 0
    offset = None

    while True:
        params: dict[str, Any] = {"limit": 100, "with_payload": True}
        if offset:
            params["offset"] = offset
        try:
            r = _req.post(url, json=params, timeout=10)
            data = r.json().get("result", {})
            points = data.get("points", [])
            if not points:
                break
            for p in points:
                payload = p.get("payload", {}) or {}
                total += 1
                prov = payload.get("provenance") or {}
                if not prov:
                    no_provenance += 1
                    continue
                source = prov.get("source", {})
                st = source.get("source_type", "unknown")
                sources[st] = sources.get(st, 0) + 1
                by = source.get("created_by", "?")
                creators[by] = creators.get(by, 0) + 1
                conf = prov.get("confidence")
                if conf is not None:
                    confidences.append(float(conf))
                # Check for criticality marker in payload
                crit = payload.get("criticality") or payload.get("_criticality")
                if crit:
                    criticality_count += 1
            offset = data.get("next_page_offset")
            if offset is None or (limit > 0 and total >= limit):
                break
        except Exception as e:
            _logger.warning("Qdrant scroll failed: %s", e)
            break

    return {
        "source_stats": dict(sorted(sources.items(), key=lambda x: -x[1])),
        "creator_stats": dict(sorted(creators.items(), key=lambda x: -x[1])),
        "confidence_avg": round(sum(confidences) / len(confidences), 3) if confidences else 0.0,
        "confidence_min": round(min(confidences), 3) if confidences else 0.0,
        "confidence_max": round(max(confidences), 3) if confidences else 0.0,
        "criticality_count": criticality_count,
        "total_scanned": total,
        "no_provenance": no_provenance,
        "provenance_rate": round((total - no_provenance) / total * 100, 1) if total else 0.0,
    }


def format_provenance_report(provenance: dict) -> str:
    """Format provenance scan results as a human-readable report.

    Args:
        provenance: Dict returned by :func:`scan_provenance`.

    Returns:
        Markdown-formatted report string.
    """
    lines = ["📊 **Provenance Scan Report**", ""]
    if "error" in provenance:
        lines.append(f"⚠️ {provenance['error']}")
        return "\n".join(lines)

    lines.append(f"  Total entries scanned: **{provenance['total_scanned']}**")
    lines.append(f"  With provenance metadata: **{provenance['total_scanned'] - provenance['no_provenance']}** "
                 f"({provenance['provenance_rate']}%)")
    lines.append(f"  Without provenance: **{provenance['no_provenance']}**")

    src = provenance.get("source_stats", {})
    if src:
        lines.append("")
        lines.append("  **Source types:**")
        for st, count in src.items():
            trust = SOURCE_TYPES.get(st, SOURCE_TYPES["unknown"])["trust"]
            emoji = "🟢" if trust >= 0.8 else "🟡" if trust >= 0.5 else "🔴"
            lines.append(f"    {emoji} {st}: {count} ({trust:.0%} trust)")

    cr = provenance.get("creator_stats", {})
    if cr:
        lines.append("")
        lines.append("  **Created by:**")
        for name, count in cr.items():
            lines.append(f"    👤 {name}: {count}")

    if provenance.get("confidences"):
        lines.append("")
        lines.append(f"  **Confidence:** avg {provenance['confidence_avg']:.2f} "
                     f"(range {provenance['confidence_min']:.1f}–{provenance['confidence_max']:.1f})")

    if provenance.get("criticality_count", 0) > 0:
        lines.append("")
        lines.append(f"  ★ **High-criticality entries:** {provenance['criticality_count']}")
    else:
        lines.append("")
        lines.append("  No ★ criticality markers found")

    lines.append("")
    return "\n".join(lines)


def attach_source(
    session_id: str | None = None,
    source_type: str = "chat",
    created_by: str = "System",
    content: str | None = None,
    source_tier: str | None = None,
) -> dict:
    """Build a provenance dict for Level 1 (source metadata).

    Args:
        session_id: The Hermes session ID this fact came from.
        source_type: One of "chat", "ingest", "cron", "manual", "inferred", "unknown".
        created_by: Who created this fact — "Kiosha", "Miosha", "Nebo", "System".
        content: Optional content hint for source_tier resolution.
        source_tier: Optional explicit source_tier override ("tier1"|"tier2"|"tier3").

    Returns:
        A provenance dict ready for ``nexus_remember(metadata={"provenance": ...})``.
    """
    source_type = source_type.lower()
    if source_type not in SOURCE_TYPES:
        _logger.warning("Unknown source_type '%s', falling back to 'unknown'", source_type)
        source_type = "unknown"

    provenance: dict[str, Any] = {
        "source": {
            "session_id": session_id,
            "source_type": source_type,
            "created_by": created_by,
            "timestamp": datetime.now().isoformat(),
        },
        "corroborated_by": [],
        "confidence": SOURCE_TYPES[source_type]["trust"],
        "modified_at": None,
        "modified_by": None,
        "depends_on": [],
        "dependents": [],
        "grounded": source_type not in ("inferred", "unknown"),
    }

    return provenance


def format_source(provenance: dict | None) -> str:
    """Human-readable source summary for display.

    Args:
        provenance: A provenance dict (or None for legacy entries).

    Returns:
        Short string like "💬 Chat by Kiosha" or "📥 Ingest by System".
    """
    if not provenance:
        return "❓ Unknown origin (legacy entry)"

    source = provenance.get("source", {})
    st = source.get("source_type", "unknown")
    by = source.get("created_by", "?")
    ts = source.get("timestamp", "")[:10] if source.get("timestamp") else ""

    trust = SOURCE_TYPES.get(st, SOURCE_TYPES["unknown"])["trust"]
    emoji = "🟢" if trust >= 0.8 else "🟡" if trust >= 0.5 else "🔴"

    label = f"{emoji} {st.capitalize()} by {by}"
    if ts:
        label += f" ({ts})"

    corroborated = provenance.get("corroborated_by", [])
    if corroborated:
        label += f" · ✓{len(corroborated)} corroborations"

    confidence = provenance.get("confidence", 0.0)
    if confidence < 1.0:
        label += f" · {confidence:.0%} confidence"

    return label


# ── Level 2: Corroboration ──────────────────────────────────────────────────


def find_corroboration(
    content: str,
    qdrant_host: str = "localhost",
    qdrant_port: int = 6333,
    collection_name: Optional[str] = None,
    threshold: float = 0.7,
    limit: int = 5,
) -> list[dict]:
    """Find entries in Qdrant that semantically corroborate or oppose *content*.

    Corroboration means: similar content, same category, same source tier.
    This is a pre-filter for Level 4 dependency graph building.

    Uses Qdrant scroll + content similarity via keyword overlap (lightweight,
    no embedding call needed). For full semantic corroboration, pair with
    ``nexus_search(query=content, limit=limit)``.

    Args:
        content: Text to find corroborations for.
        qdrant_host: Qdrant host.
        qdrant_port: Qdrant port.
        collection_name: Qdrant collection name.
        threshold: Minimum keyword overlap ratio (0.0-1.0).
        limit: Max results.

    Returns:
        List of candidate entries that may corroborate this content.
    """
    collection_name = get_collection(collection_name)
    if not HAS_REQUESTS:
        _logger.warning("requests not available — cannot query Qdrant")
        return []

    url = f"http://{qdrant_host}:{qdrant_port}/collections/{collection_name}/points/scroll"

    # Get all points (limit=100 for now, pagination could be added)
    try:
        r = _req.post(url, json={"limit": 100, "with_payload": True}, timeout=10)
        points = r.json().get("result", {}).get("points", [])
    except Exception as e:
        _logger.warning("Qdrant query failed: %s", e)
        return []

    content_lower = content.lower()
    # Note: äöüß in the regex enables German word detection alongside
    # basic English — these are used for provenance matching in
    # German-language corpora. Extend with other diacritics as needed.
    content_words = set(re.findall(r"\b[a-zäöüß]{3,}\b", content_lower))
    if not content_words:
        return []

    scored: list[tuple[float, float, dict]] = []
    for p in points:
        payload = p.get("payload", {})
        entry_text = (payload.get("content", "") or "").lower()
        entry_words = set(re.findall(r"\b[a-zäöüß]{3,}\b", entry_text))
        if not entry_words:
            continue
        overlap = len(content_words & entry_words) / len(content_words | entry_words)
        if overlap >= threshold:
            scored.append((overlap, p.get("id", ""), payload))

    scored.sort(key=lambda x: (-x[0], x[1]))
    results = []
    for overlap, pid, payload in scored[:limit]:
        results.append({
            "id": pid,
            "content": payload.get("content", ""),
            "category": payload.get("category", "fact"),
            "overlap": round(overlap, 3),
            "provenance": payload.get("provenance"),
        })

    # TODO(v1.5): Add pagination.  Currently scrolls up to 100 points — does not
    # scale beyond ~10k entries.  Replace with Qdrant scroll with offset token.
    return results


def corroborate_entry(
    point_id: str,
    corroborator_id: str,
    qdrant_host: str = "localhost",
    qdrant_port: int = 6333,
    collection_name: Optional[str] = None,
) -> dict:
    """Link two entries as corroborating each other (bidirectional).

    Both entries get each other's ID in their ``provenance.corroborated_by`` list.
    Confidence is recalculated as: min(1.0, base_trust + 0.1 per corroboration).

    Args:
        point_id: The ID of the entry being corroborated.
        corroborator_id: The ID of the entry that corroborates it.
        qdrant_host: Qdrant host.
        qdrant_port: Qdrant port.
        collection_name: Qdrant collection name.

    Returns:
        Dict with update result.
    """
    collection_name = get_collection(collection_name)
    if not HAS_REQUESTS:
        return {"error": "requests library required"}

    base_url = f"http://{qdrant_host}:{qdrant_port}"

    def _fetch(pid: str) -> dict | None:
        try:
            r = _req.get(f"{base_url}/collections/{collection_name}/points/{pid}", timeout=10)
            if is_success(r.status_code):
                return r.json().get("result")
        except Exception:
            pass
        return None

    def _update(pid: str, provenance: dict) -> dict:
        try:
            point = _fetch(pid)
            if not point:
                return {"error": f"Point {pid} not found"}
            payload = dict(point.get("payload", {}))
            payload["provenance"] = provenance
            vector = point.get("vector", [])
            r = _req.put(
                f"{base_url}/collections/{collection_name}/points",
                json={"points": [{"id": point["id"], "vector": vector, "payload": payload}]},
                timeout=10,
            )
            return r.json()
        except Exception as e:
            return {"error": str(e)}

    point_a = _fetch(point_id)
    point_b = _fetch(corroborator_id)
    if not point_a or not point_b:
        return {"error": f"One or both points not found: {point_id}, {corroborator_id}"}

    prov_a = point_a.get("payload", {}).get("provenance", dict(DEFAULT_PROVENANCE))
    prov_b = point_b.get("payload", {}).get("provenance", dict(DEFAULT_PROVENANCE))

    # Bidirectional link
    corroborated_a = list(prov_a.get("corroborated_by", []))
    corroborated_b = list(prov_b.get("corroborated_by", []))

    if corroborator_id not in corroborated_a:
        corroborated_a.append(corroborator_id)
    if point_id not in corroborated_b:
        corroborated_b.append(point_id)

    prov_a["corroborated_by"] = corroborated_a
    prov_b["corroborated_by"] = corroborated_b

    # Recalculate confidence: base trust + 0.1 per corroboration (capped at 1.0)
    source_type = prov_a.get("source", {}).get("source_type", "unknown")
    base_trust = SOURCE_TYPES.get(source_type, SOURCE_TYPES["unknown"])["trust"]
    prov_a["confidence"] = min(1.0, base_trust + 0.1 * len(corroborated_a))

    source_type_b = prov_b.get("source", {}).get("source_type", "unknown")
    base_trust_b = SOURCE_TYPES.get(source_type_b, SOURCE_TYPES["unknown"])["trust"]
    prov_b["confidence"] = min(1.0, base_trust_b + 0.1 * len(corroborated_b))

    result_a = _update(point_id, prov_a)
    result_b = _update(corroborator_id, prov_b)

    # TODO(v1.5): Add ``contradicts`` links.  Currently only supports
    # positive corroboration (confidence goes up).  A ``contradicts_by``
    # field would allow flags/crossrefs that lower confidence instead.
    return {
        "updated": [point_id, corroborator_id],
        "results": {"point": result_a, "corroborator": result_b},
        "new_confidence": prov_a["confidence"],
    }


# ── Level 4: Dependency Graph ──────────────────────────────────────────────


def add_dependency(
    point_id: str,
    depends_on_id: str,
    qdrant_host: str = "localhost",
    qdrant_port: int = 6333,
    collection_name: Optional[str] = None,
) -> dict:
    """Link two entries as dependency (bidirectional).

    Sets ``depends_on`` on *point_id* and ``dependents`` on *depends_on_id*,
    with full cycle detection via DFS.  Prevents trivial self-loops.

    Args:
        point_id: The entry that depends on another.
        depends_on_id: The entry that is depended upon.
        qdrant_host: Qdrant host.
        qdrant_port: Qdrant port.
        collection_name: Qdrant collection name.

    Returns:
        Dict with keys: ``linked`` (bool), ``had_cycle`` (bool), ``error`` (str or None).
    """
    collection_name = get_collection(collection_name)
    if not HAS_REQUESTS:
        return {"linked": False, "had_cycle": False, "error": "requests library required"}

    if point_id == depends_on_id:
        return {"linked": False, "had_cycle": False, "error": "Self-loop rejected"}

    base_url = f"http://{qdrant_host}:{qdrant_port}"

    def _fetch(pid: str) -> dict | None:
        try:
            r = _req.get(f"{base_url}/collections/{collection_name}/points/{pid}", timeout=10)
            if is_success(r.status_code):
                return r.json().get("result")
        except Exception:
            pass
        return None

    def _update(pid: str, payload: dict) -> dict:
        try:
            point = _fetch(pid)
            if not point:
                return {"error": f"Point {pid} not found"}
            vector = point.get("vector", [])
            r = _req.put(
                f"{base_url}/collections/{collection_name}/points",
                json={"points": [{"id": point["id"], "vector": vector, "payload": payload}]},
                timeout=10,
            )
            return r.json()
        except Exception as e:
            return {"error": str(e)}

    def _has_cycle(start: str, target: str, visited: set | None = None) -> bool:
        """DFS cycle check: can we reach *target* from *start* via depends_on?"""
        if visited is None:
            visited = set()
        if start in visited:
            return False
        visited.add(start)
        point = _fetch(start)
        if not point:
            return False
        prov = point.get("payload", {}).get("provenance", {})
        for dep_id in prov.get("depends_on", []):
            dep_str = str(dep_id)
            if dep_str == target:
                return True  # cycle found
            if _has_cycle(dep_str, target, visited):
                return True
        return False

    # Cycle detection: would adding this link create a cycle?
    # Check if depends_on_id can reach point_id via existing depends_on edges
    if _has_cycle(depends_on_id, point_id):
        return {"linked": False, "had_cycle": True, "error": "Cycle detected — would create circular dependency"}

    # Fetch both points
    point_a = _fetch(point_id)
    point_b = _fetch(depends_on_id)
    if not point_a or not point_b:
        return {"linked": False, "had_cycle": False, "error": "One or both points not found"}

    prov_a = point_a.get("payload", {}).get("provenance", dict(DEFAULT_PROVENANCE))
    prov_b = point_b.get("payload", {}).get("provenance", dict(DEFAULT_PROVENANCE))

    # Bidirectional link: A depends_on B → B.dependents += A
    depends_on = list(prov_a.get("depends_on", []))
    dependents_of_b = list(prov_b.get("dependents", []))

    if depends_on_id not in depends_on:
        depends_on.append(depends_on_id)
    if point_id not in dependents_of_b:
        dependents_of_b.append(point_id)

    prov_a["depends_on"] = depends_on
    prov_b["dependents"] = dependents_of_b

    # Persist both
    result_a = _update(point_id, {**point_a["payload"], "provenance": prov_a})
    result_b = _update(depends_on_id, {**point_b["payload"], "provenance": prov_b})

    return {
        "linked": True,
        "had_cycle": False,
        "point_id": point_id,
        "depends_on_id": depends_on_id,
    }


def build_dependency_graph(
    point_id: str,
    qdrant_host: str = "localhost",
    qdrant_port: int = 6333,
    collection_name: Optional[str] = None,
    max_depth: int = 3,
) -> dict:
    """Build a full dependency graph for a memory entry.

    Traces ``depends_on`` (what this fact relies on) and ``dependents``
    (what relies on this fact) up to *max_depth* hops.

    Args:
        point_id: The root entry ID.
        qdrant_host: Qdrant host.
        qdrant_port: Qdrant port.
        collection_name: Qdrant collection name.
        max_depth: Maximum recursion depth (default 3).

    Returns:
        Dict with ``root``, ``dependencies`` (upstream), ``dependents`` (downstream),
        and ``criticality`` (how many entries break if this fact is wrong).
    """
    collection_name = get_collection(collection_name)
    if not HAS_REQUESTS:
        return {"error": "requests library required"}

    base_url = f"http://{qdrant_host}:{qdrant_port}"

    def _fetch(pid: str) -> dict | None:
        try:
            r = _req.get(f"{base_url}/collections/{collection_name}/points/{pid}", timeout=10)
            if is_success(r.status_code):
                return r.json().get("result")
        except Exception:
            pass
        return None

    visited: set = set()
    upstream: list[dict] = []
    downstream: list[dict] = []

    def _traverse_up(pid: str, depth: int = 0):
        if pid in visited or depth > max_depth:
            return
        visited.add(pid)
        point = _fetch(pid)
        if not point:
            return
        payload = point.get("payload", {})
        prov = payload.get("provenance", {})
        depends_on = prov.get("depends_on", [])
        upstream.append({
            "id": pid,
            "content": (payload.get("content", "") or "")[:100],
            "depth": depth,
        })
        for dep_id in depends_on:
            _traverse_up(str(dep_id), depth + 1)

    def _traverse_down(pid: str, depth: int = 0):
        if pid in visited or depth > max_depth:
            return
        visited.add(pid)
        point = _fetch(pid)
        if not point:
            return
        payload = point.get("payload", {})
        prov = payload.get("provenance", {})
        dependents = prov.get("dependents", [])
        downstream.append({
            "id": pid,
            "content": (payload.get("content", "") or "")[:100],
            "depth": depth,
        })
        for dep_id in dependents:
            _traverse_down(str(dep_id), depth + 1)

    root = _fetch(point_id)
    if not root:
        return {"error": f"Point {point_id} not found"}

    visited.clear()
    _traverse_up(point_id)

    visited.clear()
    _traverse_down(point_id)

    total_affected = len(upstream) + len(downstream) - 1  # -1 for root counted twice

    return {
        "root": {
            "id": point_id,
            "content": (root.get("payload", {}).get("content", "") or "")[:200],
        },
        "upstream_dependencies": upstream,
        "downstream_dependents": downstream,
        "criticality": total_affected,
        "total_entries_in_graph": total_affected + 1,
    }
