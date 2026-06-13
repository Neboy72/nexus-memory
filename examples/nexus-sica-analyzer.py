#!/usr/bin/env python3
"""
SICA — Self-Improving Coding Agent Cycle für Nexus Memory.

Liest alle Nexus-Memories mit category "belief", analysiert Drift-Muster
und generiert Verbesserungsvorschläge als Skill-Drafts.

Output: JSON mit Analyse + Vorschlägen nach ~/.hermes/self-improvement/
"""

from __future__ import annotations
import json, os, sys
from pathlib import Path
from datetime import datetime, timezone
from typing import Any

# ━━ Config ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
QDRANT_HOST = os.environ.get("QDRANT_HOST", "localhost")
QDRANT_PORT = int(os.environ.get("QDRANT_PORT", "6333"))
COLLECTION = os.environ.get("NEXUS_COLLECTION", "hermes-memory")
OUTPUT_DIR = Path.home() / ".hermes/self-improvement"
SILENT_THRESHOLD = float(os.environ.get("SICA_SILENT_THRESHOLD", "0.6"))


# ━━ Qdrant Helper ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _scroll_all(host: str, port: int, collection: str,
                filter_cond: dict | None = None) -> list[dict]:
    """Scroll all points from Qdrant with optional filter."""
    import requests as _req

    url = f"http://{host}:{port}/collections/{collection}/points/scroll"
    points: list[dict] = []
    offset: str | None = None

    while True:
        body: dict[str, Any] = {"limit": 100}
        if offset:
            body["offset"] = offset
        if filter_cond:
            body["filter"] = filter_cond

        resp = _req.post(url, json=body, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        batch = data.get("result", {}).get("points", [])
        points.extend(batch)
        offset = data.get("result", {}).get("next_page_offset")
        if not offset:
            break

    return points


def _count_memories(host: str, port: int, collection: str,
                    filter_cond: dict | None = None) -> int:
    """Count points matching a filter."""
    import requests as _req

    url = f"http://{host}:{port}/collections/{collection}/points/count"
    body: dict[str, Any] = {"exact": True}
    if filter_cond:
        body["filter"] = filter_cond

    resp = _req.post(url, json=body, timeout=10)
    resp.raise_for_status()
    return resp.json().get("result", {}).get("count", 0)


# ━━ Analyse ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def analyze() -> dict:
    """Führe SICA-Analyse durch."""
    import requests as _req

    # Gesundheitscheck
    try:
        health = _req.get(
            f"http://{QDRANT_HOST}:{QDRANT_PORT}/collections/{COLLECTION}",
            timeout=5
        )
        status = "ok" if 200 <= health.status_code < 300 else "error"
    except Exception as e:
        return {"status": "error", "error": str(e), "suggestions": []}

    if status != "ok":
        return {"status": "error", "error": f"Qdrant HTTP {health.status_code}", "suggestions": []}

    # Zählungen
    total = _count_memories(QDRANT_HOST, QDRANT_PORT, COLLECTION)
    belief_count = _count_memories(
        QDRANT_HOST, QDRANT_PORT, COLLECTION,
        {"must": [{"key": "category", "match": {"value": "belief"}}]}
    )
    session_count = _count_memories(
        QDRANT_HOST, QDRANT_PORT, COLLECTION,
        {"must": [{"key": "category", "match": {"value": "session"}}]}
    )
    low_conf_count = _count_memories(
        QDRANT_HOST, QDRANT_PORT, COLLECTION,
        {
            "must": [
                {"key": "category", "match": {"value": "belief"}},
                {"key": "provenance.confidence",
                 "range": {"lt": SILENT_THRESHOLD}},
            ]
        }
    )

    # Beliefs mit low confidence abrufen
    low_conf_beliefs = []
    if low_conf_count > 0:
        points = _scroll_all(
            QDRANT_HOST, QDRANT_PORT, COLLECTION,
            {
                "must": [
                    {"key": "category", "match": {"value": "belief"}},
                    {"key": "provenance.confidence",
                     "range": {"lt": SILENT_THRESHOLD}},
                ]
            }
        )
        for p in points[:20]:
            payload = p.get("payload", {})
            low_conf_beliefs.append({
                "id": p.get("id"),
                "content": payload.get("content", "")[:120],
                "confidence": payload.get("provenance", {}).get("confidence", 1.0),
                "timestamp": payload.get("timestamp", ""),
            })

    # Kategorien-Verteilung
    cat_counts = {}
    for cat in ["fact", "belief", "session", "rule", "preference", "temp"]:
        cnt = _count_memories(
            QDRANT_HOST, QDRANT_PORT, COLLECTION,
            {"must": [{"key": "category", "match": {"value": cat}}]}
        )
        if cnt > 0:
            cat_counts[cat] = cnt

    # Suggestions generieren
    suggestions = []

    if low_conf_count > 0:
        suggestions.append({
            "type": "skill_draft",
            "priority": "high" if low_conf_count > 5 else "medium",
            "title": "Belief-Review nach Drift",
            "detail": (
                f"{low_conf_count} Beliefs haben Confidence < {SILENT_THRESHOLD}. "
                "Ein Review-Agent sollte diese Beliefs prüfen und entweder "
                "aktualisieren (-> fact) oder verwerfen (-> temp)."
            ),
            "action": "review_beliefs",
            "affected_ids": [b["id"] for b in low_conf_beliefs],
        })

    if session_count > 3 and belief_count == 0:
        suggestions.append({
            "type": "info",
            "priority": "low",
            "title": "Keine Beliefs trotz Sessions",
            "detail": (
                f"{session_count} Session-Einträge existieren, aber 0 Beliefs. "
                "Beliefs werden aus Session-Facts extrahiert — prüfe ob die "
                "Session-to-Memory Pipeline korrekt kategorisiert."
            ),
            "action": "check_pipeline",
        })

    if low_conf_count == 0 and belief_count > 0:
        suggestions.append({
            "type": "all_clear",
            "priority": "none",
            "title": "Alle Beliefs stabil",
            "detail": (
                f"Alle {belief_count} Beliefs haben Confidence >= {SILENT_THRESHOLD}. "
                "Kein Review nötig."
            ),
            "action": "none",
        })

    return {
        "status": "ok",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "collection": COLLECTION,
        "stats": {
            "total": total,
            "categories": cat_counts,
            "low_confidence_beliefs": low_conf_count,
        },
        "low_confidence_beliefs": low_conf_beliefs[:5],
        "suggestions": suggestions,
    }


# ━━ Main ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

if __name__ == "__main__":
    report = analyze()

    # Output dir anlegen
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # Write report
    report_path = OUTPUT_DIR / "latest-analysis.json"
    report_path.write_text(json.dumps(report, indent=2, default=str))

    # Write suggestions as individual markdown files
    for s in report.get("suggestions", []):
        if s["action"] == "none":
            continue
        safe_title = s["title"].lower().replace(" ", "-")
        safe_title = safe_title.replace("ä", "ae").replace("ö", "oe").replace("ü", "ue")
        sug_path = OUTPUT_DIR / f"suggestion-{safe_title}.md"
        sug_path.write_text(
            f"# {s['title']}\n\n"
            f"**Priority:** {s['priority']}\n"
            f"**Action:** {s['action']}\n\n"
            f"{s['detail']}\n"
        )

    # Stille wenn keine Aktion nötig — Watchdog-Pattern
    actions_needed = [s for s in report.get("suggestions", []) if s["action"] != "none"]
    if not actions_needed:
        sys.exit(0)

    # Bei Vorschlägen: Output für Cron-Delivery
    print(f"🔔 SICA: {len(actions_needed)} Verbesserungsvorschlag/-vorschläge")
    for s in actions_needed[:3]:
        affected = ""
        if s.get("affected_ids"):
            affected = f" ({len(s['affected_ids'])} Einträge)"
        print(f"  • [{s['priority']}] {s['title']}{affected}")
