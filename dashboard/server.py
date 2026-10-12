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

import asyncio
import json
import logging
import os
import time
import re
import sys
import urllib.request
from urllib.parse import urlparse
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

_logger = logging.getLogger(__name__)

try:
    from fastapi import Depends, FastAPI, HTTPException, Request
    from fastapi.responses import HTMLResponse, JSONResponse
    from pydantic import BaseModel
    from fastapi.staticfiles import StaticFiles
    import uvicorn
except ImportError:
    print("FastAPI not installed. Run: pip install fastapi uvicorn")
    sys.exit(1)

# Config
QDRANT_URL = os.getenv("NEXUS_QDRANT_URL", "http://localhost:6333")
COLLECTION = os.getenv("NEXUS_COLLECTION", "nexus")

# Node budget for the graph view — spread over categories, not a raw first page.
GRAPH_NODE_BUDGET = 500
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


async def _qdrant_request_async(endpoint: str, data: dict = None) -> dict:
    """W30-1 (blocking I/O moved off the event loop).

    ``_qdrant_request`` uses a blocking urllib call with a 5s timeout. Every
    async handler must go through this wrapper — a direct call would freeze
    the whole event loop (all other requests AND WebSockets) for the duration
    of the Qdrant round-trip.
    """
    return await asyncio.to_thread(_qdrant_request, endpoint, data)


def _probe_ollama(timeout: float = 2.0) -> None:
    """Blocking ollama liveness probe (W30-1) — call via ``asyncio.to_thread``."""
    urllib.request.urlopen("http://localhost:11434/api/tags", timeout=timeout)


# ---------------------------------------------------------------------------
# W30-3 CSRF/local-origin guard
# ---------------------------------------------------------------------------
# State-changing routes (POST/PATCH/DELETE) are only reachable from the local
# dashboard: they require the custom header below AND, when the browser sends
# Origin/Referer, a loopback host. A cross-site page can neither set a custom
# header without a CORS preflight nor forge these, which closes the
# DNS-rebinding / cross-origin-POST hole on the localhost port.
# NOTE: this is not an auth system — the server binds loopback only
# (uvicorn default host 127.0.0.1, see ``main()``); if that ever changes to
# 0.0.0.0 the guard must be revisited (TODO, intentionally not changed here).
_ALLOWED_ORIGIN_HOSTS = {"localhost", "127.0.0.1", "::1"}


def verify_local_mutation(request: Request) -> None:
    """FastAPI dependency: reject non-local state-changing requests (W30-3)."""
    if request.headers.get("X-Nexus-Dashboard") != "1":
        raise HTTPException(
            status_code=403,
            detail="missing X-Nexus-Dashboard header — dashboard mutations only",
        )
    origin = request.headers.get("origin") or request.headers.get("referer")
    if origin:
        try:
            host = urlparse(origin).hostname
        except ValueError:
            host = None
        if host not in _ALLOWED_ORIGIN_HOSTS:
            raise HTTPException(
                status_code=403,
                detail="cross-origin mutation blocked",
            )


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
    if "error" in resp:
        # A transport failure is not a missing memory: without this, every
        # inspector route would answer a misleading 404 for a Qdrant outage.
        raise HTTPException(
            status_code=503,
            detail=f"Qdrant unavailable: {resp['error']}",
        )
    result = resp.get("result")
    # Qdrant returns result as a LIST of points for /points (retrieve)
    points = result if isinstance(result, list) else (result.get("points") if isinstance(result, dict) else None)
    if not points:
        return None
    return points[0].get("payload") or {}


def _set_payload(memory_id: str, payload: dict) -> bool:
    body = {"payload": payload, "points": [_qdrant_id(memory_id)]}
    resp = _qdrant_request(f"/collections/{COLLECTION}/points/payload?wait=true", body)
    if "error" in resp:
        # Same distinction as _retrieve_payload: an unreachable Qdrant must not
        # be reported as a generic write failure (misleads retry/diagnostics).
        raise HTTPException(
            status_code=503,
            detail=f"Qdrant unavailable: {resp['error']}",
        )
    return resp.get("status") == "ok"


def _env_float(name: str, default: str) -> float:
    """Parse a float env var defensively — a malformed value (e.g. "5,00")
    must not abort the whole response with a ValueError."""
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return float(default)


SEARCH_RESULT_LIMIT = 50


@app.get("/api/memories/search")
async def search_memories(q: str = "", limit: int = 5):
    """Vector search over the live collection.

    The web UI has always called this path, but the route never existed — the
    search box therefore could not return anything. The embedding is taken from
    the running service (POST /embed): it already holds the same provider and
    vector space, and building a second model inside this process is what
    blocked the whole UI on 12.10.
    """
    query = (q or "").strip()
    if not query:
        return {"query": "", "count": 0, "memories": []}
    try:
        limit = max(1, min(int(limit or 5), SEARCH_RESULT_LIMIT))
    except (TypeError, ValueError):
        limit = 5

    try:
        vec = await asyncio.to_thread(_service_embed, query)
    except Exception as e:
        # Fail loud: an empty result must never look like "nothing found".
        _logger.warning("Suche: Dienst lieferte kein Embedding: %s", e)
        return {"query": query, "count": 0, "memories": [], "error": f"embedding unavailable: {e}"}

    name = _named_vector()
    body = {"vector": {"name": name, "vector": vec}, "limit": limit, "with_payload": True} \
        if name else {"vector": vec, "limit": limit, "with_payload": True}
    try:
        resp = await _qdrant_request_async(f"/collections/{COLLECTION}/points/search", body)
    except Exception as e:
        _logger.warning("Suche: Qdrant-Abfrage fehlgeschlagen: %s", e)
        return {"query": query, "count": 0, "memories": [], "error": str(e)}

    memories = []
    for hit in resp.get("result", []) or []:
        fields = _extract_memory_fields(hit.get("payload", {}))
        memories.append({
            "id": str(hit.get("id", "")),
            "text": fields.get("text", ""),
            "title": fields.get("title", ""),
            "category": fields.get("category"),
            "access_level": fields.get("access_level"),
            "confidence": fields.get("confidence", 0.7),
            "source": fields.get("source"),
            "created_at": fields.get("created_at"),
            "score": hit.get("score"),
        })
    return {"query": query, "count": len(memories), "memories": memories}


@app.get("/api/memories/{memory_id}/why")
async def inspector_why(memory_id: str):
    """Full metadata: WHY this memory exists and how it behaves."""
    pl = await asyncio.to_thread(_retrieve_payload, memory_id)
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


@app.patch("/api/memories/{memory_id}/text", dependencies=[Depends(verify_local_mutation)])
async def inspector_patch_text(memory_id: str, body: dict | None = None):
    """Edit-in-place: replace the memory's text in its payload."""
    if not body:
        return JSONResponse({"error": "body required"}, status_code=400)
    new_text = str(body.get("text") or "").strip()
    if not new_text:
        return JSONResponse({"error": "text must not be empty"}, status_code=400)
    pl = await asyncio.to_thread(_retrieve_payload, memory_id)
    if pl is None:
        return JSONResponse({"error": "Memory not found"}, status_code=404)
    # set_payload MERGES, so send only the changed fields: writing the whole
    # read payload back would clobber any field changed by a concurrent edit /
    # deprecate between the read above and this write.
    changes = {"updated_at": datetime.now(timezone.utc).isoformat()}
    if "content" in pl:
        changes["content"] = new_text
    if "text" in pl or "content" not in pl:
        changes["text"] = new_text
    if not await asyncio.to_thread(_set_payload, memory_id, changes):
        return JSONResponse({"error": "Qdrant update failed"}, status_code=500)
    return {"status": "ok", "id": memory_id, "text": new_text[:120]}


@app.post("/api/memories/{memory_id}/deprecate", dependencies=[Depends(verify_local_mutation)])
async def inspector_deprecate(memory_id: str):
    """Soft-delete: mark deprecated (recoverable — never hard delete)."""
    pl = await asyncio.to_thread(_retrieve_payload, memory_id)
    if pl is None:
        return JSONResponse({"error": "Memory not found"}, status_code=404)
    # Merge-write only the changed fields (see inspector_patch_text).
    changes = {
        "lifecycle_status": "deprecated",
        "deprecated_at": datetime.now(timezone.utc).isoformat(),
    }
    if not await asyncio.to_thread(_set_payload, memory_id, changes):
        return JSONResponse({"error": "Qdrant write failed"}, status_code=500)
    return {"status": "ok", "id": memory_id, "lifecycle_status": "deprecated"}


@app.post("/api/memories/{memory_id}/restore", dependencies=[Depends(verify_local_mutation)])
async def inspector_restore(memory_id: str):
    """Undelete: deprecated → canonical (rollback of a soft-delete)."""
    pl = await asyncio.to_thread(_retrieve_payload, memory_id)
    if pl is None:
        return JSONResponse({"error": "Memory not found"}, status_code=404)
    # Merge-write only the changed fields (see inspector_patch_text).
    changes = {
        "lifecycle_status": "canonical",
        "restored_at": datetime.now(timezone.utc).isoformat(),
    }
    if not await asyncio.to_thread(_set_payload, memory_id, changes):
        return JSONResponse({"error": "Qdrant write failed"}, status_code=500)
    return {"status": "ok", "id": memory_id, "lifecycle_status": "canonical"}



# ── Embedding: EINE Quelle der Wahrheit ──────────────────────────────────────
# Der Dienst haelt den echten Provider und den passenden Vektorraum. Das
# Dashboard baut KEIN eigenes Modell mehr: ein sentence-transformers-Init im
# Event-Loop hat am 12.10. jeden Endpunkt blockiert (16-GB-Mac, Speicher voll).
SERVICE_URL = os.getenv("NEXUS_SERVE_URL", "http://127.0.0.1:9122")


def _detect_embed_provider() -> str:
    """Which embedding provider is actually in force.

    Same order as the engine itself (nexus_memory.embeddings): the environment
    variable wins, then the config file, then a present cloud key. The card used
    to read the config file and the keys only — it never looked at
    NEXUS_EMBEDDING_PROVIDER, so it named a provider that was not in use.
    """
    provider = os.environ.get("NEXUS_EMBEDDING_PROVIDER", "").strip().lower()
    if provider:
        return provider
    try:
        import yaml
        config_path = os.path.expanduser("~/.hermes/config.yaml")
        if os.path.exists(config_path):
            with open(config_path) as f:
                cfg = yaml.safe_load(f) or {}
            provider = (cfg.get("nexus-memory", {}).get("embed_provider") or "").strip().lower()
            if provider:
                return provider
    except Exception as e:
        _logger.debug("provider detect: config read failed: %s", e)
    for p, key in (("voyage", "VOYAGE_API_KEY"), ("openai", "OPENAI_API_KEY"),
                   ("google", "GOOGLE_API_KEY"), ("jina", "JINA_API_KEY")):
        if os.getenv(key):
            return p
    return "unknown"


def _named_vector() -> str:
    """Name of the vector space the collection actually holds ("" if anonymous)."""
    try:
        resp = _qdrant_request(f"/collections/{COLLECTION}")
        vectors = (resp.get("result", {}).get("config", {}).get("params", {}).get("vectors", {}) or {})
        if isinstance(vectors, dict) and vectors and "size" not in vectors:
            return next(iter(vectors))
    except Exception as e:
        _logger.debug("named vector lookup failed: %s", e)
    return ""


def _service_embed(text: str) -> list:
    """Ask the running service for the vector (it holds the right provider)."""
    body = json.dumps({"text": text}).encode("utf-8")
    req = urllib.request.Request(f"{SERVICE_URL}/embed", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    vec = payload.get("embedding")
    if not isinstance(vec, list) or not vec:
        raise ValueError("service returned no embedding")
    return vec


def _store_state(collection_ok: bool, qdrant_reachable: bool) -> str:
    """A missing collection is NOT a dead Qdrant — the card called both that."""
    if collection_ok:
        return "ok"
    return "collection_missing" if qdrant_reachable else "qdrant_unreachable"


def _fuel_status(ollama_base: str | None = None, probe=None) -> dict:
    """Welche KI-Station wuerde die Kette JETZT nehmen — und ist sie geprueft?

    Duenner Wrapper: die Wahrheit ueber die Kette lebt in der Engine
    (``fuel_chain.station_status``). Diese Oberflaeche baut sie NICHT nach —
    genau daraus entstand der Defekt vom 12.10.2026, als die Kachel den
    Anbieter aus einem ":cloud" im Modellnamen riet. Signatur und Rueckgabe
    bleiben unveraendert (die Wachen in tests/ haengen daran); zusaetzlich
    traegt die Antwort die Budget-Zeile der Kachel.

    ``probe``: optionales ``callable(name) -> bool``, wird durchgereicht.

    BLOCKIEREND (HTTP-Probe, Ketten-Bau) — Aufrufer MUESSEN das ueber
    ``asyncio.to_thread`` erledigen; im Event-Loop legt genau das die ganze
    Oberflaeche lahm (Ausfall vom 12.10.).
    """
    out = {
        "enabled": True,
        "open_station": None,   # erste GEPRUEFT offene Station
        "next_station": None,   # was die Kette jetzt nehmen wuerde
        "model": "",
        "provider": "",
        "verified": False,
        "stations": [],
        "budget_usd": _env_float("NEXUS_FUEL_BUDGET_USD", "5.00"),
        "spent_usd": 0.0,
    }
    # Verbrauch aus der Engine, NICHT aus einer eigenen Datei-Lesung. Die Engine
    # schreibt ihr Konto als {"year_month": ..., "spent_usd": ...} unter
    # NEXUS_FUEL_STATE (Standard ~/.nexus-memory/fuel_spend.json). Diese Stelle
    # suchte frueher einen Schluessel "2026-10" bzw. "total", fand deshalb NIE
    # etwas und stand dauerhaft auf $0.00 — auch wenn Geld verbraucht war. Eine
    # zweite Lesung ist eine zweite Wahrheit; hier gilt nur die der Engine.
    try:
        from nexus_memory.fuel_chain import fuel_exhausted_info
        info = fuel_exhausted_info()
        out["budget_usd"] = info.get("budget_usd", out["budget_usd"])
        out["spent_usd"] = info.get("spent_usd", 0.0)
    except Exception as e:
        _logger.debug("_fuel_status: fuel budget state unreadable: %s", e)

    # Import erst hier, nicht zur Modul-Zeit: kein Nebeneffekt beim Server-Start.
    try:
        from nexus_memory.fuel_chain import station_status  # single source of truth
    except Exception as e:
        # Fremde Umgebung ohne das Paket: lieber ehrlich "keine Station" als ein
        # geratener Modellname. Ein stiller Fehlschlag, der in eine Falschaussage
        # kippt, ist der schlechteste Ausgang.
        _logger.warning("_fuel_status: nexus_memory nicht verfuegbar: %s", e)
        return out

    try:
        out.update(station_status(ollama_base=ollama_base, probe=probe))
    except Exception as e:
        # Die Engine haelt sich selbst fail-safe; hier bleibt nur die letzte
        # Reissleine, damit die Kachel nie einen geratenen Namen zeigt.
        _logger.warning("_fuel_status: station_status fehlgeschlagen: %s", e)
    return out


@app.get("/api/status")
async def get_system_status():
    """Get overall system status: health, version, stats."""
    # Qdrant health — zwei verschiedene Fehler, zwei verschiedene Namen.
    # Eine fehlende Sammlung ist kein totes Qdrant; vorher hiess beides gleich.
    collection_ok = False
    qdrant_reachable = False
    points_count = 0
    try:
        resp = await _qdrant_request_async(f"/collections/{COLLECTION}")
        result = resp.get("result", {})
        # Qdrant returns "status" field (e.g. "green") not "collection_name"
        collection_ok = result.get("status") in ("green", "yellow", "red") or "points_count" in result
        qdrant_reachable = True
        points_count = result.get("points_count", 0)
    except Exception as e:
        # Fail-closed, aber LAUT: eine unbekannte Sammlung antwortet 404 und
        # wurde vorher stillschweigend als "Qdrant Offline" angezeigt.
        qdrant_reachable = not any(m in str(e) for m in ("doesn't exist", "Not found", "404"))
        _logger.warning("get_system_status: Sammlung %s nicht lesbar: %s", COLLECTION, e)

    qdrant_healthy = collection_ok
    store_state = _store_state(collection_ok, qdrant_reachable)

    # Embedding provider — EINE Quelle der Wahrheit, in der Reihenfolge der Engine
    config_path = os.path.expanduser("~/.hermes/config.yaml")
    embed_provider = _detect_embed_provider()
    if embed_provider == "unknown":
        try:
            await asyncio.to_thread(_probe_ollama, 2)
            embed_provider = "ollama"
        except Exception as e:
            _logger.debug("get_system_status: ollama probe failed: %s", e)

    # Version — aus dem INSTALLIERTEN nexus_memory-Paket (importlib.metadata),
    # NICHT aus dem Legacy-Repo-Root-Paket `nexus` (dessen __init__ ist veraltet
    # und bleibt z.B. auf 0.13.5 stehen — Fable-Bug-Muster: Docs/Code driftet).
    try:
        from importlib.metadata import version as _pkg_version
        version = _pkg_version("nexus-memory")
    except Exception:
        version = "unknown"

    # Fuel-Status: welche KI-Station nutzt der Konsolidierungs-Daemon, wie viel verbraucht?
    # Die Kette selbst befragen (kein fest eingebauter Name mehr) und das IMMER
    # im Thread: die Ollama-Probe ist HTTP und wuerde im Event-Loop JEDEN
    # anderen Endpunkt blockieren (Ausfall vom 12.10.).
    fuel = await asyncio.to_thread(_fuel_status)

    # Echter Modellname statt nur Provider-Label — Regel: volle Transparenz.
    # NIE auf dem Event-Loop bauen: ein sentence-transformers-Init dauert Minuten
    # und blockiert dann JEDEN anderen Endpunkt (genau der Ausfall vom 12.10.).
    # Der Fingerprint der Sammlung nennt das Modell, das wirklich drinliegt.
    embed_model = embed_provider
    named = _named_vector()
    if named and "__" in named:
        embed_model = named.split("__")[1]

    return {
        "version": version,
        "qdrant_healthy": qdrant_healthy,
        "store_state": store_state,
        "points_count": points_count,
        "embedding_provider": embed_model,  # echtes Modell, nicht nur Provider-Label
        "config_path": config_path,
        "fuel": fuel,
    }


class FuelPaidRequest(BaseModel):
    enabled: bool = True


@app.post("/api/fuel/paid", dependencies=[Depends(verify_local_mutation)])
async def api_fuel_paid(request: FuelPaidRequest):
    """User-Toggle für PAID Konsolidierungs-Stationen (OpenAI/OpenRouter).

    AN  (default): KI-Sortierung aktiv — Ollama immer, Fremd-Anbieter bei Budget.
    AUS:                keine Fremd-Anbieter mehr; Ollama (lokal/Cloud) bleibt.
    """
    try:
        enabled = bool(request.enabled)
        from nexus_memory import fuel_chain
        p = fuel_chain.FUEL_TOGGLE_PATH
        p.parent.mkdir(parents=True, exist_ok=True)
        if enabled:
            p.touch()
            msg = "KI-Sortierung aktiviert — deine Erinnerungen werden wieder automatisch sortiert."
        else:
            p.unlink(missing_ok=True)
            msg = "KI-Sortierung deaktiviert — deine Erinnerungen werden nicht mehr automatisch sortiert."
        return JSONResponse({"ok": True, "enabled": enabled, "message": msg})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


@app.get("/api/agents")
async def get_agents():
    """List all connected agents with trust levels.

    Self-healing: ghost entries (agents uninstalled from this machine) are
    cleaned from the registry before serving the list.
    """
    try:
        cleanup_removed_agents()
    except Exception as e:
        # cleanup must never break the agents listing — log for diagnosis.
        _logger.debug("agents list: cleanup_removed_agents failed: %s", e)
    registry = load_agents_registry()
    return registry


@app.post("/api/agents/cleanup", dependencies=[Depends(verify_local_mutation)])
async def cleanup_agents():
    """Manually trigger ghost-agent cleanup. Returns the removal report."""
    return cleanup_removed_agents()


@app.post("/api/agents/{agent_id}/trust", dependencies=[Depends(verify_local_mutation)])
async def update_trust_level(agent_id: str, level: str):
    """Change trust level for an agent."""
    result = set_agent_trust_level(agent_id, level)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@app.get("/api/memories/stats")
async def get_memory_stats():
    """Get memory statistics from Qdrant - scrolls ALL points for accurate counts."""
    # Sync urllib scroll (up to 500 requests) — off the event loop.
    points = await asyncio.to_thread(_scroll_all_memories, 500_000)
    
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
    """Get memories list with optional filters. Used by the D3 graph page.

    Active filters scroll the FULL collection (cap 50k) so the limit counts
    MATCHING points — otherwise rare access levels (e.g. trusted: 202 of
    21.8k) come back nearly empty and the graph looks broken.
    """
    # IMMER die volle Sammlung lesen. Ohne Filter wurde vorher nur die erste
    # Seite gelesen — ein beliebiger Ausschnitt, kein Querschnitt: bei ~75 %
    # Fakten enthaelt er praktisch nur Fakten, und die Graph-Seite zeigte dann
    # nur zwei Farben. Die volle Sammlung kostet ~0,6 s (gemessen) und macht
    # Auswahl, Zaehlung UND Filter ehrlich.
    all_points = await asyncio.to_thread(_scroll_all_memories, 500_000)

    memories = []

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

    # ECHTE Verteilung, bevor ausgewaehlt wird: die Legende darf nicht die
    # gekuerzte Auswahl widerspiegeln, sonst stuenden dort 33 statt 28.931.
    by_cat_full: dict = {}
    for mem in memories:
        by_cat_full.setdefault(mem["category"], []).append(mem)
    category_counts = {cat: len(mems) for cat, mems in by_cat_full.items()}

    # H189: trim to the requested limit. Aber als QUERSCHNITT: Round-Robin
    # ueber die Kategorien, damit eine kleine Kategorie nicht von der
    # Faktenmasse verdraengt wird.
    if len(memories) > limit:
        ordered = sorted(by_cat_full.items(), key=lambda kv: -len(kv[1]))
        picked: list = []
        idx = 0
        while len(picked) < limit:
            moved = False
            for _, group in ordered:
                if idx < len(group):
                    picked.append(group[idx])
                    moved = True
                    if len(picked) >= limit:
                        break
            if not moved:
                break
            idx += 1
        memories = picked

    # Group the (trimmed) memories for edge building.
    by_cat: dict = {}
    by_source: dict = {}
    for mem in memories:
        by_cat.setdefault(mem["category"], []).append(mem)
        by_source.setdefault(mem["source"], []).append(mem)

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

    return {"memories": memories, "edges": edges, "category_counts": category_counts}


@app.get("/api/stats")
async def get_full_stats():
    """Full stats for the D3 graph page - scans ALL memories."""
    # Sync urllib scroll (up to 500 requests) — off the event loop.
    all_points = await asyncio.to_thread(_scroll_all_memories, 500_000)

    total_resp = await _qdrant_request_async(f"/collections/{COLLECTION}")
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
        # W30-2: `payload.get("confidence", 0.7)` only covers a MISSING key —
        # an explicit `"confidence": null` (or a str/bool) reached the
        # aggregation and crashed sum() with TypeError. Non-numeric values
        # fall back to the neutral default (bool is an int subclass — exclude
        # it explicitly).
        _conf = f["confidence"]
        confidences.append(
            _conf if isinstance(_conf, (int, float)) and not isinstance(_conf, bool) else 0.7
        )
        if payload.get("confidence") is not None:
            confidences_real += 1

        # Growth + freshness: created_at comes in many schema variants.
        created = f.get("created_at")
        ts = None
        if isinstance(created, (int, float)):
            # seconds or milliseconds epoch. An out-of-range value (absurdly
            # large or negative) makes fromtimestamp raise — that must not
            # abort the whole /api/stats response.
            try:
                ts = datetime.fromtimestamp(created / 1000 if created > 1e11 else created, tz=timezone.utc)
            except (ValueError, OverflowError, OSError):
                ts = None
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
        # Counted during the scan above — the previous second full pass over
        # every point was redundant (and the counter was dead code).
        "confidence_real_count": confidences_real,
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
    """Get memory graph data (categories as nodes, shared attributes as edges).

    The category counts come from the FULL collection. A single first-page
    scroll is an arbitrary window and not a sample: with roughly three quarters
    of the store being facts, the rare categories (session, belief, preference)
    never appear inside it, so the graph looked as if the store held nothing but
    facts. The counts are complete now, and the node budget is spread over the
    categories so a small one stays visible next to the mass of facts.
    """
    all_points = await asyncio.to_thread(_scroll_all_memories, 500_000)

    by_cat: dict[str, list] = {}
    for p in all_points:
        fields = _extract_memory_fields(p.get("payload", {}))
        cat = fields.get("category") or "uncategorized"
        text = (fields.get("text") or "")[:100]
        by_cat.setdefault(cat, []).append({
            "id": p.get("id", ""),
            "label": text[:50],
            "category": cat,
            "access_level": fields.get("access_level", "none"),
            "full_text": text,
        })

    categories = {cat: len(items) for cat, items in by_cat.items()}

    per_cat = max(1, GRAPH_NODE_BUDGET // max(1, len(by_cat)))
    nodes = [node for items in by_cat.values() for node in items[:per_cat]]
    
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
    def _probe_qdrant_healthz() -> None:
        # Only liveness matters — the body is not needed (no dead read).
        with urllib.request.urlopen(f"{QDRANT_URL}/healthz", timeout=5):
            pass

    try:
        # W30-1: blocking probe moved off the event loop.
        await asyncio.to_thread(_probe_qdrant_healthz)
        checks["qdrant"] = {"status": "ok", "detail": "alive"}
    except Exception as e:
        checks["qdrant"] = {"status": "error", "detail": str(e)}

    # Collection
    try:
        resp = await _qdrant_request_async(f"/collections/{COLLECTION}")
        result = resp.get("result", {})
        checks["collection"] = {
            "status": "ok" if result.get("status") in ("green", "yellow", "red") or "points_count" in result else "error",
            "name": COLLECTION,
            "points": result.get("points_count", 0),
            "vectors_config": result.get("config", {}).get("params", {}).get("vectors", {}),
        }
    except Exception as e:
        checks["collection"] = {"status": "error", "detail": str(e)}
    
    # Embedding provider - EINE Quelle der Wahrheit (siehe _detect_embed_provider)
    embed_provider = _detect_embed_provider()
    if embed_provider == "unknown":
        try:
            await asyncio.to_thread(_probe_ollama, 2)
            embed_provider = "ollama"
        except Exception as e:
            _logger.debug("health: ollama probe failed: %s", e)
    checks["embedding"] = {
        "status": "ok" if embed_provider != "unknown" else "warning",
        "provider": embed_provider,
    }
    
    # Overall
    all_ok = all(c.get("status") == "ok" for c in checks.values())
    checks["overall"] = "healthy" if all_ok else "issues"
    
    return checks


@app.post("/api/backup", dependencies=[Depends(verify_local_mutation)])
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


@app.post("/api/agents/{agent_id}/connect", dependencies=[Depends(verify_local_mutation)])
async def connect_agent(agent_id: str):
    """One-click connect: write the nexus MCP entry into the agent's own config.

    Connection (maintainer semantics) — Nexus registers itself in a detected agent.
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
        if not isinstance(existing, dict):
            return JSONResponse(
                {"error": "config unreadable: expected a JSON object"},
                status_code=500,
            )

    servers = existing.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        # A non-object "mcpServers" would make servers.update() raise an opaque
        # 500 — fail with a clear error like the unreadable-config path above.
        return JSONResponse(
            {"error": "config unreadable: mcpServers must be a JSON object"},
            status_code=500,
        )
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
            # Rule (10.10.2026): the user connected this agent themselves on this
            # machine, so it is the owner — full read access by default. Narrowing
            # it down (trusted/public) stays a deliberate dashboard choice.
            trust_level="private",
            install_type="mcp",
            config_dir=meta.get("config_dir") or str(cfg_path.parent),
        )
    except Exception as e:
        # registration is best-effort; the config write itself succeeded.
        _logger.debug("connect: agent registration failed: %s", e)
    return {"status": "ok", "action": "connected" if not already else "already-connected", "config": str(cfg_path)}


@app.post("/api/agents/{agent_id}/disconnect", dependencies=[Depends(verify_local_mutation)])
async def disconnect_agent(agent_id: str):
    """Toggle Off: remove the nexus MCP entry from the agent's own config.

    Reverse of connect (maintainer toggle semantics). Guards:
    - only pure-MCP agents are disconnectable via the dashboard; agents
      whose install_type contains "plugin" are wired into the host core
      and must never be touched by a web click (Regel 1 spirit).
    - the config write is preceded by a timestamped .bak backup.
    """
    config_paths = {
        "windsurf": (Path.home() / ".codeium" / "windsurf" / "mcp_config.json", "json"),
        "pi": (Path.home() / ".pi" / "agent" / "settings.json", "json"),
    }
    if agent_id not in config_paths:
        return JSONResponse({"error": f"no config target known for '{agent_id}'"}, status_code=400)

    # Guard: never disconnect plugin-embedded agents from the web UI.
    try:
        from nexus_memory.agent_detect import load_agents_registry

        reg_entry = next(
            (a for a in load_agents_registry().get("agents", []) if a.get("id") == agent_id),
            None,
        )
        if reg_entry and "plugin" in (reg_entry.get("install_type") or ""):
            return JSONResponse(
                {"error": "plugin-embedded agent — disconnect only via its host config"},
                status_code=403,
            )
    except Exception as e:
        # registry read failure must not silently allow/deny; config is
        # authoritative — log it for diagnosis.
        _logger.debug("disconnect: registry read failed: %s", e)

    cfg_path, fmt = config_paths[agent_id]

    existing: dict = {}
    if not cfg_path.exists():
        # Nothing wired in the config: still drop a stale registry entry.
        removed = False
        try:
            from nexus_memory.agent_detect import unregister_agent

            removed = unregister_agent(agent_id)
        except Exception:
            removed = False
        return {"status": "ok", "action": "not-wired", "registry_removed": removed}

    try:
        existing = json.loads(cfg_path.read_text())
    except Exception as exc:
        return JSONResponse({"error": f"config unreadable: {exc}"}, status_code=500)
    if not isinstance(existing, dict):
        return JSONResponse(
            {"error": "config unreadable: expected a JSON object"},
            status_code=500,
        )

    servers = existing.get("mcpServers") or {}
    if "nexus" not in servers:
        removed = False
        try:
            from nexus_memory.agent_detect import unregister_agent

            removed = unregister_agent(agent_id)
        except Exception:
            removed = False
        return {"status": "ok", "action": "already-disconnected", "registry_removed": removed}

    # Backup before write (Regel 1: proof first)
    backup = cfg_path.with_suffix(
        cfg_path.suffix + f".bak-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    )
    backup.write_text(cfg_path.read_text())

    servers.pop("nexus")
    if servers or existing.keys() - {"mcpServers"}:
        existing["mcpServers"] = servers
        cfg_path.write_text(json.dumps(existing, indent=2) + "\n")
    else:
        # The file existed ONLY to hold the nexus entry — remove it entirely
        # so a bare leftover config does not re-trigger detection.
        cfg_path.unlink()

    # Drop the agent from the registry so it moves back to Available.
    removed = False
    try:
        from nexus_memory.agent_detect import unregister_agent

        removed = unregister_agent(agent_id)
    except Exception:
        removed = False
    return {"status": "ok", "action": "disconnected", "registry_removed": removed}


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

class NoCacheStatic(StaticFiles):
    def file_response(self, *args, **kwargs):
        resp = super().file_response(*args, **kwargs)
        resp.headers["Cache-Control"] = "no-cache"
        return resp

if static_dir.exists():
    app.mount("/static", NoCacheStatic(directory=str(static_dir)), name="static")

# Handbook (offline user guide) — ships with the package, opens from the Docs button
handbook_dir = DASHBOARD_DIR / "handbook"
if handbook_dir.exists():
    # html=True so the mount itself serves index.html for "/handbook": the
    # mount is registered before the explicit route below and Starlette matches
    # in registration order, so without it that route was unreachable and
    # "/handbook" answered 404 (StaticFiles html=False on a directory path).
    app.mount("/handbook", NoCacheStatic(directory=str(handbook_dir), html=True), name="handbook")

    @app.get("/handbook", response_class=HTMLResponse, include_in_schema=False)
    async def handbook_index():
        idx = handbook_dir / "index.html"
        if idx.exists():
            return HTMLResponse(content=idx.read_text(), headers={"Cache-Control": "no-cache"})
        return HTMLResponse(content="<h1>Handbook not found</h1>")

# Assets directory (logos, brand)
assets_dir = DASHBOARD_DIR / "assets"
if assets_dir.exists():
    app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")


# Success moment (project rule 07.09/09.09: a finished install AND every update must
# SHOW the dashboard): after boot, open the browser ONCE per installation/update
# (marker file prevents re-opening on every start) and ALWAYS print the URL +
# bookmark hint — headless systems get the hint instead of the browser.
_OPEN_MARKER = Path.home() / ".nexus-dashboard-opened"


def print_url_banner(url: str) -> None:
    """Always shown: the full URL + bookmark recommendation (belt & braces)."""
    line = "\u2500" * 62
    print()
    print(line)
    print("  \U0001f9e0 Nexus Memory Dashboard is running")
    print()
    print(f"      {url}")
    print()
    print("  Tip: bookmark this address in your browser so you can")
    print("  open your memory dashboard anytime \u2014 one click, no setup.")
    print(line)
    print()


def reset_open_marker() -> None:
    """Called after a successful update so the next dashboard start re-opens
    the browser \u2014 an update gets the same success moment as a fresh install."""
    import contextlib
    with contextlib.suppress(Exception):
        _OPEN_MARKER.unlink()


def maybe_open_browser(url: str) -> None:
    """Open the browser once per installation/update. Never crash, never nag.
    NEXUS_DASHBOARD_NO_OPEN=1 (headless hosts, e.g. the Mac Mini) skips the
    browser pop and keeps the banner \u2014 the URL hint is the success moment."""
    try:
        import os
        if os.environ.get("NEXUS_DASHBOARD_NO_OPEN", "").strip().lower() in ("1", "true", "yes"):
            return
        if _OPEN_MARKER.exists():
            return  # already shown before \u2014 no spam
        _OPEN_MARKER.parent.mkdir(parents=True, exist_ok=True)
        _OPEN_MARKER.write_text(url, encoding="utf-8")
        import webbrowser
        webbrowser.open(url)
        print(f"Opened dashboard in your browser: {url}")
    except Exception as exc:  # headless/SSH/no default browser \u2014 hint suffices
        logging.info("dashboard: browser auto-open skipped (%s)", exc)


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Nexus Memory Dashboard")
    parser.add_argument("--port", type=int, default=9121)
    parser.add_argument("--host", type=str, default="127.0.0.1")
    args = parser.parse_args()
    # Derive the printed/bookmarked URL from --host so it is reachable: a
    # wildcard bind (0.0.0.0) maps to loopback for the banner.
    host = args.host
    if host in ("0.0.0.0", "::", ""):
        host = "127.0.0.1"
    url = f"http://{host}:{args.port}"

    print_url_banner(url)
    maybe_open_browser(url)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()