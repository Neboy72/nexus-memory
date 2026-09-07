#!/usr/bin/env python3
"""Nexus Memory Dashboard - FastAPI Backend.

Serves the Nexus Memory dashboard with:
- Agent management (list, trust-level toggle)
- Memory stats
- Health check
- Graph data
- Backup trigger

Run: python3 dashboard.py --port 9120
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.request
import urllib.error
from pathlib import Path
from datetime import datetime, timezone

# ── Load user .env so the dashboard inherits Hermes env vars ──────────
# When launched via LaunchAgent, environment variables from ~/.hermes/.env
# are not inherited. This ensures VOYAGE_API_KEY etc. are always available.
def _load_user_env():
    for env_path in [
        Path.home() / ".hermes" / ".env",
        Path(__file__).parent.parent / ".env",
    ]:
        if env_path.exists():
            with open(env_path) as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, _, val = line.partition("=")
                    key, val = key.strip(), val.strip()
                    if key and key not in os.environ:
                        os.environ[key] = val

_load_user_env()

# Add parent to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from nexus_memory.agent_detect import (
    load_agents_registry,
    set_agent_trust_level,
    detect_all_agents,
    cleanup_removed_agents,
)
from nexus_memory.chat_wizard import get_status, TRUST_LEVELS

try:
    from fastapi import FastAPI, HTTPException
    from fastapi.responses import HTMLResponse, JSONResponse
    from fastapi.staticfiles import StaticFiles
    import uvicorn
except ImportError:
    print("FastAPI not installed. Run: pip install fastapi uvicorn")
    sys.exit(1)

# Config
QDRANT_URL = os.getenv("NEXUS_QDRANT_URL", "http://localhost:6333")
COLLECTION = os.getenv("NEXUS_COLLECTION", "nexus")
DASHBOARD_DIR = Path(__file__).parent

app = FastAPI(title="Nexus Memory Dashboard", version="0.5.0")


def _qdrant_request(endpoint: str, data: dict = None) -> dict:
    """Make a Qdrant API request."""
    url = f"{QDRANT_URL}{endpoint}"
    if data:
        req = urllib.request.Request(
            url,
            data=json.dumps(data).encode(),
            headers={"Content-Type": "application/json"}
        )
    else:
        req = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read())
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Memory Inspector backend (parity with webui/main.py — Astra item 3).
# Soft-only writes: deprecate sets lifecycle_status=deprecated (recoverable),
# never hard-delete. Legacy numeric point IDs are normalized to int
# (Qdrant accepts unsigned ints or UUIDs, not numeric strings).
# ---------------------------------------------------------------------------
def _qdrant_id(memory_id: str):
    mid = (memory_id or "").strip()
    if re.fullmatch(r"\d+", mid):
        return int(mid)
    return memory_id


def _retrieve_payload(memory_id: str) -> dict | None:
    body = {"ids": [_qdrant_id(memory_id)], "with_payload": True}
    resp = _qdrant_request(f"/collections/{COLLECTION}/points", body)
    result = resp.get("result")
    # Qdrant returns result as a LIST of points for /points (retrieve)
    points = result if isinstance(result, list) else (result.get("points") if isinstance(result, dict) else None)
    if not points:
        return None
    return points[0].get("payload") or {}


def _set_payload(memory_id: str, payload: dict) -> bool:
    body = {"payload": payload, "points": [_qdrant_id(memory_id)]}
    resp = _qdrant_request(f"/collections/{COLLECTION}/points/payload?wait=true", body)
    return resp.get("status") == "ok"


@app.get("/api/memories/{memory_id}/why")
async def inspector_why(memory_id: str):
    """Full metadata: WHY this memory exists and how it behaves."""
    pl = _retrieve_payload(memory_id)
    if pl is None:
        return JSONResponse({"error": "Memory not found"}, status_code=404)
    return {
        "id": memory_id,
        "text": (pl.get("content") or pl.get("text") or pl.get("title") or "")[:2000],
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


@app.patch("/api/memories/{memory_id}/text")
async def inspector_patch_text(memory_id: str, body: dict | None = None):
    """Edit-in-place: replace the memory's text in its payload."""
    if not body:
        return JSONResponse({"error": "body required"}, status_code=400)
    new_text = str(body.get("text") or "").strip()
    if not new_text:
        return JSONResponse({"error": "text must not be empty"}, status_code=400)
    pl = _retrieve_payload(memory_id)
    if pl is None:
        return JSONResponse({"error": "Memory not found"}, status_code=404)
    payload = dict(pl)
    if "content" in payload:
        payload["content"] = new_text
    if "text" in payload or "content" not in payload:
        payload["text"] = new_text
    payload["updated_at"] = datetime.now(timezone.utc).isoformat()
    if not _set_payload(memory_id, payload):
        return JSONResponse({"error": "Qdrant update failed"}, status_code=500)
    return {"status": "ok", "id": memory_id, "text": new_text[:120]}


@app.post("/api/memories/{memory_id}/deprecate")
async def inspector_deprecate(memory_id: str):
    """Soft-delete: mark deprecated (recoverable — never hard delete)."""
    pl = _retrieve_payload(memory_id)
    if pl is None:
        return JSONResponse({"error": "Memory not found"}, status_code=404)
    payload = dict(pl)
    payload["lifecycle_status"] = "deprecated"
    payload["deprecated_at"] = datetime.now(timezone.utc).isoformat()
    if not _set_payload(memory_id, payload):
        return JSONResponse({"error": "Qdrant write failed"}, status_code=500)
    return {"status": "ok", "id": memory_id, "lifecycle_status": "deprecated"}


@app.post("/api/memories/{memory_id}/restore")
async def inspector_restore(memory_id: str):
    """Undelete: deprecated → canonical (rollback of a soft-delete)."""
    pl = _retrieve_payload(memory_id)
    if pl is None:
        return JSONResponse({"error": "Memory not found"}, status_code=404)
    payload = dict(pl)
    payload["lifecycle_status"] = "canonical"
    payload["restored_at"] = datetime.now(timezone.utc).isoformat()
    if not _set_payload(memory_id, payload):
        return JSONResponse({"error": "Qdrant write failed"}, status_code=500)
    return {"status": "ok", "id": memory_id, "lifecycle_status": "canonical"}


@app.get("/api/status")
async def get_system_status():
    """Get overall system status: health, version, stats."""
    # Qdrant health
    qdrant_healthy = False
    points_count = 0
    try:
        resp = _qdrant_request(f"/collections/{COLLECTION}")
        result = resp.get("result", {})
        # Qdrant returns "status" field (e.g. "green") not "collection_name"
        qdrant_healthy = result.get("status") in ("green", "yellow", "red") or "points_count" in result
        points_count = result.get("points_count", 0)
    except Exception:
        pass

    # Config - detect embedding provider from config or env
    embed_provider = "unknown"
    config_path = os.path.expanduser("~/.hermes/config.yaml")
    try:
        import yaml
        if os.path.exists(config_path):
            with open(config_path) as f:
                cfg = yaml.safe_load(f) or {}
            embed_provider = cfg.get("nexus-memory", {}).get("embed_provider", "unknown")
        if embed_provider == "unknown":
            for p, key in [("voyage", "VOYAGE_API_KEY"), ("openai", "OPENAI_API_KEY"), ("google", "GOOGLE_API_KEY"), ("jina", "JINA_API_KEY")]:
                if os.getenv(key):
                    embed_provider = p
                    break
            else:
                try:
                    import urllib.request as ur
                    ur.urlopen("http://localhost:11434/api/tags", timeout=2)
                    embed_provider = "ollama"
                except:
                    pass
    except Exception:
        pass

    # Version — aus dem INSTALLIERTEN nexus_memory-Paket (importlib.metadata),
    # NICHT aus dem Legacy-Repo-Root-Paket `nexus` (dessen __init__ ist veraltet
    # und bleibt z.B. auf 0.13.5 stehen — Fable-Bug-Muster: Docs/Code driftet).
    try:
        from importlib.metadata import version as _pkg_version
        version = _pkg_version("nexus-memory")
    except Exception:
        version = "unknown"

    return {
        "version": version,
        "qdrant_healthy": qdrant_healthy,
        "points_count": points_count,
        "embedding_provider": embed_provider,
        "config_path": config_path,
    }


@app.get("/api/agents")
async def get_agents():
    """List all connected agents with trust levels.

    Self-healing: ghost entries (agents uninstalled from this machine) are
    cleaned from the registry before serving the list.
    """
    try:
        cleanup_removed_agents()
    except Exception:
        pass  # cleanup must never break the agents listing
    registry = load_agents_registry()
    return registry


@app.post("/api/agents/cleanup")
async def cleanup_agents():
    """Manually trigger ghost-agent cleanup. Returns the removal report."""
    return cleanup_removed_agents()


@app.post("/api/agents/{agent_id}/trust")
async def update_trust_level(agent_id: str, level: str):
    """Change trust level for an agent."""
    result = set_agent_trust_level(agent_id, level)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@app.get("/api/memories/stats")
async def get_memory_stats():
    """Get memory statistics from Qdrant - scrolls ALL points for accurate counts."""
    points = _scroll_all_memories(max_points=500_000)
    
    categories = {}
    access_levels = {}
    for p in points:
        payload = p.get("payload", {})
        fields = _extract_memory_fields(payload)
        cat = fields.get("category", "unknown")
        level = fields.get("access_level", "none")
        categories[cat] = categories.get(cat, 0) + 1
        access_levels[level] = access_levels.get(level, 0) + 1
    
    total = len(points)
    
    return {
        "total": total,
        "by_category": categories,
        "by_access_level": access_levels,
    }


def _scroll_all_memories(max_points: int = 5000) -> list:
    """Scroll ALL memories from Qdrant using pagination. Returns all payloads.

    Safe cap: 500_000 points — a runaway loop guard, NOT a practical limit.
    """
    all_points = []
    offset = None
    while len(all_points) < max_points:
        body = {"limit": min(1000, max_points - len(all_points)), "with_payload": True, "with_vector": False}
        if offset:
            body["offset"] = offset
        resp = _qdrant_request(f"/collections/{COLLECTION}/points/scroll", body)
        result = resp.get("result", {})
        points = result.get("points", [])
        if not points:
            break
        all_points.extend(points)
        offset = result.get("next_page_offset")
        if not offset:
            break
    return all_points


def _extract_memory_fields(payload: dict) -> dict:
    """Extract normalized fields from a Qdrant payload, handling 32+ schema variants."""
    text = payload.get("text") or payload.get("content") or payload.get("title") or ""
    source = payload.get("source") or "unknown"
    category = payload.get("category") or "uncategorized"

    # Access level: PAPERLESS = ALWAYS PRIVATE, no exceptions
    if source == "paperless":
        access_level = "private"
    elif payload.get("access_level"):
        access_level = payload.get("access_level")
    else:
        trust = payload.get("trust")
        if trust is not None:
            if trust <= 0.3:
                access_level = "private"
            elif trust >= 0.8:
                access_level = "public"
            else:
                access_level = "trusted"
        elif source in ("wiki", "hermes-plugin", "hermes-builtin"):
            access_level = "public"
        else:
            access_level = "unknown"

    # Drift: explicit field only, no guessing
    drift = payload.get("drift") or "not_tracked"

    return {
        "text": text,
        "title": payload.get("title", ""),
        "category": category,
        "access_level": access_level,
        "drift": drift,
        "source": source,
        "confidence": payload.get("confidence", 0.7),
        "created_at": payload.get("created_at") or payload.get("created") or payload.get("modified") or payload.get("timestamp"),
    }


@app.get("/api/memories")
async def get_memories(category: str = "all", access_level: str = "all", drift: str = "all", source: str = "", limit: int = 500):
    """Get memories list with optional filters. Used by the D3 graph page."""
    all_points = _scroll_all_memories(max_points=min(limit * 2, 5000))

    memories = []
    by_cat = {}
    by_source = {}

    for p in all_points:
        payload = p.get("payload", {})
        f = _extract_memory_fields(payload)

        # Apply filters
        if category != "all" and f["category"] != category:
            continue
        if access_level != "all" and f["access_level"] != access_level:
            continue
        if drift != "all" and f["drift"] != drift:
            continue
        if source and f["source"] != source:
            continue

        mem = {
            "id": str(p.get("id", "")),
            "text": f["text"],
            "title": f["title"],
            "category": f["category"],
            "access_level": f["access_level"],
            "access": f["access_level"],
            "confidence": f["confidence"],
            "drift": f["drift"],
            "source": f["source"],
            "created_at": f["created_at"],
            "fullText": f["text"],
        }
        memories.append(mem)
        by_cat.setdefault(f["category"], []).append(mem)
        by_source.setdefault(f["source"], []).append(mem)

    # Build edges: connect memories sharing the same source OR same category
    # Each memory connects to its nearest neighbors (max 3 per group) to avoid clutter
    edges = []
    seen_pairs = set()
    for group in [by_cat, by_source]:
        for key, mems in group.items():
            if len(mems) < 2:
                continue
            # Connect each memory to up to 3 others in same group
            for i, m1 in enumerate(mems):
                for j in range(i + 1, min(i + 4, len(mems))):
                    m2 = mems[j]
                    pair = tuple(sorted([m1["id"], m2["id"]]))
                    if pair not in seen_pairs:
                        edges.append({"source": m1["id"], "target": m2["id"]})
                        seen_pairs.add(pair)

    category_counts = {cat: len(mems) for cat, mems in by_cat.items()}
    return {"memories": memories, "edges": edges, "category_counts": category_counts}


@app.get("/api/stats")
async def get_full_stats():
    """Full stats for the D3 graph page - scans ALL memories."""
    all_points = _scroll_all_memories(max_points=500_000)

    total_resp = _qdrant_request(f"/collections/{COLLECTION}")
    total = total_resp.get("result", {}).get("points_count", 0)

    by_cat = {}
    by_drift = {}
    sources = set()
    confidences = []
    confidences_real = 0
    grown_week = 0
    last_memory_at = None
    last_memory_dt = None

    import datetime as _dt
    week_ago = datetime.now(timezone.utc) - _dt.timedelta(days=7)

    for p in all_points:
        payload = p.get("payload", {})
        f = _extract_memory_fields(payload)

        by_cat[f["category"]] = by_cat.get(f["category"], 0) + 1
        by_drift[f["drift"]] = by_drift.get(f["drift"], 0) + 1
        sources.add(f["source"])
        confidences.append(f["confidence"])
        if payload.get("confidence") is not None:
            confidences_real += 1

        # Growth + freshness: created_at comes in many schema variants.
        created = f.get("created_at")
        ts = None
        if isinstance(created, (int, float)):
            # seconds or milliseconds epoch
            ts = datetime.fromtimestamp(created / 1000 if created > 1e11 else created, tz=timezone.utc)
        elif isinstance(created, str) and created.strip():
            try:
                raw = created.strip().replace("Z", "+00:00")
                ts = datetime.fromisoformat(raw)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
            except Exception:
                ts = None
        if ts is not None:
            if ts >= week_ago:
                grown_week += 1
            if last_memory_at is None or ts > last_memory_dt:
                last_memory_dt = ts
                last_memory_at = ts.isoformat()

    avg_conf = sum(confidences) / len(confidences) if confidences else 0.7

    # Count edges (same-source + same-category links)
    edge_count = sum(v - 1 for v in by_cat.values() if v > 1)

    # Map drift statuses to fresh/drifting/drifted for the dashboard
    drift_summary = {
        "fresh": by_drift.get("fresh", 0),
        "drifting": by_drift.get("drifting", 0),
        "drifted": by_drift.get("drifted", 0),
        "not_tracked": by_drift.get("not_tracked", 0),
    }

    return {
        "total_memories": total,
        "total_edges": edge_count,
        "avg_confidence": avg_conf,
        "confidence_real_count": sum(1 for p in all_points if (p.get("payload") or {}).get("confidence") is not None),
        "total_unique_sources": len(sources),
        "by_category": by_cat,
        "by_drift_status": drift_summary,
        # Newbie-truth metrics: growth, freshness, duplicates
        "grown_this_week": grown_week,
        "last_memory_at": last_memory_at,
        "by_category_counts": {k: v for k, v in sorted(by_cat.items(), key=lambda x: -x[1])},
    }


@app.get("/api/graph")
async def get_graph_data():
    """Get memory graph data (categories as nodes, shared attributes as edges)."""
    resp = _qdrant_request(
        f"/collections/{COLLECTION}/points/scroll",
        {"limit": 500, "with_payload": True, "with_vector": False}
    )
    
    points = resp.get("result", {}).get("points", [])
    
    # Build nodes from memories
    nodes = []
    categories = {}
    for p in points[:500]:  # Cap at 500 for performance
        payload = p.get("payload", {})
        cat = payload.get("category", "unknown")
        text = (payload.get("text") or payload.get("content", ""))[:100]
        level = payload.get("access_level", "none")
        
        nodes.append({
            "id": p.get("id", ""),
            "label": text[:50],
            "category": cat,
            "access_level": level,
            "full_text": text,
        })
        
        categories[cat] = categories.get(cat, 0) + 1
    
    # Build category nodes
    cat_nodes = [
        {"id": f"cat-{cat}", "label": cat, "category": "meta", "count": count}
        for cat, count in categories.items()
    ]
    
    # Build edges: memory → category
    edges = [
        {"source": n["id"], "target": f"cat-{n['category']}"}
        for n in nodes
    ]
    
    return {
        "nodes": cat_nodes + nodes,
        "edges": edges,
        "categories": categories,
    }


@app.get("/api/health")
async def health_check():
    """Detailed health check."""
    checks = {}
    
    # Qdrant
    try:
        import urllib.request as ur
        health_url = f"{QDRANT_URL}/healthz"
        with ur.urlopen(health_url, timeout=5) as r:
            body = r.read().decode()
        checks["qdrant"] = {"status": "ok", "detail": "alive"}
    except Exception as e:
        checks["qdrant"] = {"status": "error", "detail": str(e)}
    
    # Collection
    try:
        resp = _qdrant_request(f"/collections/{COLLECTION}")
        result = resp.get("result", {})
        checks["collection"] = {
            "status": "ok" if result.get("status") in ("green", "yellow", "red") or "points_count" in result else "error",
            "name": COLLECTION,
            "points": result.get("points_count", 0),
            "vectors_config": result.get("config", {}).get("params", {}).get("vectors", {}),
        }
    except Exception as e:
        checks["collection"] = {"status": "error", "detail": str(e)}
    
    # Embedding provider - detect from config or env
    embed_provider = "unknown"
    try:
        import yaml
        config_path = os.path.expanduser("~/.hermes/config.yaml")
        if os.path.exists(config_path):
            with open(config_path) as f:
                cfg = yaml.safe_load(f) or {}
            embed_provider = cfg.get("nexus-memory", {}).get("embed_provider", "unknown")
        if embed_provider == "unknown":
            # Check env vars
            for p, key in [("voyage", "VOYAGE_API_KEY"), ("openai", "OPENAI_API_KEY"), ("google", "GOOGLE_API_KEY"), ("jina", "JINA_API_KEY")]:
                if os.getenv(key):
                    embed_provider = p
                    break
            else:
                # Check for ollama
                try:
                    import urllib.request as ur
                    r = ur.urlopen("http://localhost:11434/api/tags", timeout=2)
                    embed_provider = "ollama"
                except:
                    pass
    except Exception:
        pass
    checks["embedding"] = {
        "status": "ok" if embed_provider != "unknown" else "warning",
        "provider": embed_provider,
    }
    
    # Overall
    all_ok = all(c.get("status") == "ok" for c in checks.values())
    checks["overall"] = "healthy" if all_ok else "issues"
    
    return checks


@app.post("/api/backup")
async def trigger_backup():
    """Trigger a manual backup (full JSON export via the production backup engine)."""
    try:
        from nexus_memory.mcp_server import MemoryStore, QDRANT_HOST, QDRANT_PORT
        from qdrant_client import QdrantClient

        store = MemoryStore.__new__(MemoryStore)
        # Only the pieces the backup engine touches — no embedding init, no
        # collection side effects. Fail loud if the engine grows a new dependency.
        store.client = QdrantClient(host=QDRANT_HOST, port=QDRANT_PORT)
        store._embedder = None
        store._hybrid_retriever = None
        store._skill_graph = None
        store._scope_centroids = None
        result = store._do_backup()
        return {"status": "ok", "backup_path": result}
    except Exception as e:
        return {"status": "error", "error": str(e)}


@app.post("/api/agents/{agent_id}/connect")
async def connect_agent(agent_id: str):
    """One-click connect: write the nexus MCP entry into the agent's own config.

    Connection (Nebo semantics) — Nexus registers itself in a detected agent.
    Every config write is preceded by a timestamped .bak backup (Regel 1),
    the operation is idempotent (already connected → no-op success).
    """
    config_paths = {
        "windsurf": (Path.home() / ".codeium" / "windsurf" / "mcp_config.json", "json"),
        "pi": (Path.home() / ".pi" / "agent" / "settings.json", "json"),
    }
    if agent_id not in config_paths:
        return JSONResponse({"error": f"no config target known for '{agent_id}'"}, status_code=400)

    cfg_path, fmt = config_paths[agent_id]
    nexus_entry = {
        "nexus": {"command": "nexus-memory", "args": [], "env": {}}
    }

    existing: dict = {}
    if cfg_path.exists():
        try:
            existing = json.loads(cfg_path.read_text())
        except Exception as exc:
            return JSONResponse({"error": f"config unreadable: {exc}"}, status_code=500)

    servers = existing.setdefault("mcpServers", {})
    already = "nexus" in servers
    if not already:
        # Backup before write (Regel 1: proof first)
        if cfg_path.exists():
            backup = cfg_path.with_suffix(
                cfg_path.suffix + f".bak-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
            )
            backup.write_text(cfg_path.read_text())

        servers.update(nexus_entry)
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text(json.dumps(existing, indent=2) + "\n")

    # Register in the agents registry so the agent moves to Connected Agents.
    try:
        from nexus_memory.agent_detect import detect_all_agents, register_agent

        detected = {a["id"]: a for a in detect_all_agents().get("detected_agents", [])}
        meta = detected.get(agent_id) or {}
        register_agent(
            agent_id=agent_id,
            name=meta.get("name") or agent_id,
            icon=meta.get("icon") or "🔌",
            trust_level="trusted",
            install_type="mcp",
            config_dir=meta.get("config_dir") or str(cfg_path.parent),
        )
    except Exception:
        pass  # registration is best-effort; the config write itself succeeded
    return {"status": "ok", "action": "connected" if not already else "already-connected", "config": str(cfg_path)}


@app.get("/api/detect")
async def detect_agents():
    """Run agent auto-detection."""
    return detect_all_agents()


@app.get("/", response_class=HTMLResponse)
async def dashboard_home():
    html_path = DASHBOARD_DIR / "static" / "index.html"
    if html_path.exists():
        return HTMLResponse(content=html_path.read_text(), headers={"Cache-Control": "no-cache"})
    return HTMLResponse(content="<h1>Nexus Memory Dashboard</h1><p>UI not found. Run from nexus-memory/dashboard/</p>")

@app.get("/graph", response_class=HTMLResponse)
async def graph_page():
    html_path = DASHBOARD_DIR / "static" / "graph.html"
    if html_path.exists():
        return HTMLResponse(content=html_path.read_text())
    return HTMLResponse(content="<h1>Graph not found</h1>")


# Static files — no-cache so the dashboard UI updates land immediately
# (cached stale CSS was serving the inspector unstyled = "hingerotzt" look).
static_dir = DASHBOARD_DIR / "static"
if static_dir.exists():
    from starlette.middleware import Middleware

    class NoCacheStatic(StaticFiles):
        def file_response(self, *args, **kwargs):
            resp = super().file_response(*args, **kwargs)
            resp.headers["Cache-Control"] = "no-cache"
            return resp

    app.mount("/static", NoCacheStatic(directory=str(static_dir)), name="static")

# Assets directory (logos, brand)
assets_dir = DASHBOARD_DIR / "assets"
if assets_dir.exists():
    app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Nexus Memory Dashboard")
    parser.add_argument("--port", type=int, default=9120)
    parser.add_argument("--host", type=str, default="0.0.0.0")
    args = parser.parse_args()
    
    print(f"Nexus Memory Dashboard starting on http://localhost:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()