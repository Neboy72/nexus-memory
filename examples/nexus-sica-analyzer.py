#!/usr/bin/env python3
"""
SICA — Self-Improving Coding Agent Cycle für Nexus Memory.

Liest alle Nexus-Memories mit category "belief", analysiert Drift-Muster
und generiert Verbesserungsvorschläge als Skill-Drafts.

Output: JSON mit Analyse + Vorschlägen nach ~/.hermes/self-improvement/
"""

from __future__ import annotations
import json, logging, os, sys
from pathlib import Path
from datetime import datetime, timezone
from typing import Any

_logger = logging.getLogger(__name__)

# ━━ Config ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# OCR-5 (bug medium): int()/float() at import time crashed the whole cron run
# on a malformed env value (stray char, comma decimal) BEFORE analyze() could
# produce its structured _error_report. Parse defensively: a bad value
# degrades to the default and is recorded as a warning.
def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        _logger.warning("SICA analyzer: invalid %s, using default %d", name, default)
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except ValueError:
        _logger.warning("SICA analyzer: invalid %s, using default %s", name, default)
        return default


QDRANT_HOST = os.environ.get("QDRANT_HOST", "localhost")
QDRANT_PORT = _env_int("QDRANT_PORT", 6333)
COLLECTION = os.environ.get("NEXUS_COLLECTION", "nexus")
OUTPUT_DIR = Path.home() / ".hermes/self-improvement"
SILENT_THRESHOLD = _env_float("SICA_SILENT_THRESHOLD", 0.6)


# ━━ Qdrant Helper ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _scroll_all(host: str, port: int, collection: str,
                filter_cond: dict | None = None) -> list[dict] | None:
    """Scroll all points from Qdrant with optional filter.

    W31-1: a network/HTTP failure must NOT escape as a traceback — the caller
    records it in the report instead. Returns ``None`` on failure.
    """
    import requests as _req

    url = f"http://{host}:{port}/collections/{collection}/points/scroll"
    points: list[dict] = []
    # W40-2: offset is a Qdrant point id (unsigned 64-bit) — the numeric 0 is a
    # legitimate offset, so pagination must test against None, never truthiness.
    offset: Any = None
    # OCR-6 (bug medium): pagination guards — page cap + loop detection (a
    # server repeating the same offset must not spin the cron job forever).
    page_count = 0
    MAX_SCROLL_PAGES = 50
    seen_offsets: set = set()

    while True:
        body: dict[str, Any] = {"limit": 100}
        if offset is not None:
            body["offset"] = offset
        if filter_cond:
            body["filter"] = filter_cond

        # OCR-6 (maintainability low, L538): the try used to wrap the WHOLE
        # pagination loop (request, JSON decode, validation, accumulation), so
        # a genuine logic bug inside the loop was logged as "scroll failed" —
        # a bug indistinguishable from an outage. Guard ONLY the network call
        # and the JSON decode; the shape validation below runs unguarded so a
        # defect there surfaces as itself.
        try:
            resp = _req.post(url, json=body, timeout=15)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:  # W31-1: RequestException/HTTP error → structured result
            _logger.warning("SICA analyzer: scroll failed: %s", exc)
            return None

        # OCR-5 (bug medium): the payload was dereferenced before its shape
        # was validated — a 2xx response with "result": null (or a non-dict
        # result) raised AttributeError inside the guard, and the broad
        # handler below mislabelled it as a network/scroll failure. Validate
        # the shape explicitly: a malformed payload is a DEGRADED scroll
        # (None), a genuine bug stays distinguishable from an outage.
        result = data.get("result") if isinstance(data, dict) else None
        if not isinstance(result, dict):
            _logger.warning("SICA analyzer: unexpected scroll payload: %r", data)
            return None
        batch = result.get("points", [])
        if not isinstance(batch, list):
            _logger.warning("SICA analyzer: unexpected points shape: %r", result)
            return None
        points.extend(batch)
        offset = result.get("next_page_offset")
        if offset is None:
            break

    return points


def _count_memories(host: str, port: int, collection: str,
                    filter_cond: dict | None = None) -> int | None:
    """Count points matching a filter.

    W31-1: guards the POST/``raise_for_status`` — returns ``None`` on failure
    instead of raising out of the cron run.
    """
    import requests as _req

    url = f"http://{host}:{port}/collections/{collection}/points/count"
    body: dict[str, Any] = {"exact": True}
    if filter_cond:
        body["filter"] = filter_cond

    try:
        resp = _req.post(url, json=body, timeout=10)
        resp.raise_for_status()
        # OCR-4: a 2xx with an unexpected shape must NOT read as 0 — result:null
        # crashes the chained .get, a missing/null count is a FAILED count, not
        # "zero points". Degrade to None so callers treat it as unknown.
        # OCR-6 (bug low, L510): resp.json() itself may be a list/str/number —
        # chaining .get directly raised AttributeError that the broad handler
        # below mislabelled as an HTTP failure. Guard the shape first (still
        # returns None, but the failure is a shape problem, not a network one).
        data = resp.json()
        if not isinstance(data, dict):
            _logger.warning("SICA analyzer: count payload is not an object: %r", data)
            return None
        result = data.get("result")
        if not isinstance(result, dict):
            return None
        count = result.get("count")
        return count if isinstance(count, int) else None
    except Exception as exc:  # W31-1
        _logger.warning("SICA analyzer: count failed: %s", exc)
        return None


# ━━ Analyse ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _error_report(error: str) -> dict:
    """Structured error report that a cron watchdog can actually see.

    W31-3: an error report with an EMPTY "suggestions" list made the
    watchdog exit 0 without printing anything — the outage stayed invisible.
    The report therefore carries "status": "error" AND one actionable
    suggestion so cron-delivery fires.
    """
    return {
        "status": "error",
        "error": error,
        "suggestions": [{
            "type": "error",
            "priority": "high",
            "title": "Qdrant nicht erreichbar",
            "detail": (
                f"SICA-Analyse abgebrochen: {error}. "
                "Qdrant erreichbar machen und SICA erneut laufen lassen."
            ),
            "action": "check_qdrant",
        }],
    }


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
        return _error_report(str(e))

    if status != "ok":
        return _error_report(f"Qdrant HTTP {health.status_code}")

    # W31-1: downstream request failures are collected instead of aborting the
    # run — every count that could not be answered stays None and is named
    # in the report's "errors" field.
    # W40-scan (high): the old form degraded a FAILED count to 0, which is
    # indistinguishable from a genuine "no matching points" — the report then
    # reassured the operator ("Alle Beliefs stabil") DURING a Qdrant outage.
    # Branches must distinguish "zero" from "unknown".
    errors: list[str] = []

    def _count(filter_cond: dict | None, label: str) -> int | None:
        cnt = _count_memories(QDRANT_HOST, QDRANT_PORT, COLLECTION, filter_cond)
        if cnt is None:
            errors.append(f"count failed: {label}")
            return None
        return cnt

    # Zählungen
    total = _count(None, "total")
    belief_count = _count(
        {"must": [{"key": "category", "match": {"value": "belief"}}]}, "belief"
    )
    session_count = _count(
        {"must": [{"key": "category", "match": {"value": "session"}}]}, "session"
    )

    low_conf_filter = {
        "must": [
            {"key": "category", "match": {"value": "belief"}},
            {"key": "provenance.confidence",
             "range": {"lt": SILENT_THRESHOLD}},
        ]
    }
    low_conf_count = _count(low_conf_filter, "low_confidence")

    # Beliefs mit low confidence abrufen
    low_conf_beliefs = []
    # W40-2: the previews below stay capped, but the ids are collected for the
    # FULL result — `affected_ids` drives the review agent, so truncating it
    # silently under-reported the real scope (low_conf_count).
    affected_ids: list[Any] = []
    # W40-scan (high): a FAILED scroll must not read as "no ids" either —
    # affected_ids=None marks the degraded state explicitly.
    if low_conf_count is None:
        affected_ids = None  # type: ignore[assignment]
    elif low_conf_count > 0:
        points = _scroll_all(QDRANT_HOST, QDRANT_PORT, COLLECTION, low_conf_filter)
        if points is None:
            errors.append("scroll failed: low_confidence_beliefs")
            affected_ids = None  # type: ignore[assignment]
            points = []
        if points:
            for p in points:
                # OCR-6 (bug low, L548): _scroll_all validates the batch is a
                # list but not each ELEMENT — a scalar/string entry crashed
                # .get with AttributeError that escaped analyze() entirely.
                # Skip non-dict entries defensively.
                if not isinstance(p, dict):
                    _logger.warning("SICA analyzer: non-dict point skipped: %r", p)
                    continue
                payload = p.get("payload") or {}  # W31-2: explicit null → {}
                if not isinstance(payload, dict):
                    payload = {}
                # W31-2: `provenance` may be present but null → `.get` would crash.
                prov = payload.get("provenance") or {}
                if not isinstance(prov, dict):
                    prov = {}
                content = payload.get("content") or ""
                affected_ids.append(p.get("id"))
                if len(low_conf_beliefs) >= 20:
                    continue
                low_conf_beliefs.append({
                    "id": p.get("id"),
                    "content": str(content)[:120],
                    "confidence": prov.get("confidence", 1.0),
                    "timestamp": payload.get("timestamp") or "",
                })

    # Kategorien-Verteilung
    cat_counts = {}
    for cat in ["fact", "belief", "session", "rule", "preference", "temp"]:
        cnt = _count(
            {"must": [{"key": "category", "match": {"value": cat}}]}, f"category:{cat}"
        )
        if cnt is not None and cnt > 0:
            cat_counts[cat] = cnt

    # Suggestions generieren
    suggestions = []

    if low_conf_count is not None and low_conf_count > 0:
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
            "affected_ids": affected_ids,
        })

    # W40-scan: both suggestion branches gate on the count having SUCCEEDED —
    # None (failed count) never claims "keine Beliefs" or "alle stabil".
    if session_count is not None and session_count > 3 and belief_count == 0:
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

    if low_conf_count == 0 and belief_count is not None and belief_count > 0:
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
        # W31-1: partial outages are named here (never silent).
        "errors": errors,
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
    # OCR-4 (bug high): silence required TWO conditions — no actionable
    # suggestion AND no recorded error. A degraded run (failed count/scroll,
    # errors non-empty) with only "all_clear"-style suggestions exited 0 and
    # the partial outage stayed invisible. Errors are always surfaced.
    actions_needed = [s for s in report.get("suggestions", []) if s["action"] != "none"]
    errors_present = bool(report.get("errors"))
    if not actions_needed and not errors_present:
        sys.exit(0)
    if not actions_needed and errors_present:
        print(f"⚠️ SICA: 0 Aktionen, aber {len(report['errors'])} Fehler bei der Analyse (degraded):")
        for e in report["errors"]:
            print(f"  • {e}")
        sys.exit(1)

    # Bei Vorschlägen: Output für Cron-Delivery
    print(f"🔔 SICA: {len(actions_needed)} Verbesserungsvorschlag/-vorschläge")
    # OCR-5 (bug medium): the comment above promises "Errors are always
    # surfaced", but in the MIXED case (suggestions AND errors) only the
    # suggestions were printed and the process exited 0 — a partial outage
    # (counts failed, one review suggestion) stayed invisible to the cron
    # watchdog. Surface the errors here too, degraded exit.
    # OCR-6 (bug medium): the mixed case announced the suggestions in the
    # header and then exited BEFORE printing them — the announced suggestions
    # were invisible. Print the suggestions FIRST, then surface the errors
    # and degrade.
    for s in actions_needed[:3]:
        affected = ""
        if s.get("affected_ids"):
            affected = f" ({len(s['affected_ids'])} Einträge)"
        print(f"  • [{s['priority']}] {s['title']}{affected}")
    if errors_present:
        print(f"⚠️ SICA: zusätzlich {len(report['errors'])} Fehler bei der Analyse (degraded):")
        for e in report["errors"]:
            print(f"  • {e}")
        sys.exit(1)
