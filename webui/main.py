#!/usr/bin/env python3
"""Nexus Memory Web UI — FastAPI Backend with Live Qdrant Connection"""

import json
import logging
import os
import re
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from qdrant_client import QdrantClient
import uvicorn

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
HERE = Path(__file__).parent
STATIC = HERE / "static"

app = FastAPI(
    title="Nexus Memory",
    description="Universal Memory Layer for AI Agents",
)

app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")

# ---------------------------------------------------------------------------
# Qdrant Connection
# ---------------------------------------------------------------------------
QDRANT_HOST = os.environ.get("NEXUS_QDRANT_HOST", "localhost")
QDRANT_PORT = int(os.environ.get("NEXUS_QDRANT_PORT", "6333"))
QDRANT_COLLECTION = os.environ.get("NEXUS_QDRANT_COLLECTION", "nexus")

qdrant = QdrantClient(host=QDRANT_HOST, port=QDRANT_PORT)

try:
    collection_info = qdrant.get_collection(QDRANT_COLLECTION)
    print(f"  Qdrant: connected -> '{QDRANT_COLLECTION}' ({collection_info.points_count} points)")
except Exception as e:
    print(f"  WARNING: Could not connect to Qdrant at {QDRANT_HOST}:{QDRANT_PORT}: {e}")
    print(f"  Make sure Qdrant is running on port {QDRANT_PORT}.")


# ---------------------------------------------------------------------------
# Category helpers
# ---------------------------------------------------------------------------
CATEGORY_MAP = {
    "pattern": "belief",
    "lesson": "belief",
    "decision": "fact",
    "test": "fact",
}

SOURCE_CATEGORY = {
    "paperless": "fact",
    "wiki": "fact",
    "youtube": "fact",
    "scout": "fact",
}


def _is_session_source(src):
    """Heuristic: session sources look like timestamps or contain 'session'."""
    if not src:
        return False
    if src == "session":
        return True
    # Matches patterns like '20260523_084824_3f1551da', 'session-2026-05-14'
    parts = src.split("_")[0]
    if len(parts) == 8 and parts.isdigit():
        return True
    if "session" in src.lower():
        return True
    return False


# ---------------------------------------------------------------------------
# Load memories from Qdrant
# ---------------------------------------------------------------------------
def _load_memories():
    """Fetch all memories from Qdrant's nexus collection."""
    memories = []
    try:
        next_offset = None
        while True:
            page, next_offset = qdrant.scroll(
                collection_name=QDRANT_COLLECTION,
                limit=1000,
                offset=next_offset,
                with_payload=True,
                with_vectors=False,
            )
            for point in page:
                payload = point.payload or {}
                mem_id = str(point.id)

                # Content: prefer 'content' field, fallback to 'text'
                text = payload.get("content") or payload.get("text") or ""
                if isinstance(text, (dict, list)):
                    text = json.dumps(text, ensure_ascii=False)
                # Truncate very long content for graph display
                if len(text) > 500:
                    text = text[:497] + "..."

                # --- Category resolution: 3-layer fallback ---
                cat = payload.get("category")

                # Layer 1: Normalise non-standard payload categories
                if cat and cat not in ("fact", "belief", "session", "rule", "preference", "temp"):
                    cat = CATEGORY_MAP.get(cat, "fact")

                # Layer 2: Source-based fallback if still no valid category
                if not cat or cat not in ("fact", "belief", "session", "rule", "preference", "temp"):
                    src = payload.get("source", "")
                    if src in SOURCE_CATEGORY:
                        cat = SOURCE_CATEGORY[src]
                    elif _is_session_source(src):
                        cat = "session"
                    else:
                        cat = "fact"

                # --- Confidence ---
                conf = payload.get("confidence")
                if conf is None:
                    conf = 0.5 if "category" not in (payload or {}) else 0.7

                # --- Access level ---
                access = payload.get("access_level", "public")
                if access not in ("public", "trusted", "private"):
                    access = "public"

                # --- Timestamp: prefer Paperless 'created', fallback to Nexus 'created_at' ---
                created_raw = payload.get("created") or payload.get("created_at")
                if created_raw and isinstance(created_raw, str):
                    try:
                        dt = datetime.fromisoformat(created_raw.replace("Z", "+00:00"))
                    except (ValueError, TypeError):
                        dt = datetime.now(timezone.utc)
                else:
                    dt = datetime.now(timezone.utc)

                memories.append({
                    "id": mem_id,
                    "text": text,
                    "category": cat,
                    "title": payload.get("title", ""),
                    "access_level": access,
                    "confidence": conf,
                    "source": payload.get("source", "unknown"),
                    "source_url": payload.get("source_url", ""),
                    "drift": "fresh",
                    "created_at": dt.isoformat(),
                    "updated_at": dt.isoformat(),
                })
            if next_offset is None:
                break
    except Exception as e:
        print(f"  WARNING: Qdrant scroll failed: {e}")
        import traceback
        traceback.print_exc()
    return memories


# ---------------------------------------------------------------------------
# Build edges (connections between related memories)
# ---------------------------------------------------------------------------
def _build_edges(memories):
    """Create edges from shared source or cross-category links."""
    edges = []
    # Connect by source
    source_groups = defaultdict(list)
    for m in memories:
        src = m.get("source", "unknown")
        source_groups[src].append(m["id"])

    for src, ids in source_groups.items():
        if len(ids) > 1:
            for i in range(len(ids) - 1):
                edges.append({"source": ids[i], "target": ids[i + 1], "type": src})

    # Cross-category bridging (for visual variety)
    cat_groups = defaultdict(list)
    for m in memories:
        cat_groups[m["category"]].append(m["id"])

    cat_order = ["fact", "belief", "session", "rule", "preference", "temp"]
    for i in range(len(cat_order) - 1):
        c1 = cat_groups.get(cat_order[i], [])
        c2 = cat_groups.get(cat_order[i + 1], [])
        if c1 and c2:
            edges.append({"source": c1[0], "target": c2[0], "type": "cross-category"})

    return edges


# ---------------------------------------------------------------------------
# In-memory cache (refresh every 60s to stay live)
# ---------------------------------------------------------------------------
_cache = {"memories": None, "edges": None, "ts": 0}


def _get_data():
    now = datetime.now().timestamp()
    if _cache["memories"] is None or now - _cache["ts"] > 60:
        memories = _load_memories()
        edges = _build_edges(memories)
        _cache["memories"] = memories
        _cache["edges"] = edges
        _cache["ts"] = now
    return _cache["memories"], _cache["edges"], _cache["memories"]


# ---------------------------------------------------------------------------
# API Routes
# ---------------------------------------------------------------------------
@app.get("/api/health")
async def health():
    try:
        info = qdrant.get_collection(QDRANT_COLLECTION)
        return {
            "status": "ok",
            "version": "0.2.5",
            "provider": f"Qdrant ({QDRANT_HOST}:{QDRANT_PORT})",
            "collection": QDRANT_COLLECTION,
            "memories": info.points_count,
        }
    except Exception as e:
        return {"status": "error", "detail": str(e)}


@app.get("/api/stats")
async def stats():
    memories, edges, _ = _get_data()
    if not memories:
        return {"total_memories": 0, "total_edges": 0, "by_category": {},
                "by_access_level": {}, "by_drift_status": {"fresh": 0},
                "total_unique_sources": 0, "avg_confidence": 0}

    cat_counts = Counter(m["category"] for m in memories)
    level_counts = Counter(m["access_level"] for m in memories)
    drift_counts = Counter(m.get("drift", "fresh") for m in memories)
    sources = {m["source"] for m in memories}

    return {
        "total_memories": len(memories),
        "total_edges": len(edges),
        "by_category": dict(cat_counts),
        "by_access_level": dict(level_counts),
        "by_drift_status": dict(drift_counts),
        "total_unique_sources": len(sources),
        "avg_confidence": round(sum(m.get("confidence", 0.5) for m in memories) / len(memories), 2),
    }


@app.get("/api/memories")
async def get_memories(
    category: Optional[str] = Query(None),
    access_level: Optional[str] = Query(None),
    drift: Optional[str] = Query(None),
    source: Optional[str] = Query(None),
    limit: int = Query(500, le=2000),
):
    memories, edges, all_memories = _get_data()
    results = list(memories)
    if category and category != "all":
        results = [m for m in results if m["category"] == category]
    if access_level and access_level != "all":
        results = [m for m in results if m["access_level"] == access_level]
    if drift and drift != "all":
        results = [m for m in results if m.get("drift") == drift]
    if source:
        results = [m for m in results if m["source"] == source]

    # Gesamt-Kategorie-Verteilung
    cat_counts = Counter()
    for m in all_memories:
        cat_counts[m["category"]] += 1

    visible_ids = {m["id"] for m in results[:limit]}
    filtered_edges = [e for e in edges if e["source"] in visible_ids and e["target"] in visible_ids]

    return {
        "memories": results[:limit],
        "total": len(results),
        "edges": filtered_edges,
        "category_counts": dict(cat_counts),
    }


@app.get("/api/memories/search")
async def search_memories(q: str = Query(""), limit: int = Query(50, le=100)):
    memories, _, _ = _get_data()
    ql = q.lower()
    results = [
        m for m in memories
        if ql in m["text"].lower() or ql in m.get("category", "") or ql in m.get("source", "")
    ]
    return {
        "query": q,
        "memories": results[:limit],
        "total": len(results),
    }


@app.get("/api/memories/{memory_id}")
async def get_memory(memory_id: str):
    memories, _, _ = _get_data()
    for m in memories:
        if m["id"] == memory_id:
            return m
    return JSONResponse({"error": "Memory not found"}, status_code=404)


# ---------------------------------------------------------------------------

def _qdrant_id(memory_id: str):
    """Qdrant accepts UUIDs or unsigned ints — normalize numeric legacy IDs."""
    mid = (memory_id or "").strip()
    if re.fullmatch(r"\d+", mid):
        return int(mid)
    return memory_id


# Memory Inspector (Astra-Punkt 3): WAS + WARUM + KORRIGIEREN + ZURÜCKROLLEN
# Write-Operationen sind soft-only (lifecycle_status) — never hard delete.
# ---------------------------------------------------------------------------
from pydantic import BaseModel


class TextPatch(BaseModel):
    text: str


@app.patch("/api/memories/{memory_id}/text")
async def patch_memory_text(memory_id: str, body: TextPatch):
    """Edit-in-place: replace the memory's text in its payload."""
    new_text = (body.text or "").strip()
    if not new_text:
        return JSONResponse({"error": "text must not be empty"}, status_code=400)
    try:
        found = qdrant.retrieve(collection_name=QDRANT_COLLECTION, ids=[_qdrant_id(memory_id)], with_payload=True)
        if not found:
            return JSONResponse({"error": "Memory not found"}, status_code=404)
        payload = dict(found[0].payload or {})
        if "content" in payload:
            payload["content"] = new_text
        if "text" in payload or "content" not in payload:
            payload["text"] = new_text
        payload["updated_at"] = datetime.now(timezone.utc).isoformat()
        qdrant.set_payload(collection_name=QDRANT_COLLECTION, payload=payload, points=[_qdrant_id(memory_id)])
        _cache["ts"] = 0  # force reload
        return {"status": "ok", "id": memory_id, "text": new_text[:120]}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/memories/{memory_id}/deprecate")
async def deprecate_memory(memory_id: str):
    """Soft-delete: mark deprecated (recoverable — Regel 1: never hard delete)."""
    try:
        found = qdrant.retrieve(collection_name=QDRANT_COLLECTION, ids=[_qdrant_id(memory_id)], with_payload=True)
        if not found:
            return JSONResponse({"error": "Memory not found"}, status_code=404)
        payload = dict(found[0].payload or {})
        payload["lifecycle_status"] = "deprecated"
        payload["deprecated_at"] = datetime.now(timezone.utc).isoformat()
        qdrant.set_payload(collection_name=QDRANT_COLLECTION, payload=payload, points=[_qdrant_id(memory_id)])
        _cache["ts"] = 0
        return {"status": "ok", "id": memory_id, "lifecycle_status": "deprecated"}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/memories/{memory_id}/restore")
async def restore_memory(memory_id: str):
    """Undelete: deprecated → canonical (rollback of a soft-delete)."""
    try:
        found = qdrant.retrieve(collection_name=QDRANT_COLLECTION, ids=[_qdrant_id(memory_id)], with_payload=True)
        if not found:
            return JSONResponse({"error": "Memory not found"}, status_code=404)
        payload = dict(found[0].payload or {})
        payload["lifecycle_status"] = "canonical"
        payload["restored_at"] = datetime.now(timezone.utc).isoformat()
        qdrant.set_payload(collection_name=QDRANT_COLLECTION, payload=payload, points=[_qdrant_id(memory_id)])
        _cache["ts"] = 0
        return {"status": "ok", "id": memory_id, "lifecycle_status": "canonical"}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/memories/{memory_id}/why")
async def why_memory(memory_id: str):
    """Full metadata: WHY this memory exists and how it behaves."""
    try:
        found = qdrant.retrieve(collection_name=QDRANT_COLLECTION, ids=[_qdrant_id(memory_id)], with_payload=True)
        if not found:
            return JSONResponse({"error": "Memory not found"}, status_code=404)
        pl = found[0].payload or {}
        return {
            "id": memory_id,
            "text": (pl.get("content") or pl.get("text") or "")[:2000],
            "category": pl.get("category"),
            "access_level": pl.get("access_level"),
            "scope": pl.get("scope", "default"),
            "lifecycle_status": pl.get("lifecycle_status"),
            "source": pl.get("source"),
            "source_url": pl.get("source_url"),
            "confidence": pl.get("confidence"),
            "created_at": pl.get("created_at") or pl.get("created"),
            "deprecated_at": pl.get("deprecated_at"),
            "superseded": pl.get("superseded"),
            "agent": pl.get("agent"),
        }
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ---------------------------------------------------------------------------
# SPA catch-all
# ---------------------------------------------------------------------------
@app.get("/{path:path}")
async def spa(path: str):
    if path.startswith("api/") or path.startswith("static/"):
        return JSONResponse({"error": "Not found"}, status_code=404)
    index_path = STATIC / "index.html"
    if not index_path.exists():
        return JSONResponse({"error": "Frontend not built"}, status_code=500)
    return HTMLResponse(index_path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------
# Success moment (Nebo law 07.09: a finished install must SHOW the dashboard):
# after the server boots, open the browser ONCE per installation (marker file
# prevents re-opening on every start) and ALWAYS print the URL + bookmark
# hint — headless systems get the hint instead of the browser.
WEBUI_URL = "http://127.0.0.1:9210"
_OPEN_MARKER = Path.home() / ".nexus-webui-opened"


def _print_url_banner(url: str) -> None:
    """Always shown: the full URL + bookmark recommendation (belt & braces)."""
    line = "─" * 62
    print()
    print(line)
    print("  🧠 Nexus Memory Dashboard is running")
    print()
    print(f"      {url}")
    print()
    print("  Tip: bookmark this address in your browser so you can")
    print("  open your memory dashboard anytime — one click, no setup.")
    print(line)
    print()


def _maybe_open_browser(url: str) -> None:
    """Open the browser once per installation. Never crash, never nag."""
    try:
        if _OPEN_MARKER.exists():
            return  # already shown before — no spam
        _OPEN_MARKER.parent.mkdir(parents=True, exist_ok=True)
        _OPEN_MARKER.write_text(url, encoding="utf-8")
        import webbrowser

        webbrowser.open(url)
        print(f"Opened dashboard in your browser: {url}")
    except Exception as exc:  # headless/SSH/no default browser — hint suffices
        logging.info("webui: browser auto-open skipped (%s)", exc)


if __name__ == "__main__":
    _print_url_banner(WEBUI_URL)
    _maybe_open_browser(WEBUI_URL)
    uvicorn.run(
        "main:app",
        host="127.0.0.1",
        port=9210,
        reload=True,
        log_level="info",
    )
