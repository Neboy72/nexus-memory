#!/usr/bin/env python3
"""Nexus Memory Web UI — FastAPI Backend with Live Qdrant Connection"""

import json
import os
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
if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host="127.0.0.1",
        port=9120,
        reload=True,
        log_level="info",
    )
