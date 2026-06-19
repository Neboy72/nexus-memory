"""
nexus-memory — MCP Server (v0.4.0)
Universal Memory Layer for AI Agents

Integrates all nexus v2.8.0 features:
- MemoryCategory Enum (fact, belief, session, rule, preference, temp)
- Provenance (source_url, confidence)
- Guardrails (content length, PII hints)
- Access Control (public/trusted/private)
- Qdrant vector storage
- Embedding: auto-detect (sentence-transformers local → Voyage cloud)
"""

import asyncio
import json
import logging
import os
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import mcp.server.stdio
import mcp.types as types
from mcp.server import Server
from mcp.server.models import InitializationOptions
from qdrant_client import QdrantClient
from qdrant_client.http import models as qmodels

# Integrate from the nexus package (v2.8.0+ features)
import nexus
from nexus import MemoryCategory, __version__ as nexus_version
from nexus.provenance import attach_source
from nexus.config import is_success

# Repo-Root dynamisch ableiten (nexus/ liegt im Repo-Root)
_NEXUS_REPO = os.path.dirname(os.path.dirname(nexus.__file__))
NEXUS_REPO_PATH = os.environ.get("NEXUS_REPO_PATH", _NEXUS_REPO)

# ── Auto-load .env files ──────────────────────────────────────────
# Load from NEXUS_ENV_FILE explicit path, then ~/.hermes/.env, then cwd/.env
env_paths = []
custom_env = os.environ.get("NEXUS_ENV_FILE")
if custom_env:
    env_paths.append(Path(custom_env))
env_paths.append(Path.home() / ".hermes" / ".env")
env_paths.append(Path.cwd() / ".env")

for env_path in env_paths:
    if env_path.exists():
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, val = line.split("=", 1)
                    key = key.strip()
                    val = val.strip().strip("'\"")
                    if key not in os.environ:
                        os.environ[key] = val

# ── Config ────────────────────────────────────────────────────────

QDRANT_HOST = os.environ.get("NEXUS_QDRANT_HOST", "localhost")
QDRANT_PORT = int(os.environ.get("NEXUS_QDRANT_PORT", "6333"))
COLLECTION_NAME = os.environ.get("NEXUS_COLLECTION", "nexus")
VOYAGE_API_KEY = os.environ.get("VOYAGE_API_KEY", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY", "")


# ── Webhook Subscriptions ─────────────────────────────────────────

# Event types emitted by the MCP server when memory state changes.
# Subscriptions are stored as plain JSON in ~/.nexus-webhooks.json so the
# feature has zero new dependencies and no impact on the Qdrant collection.
WEBHOOK_EVENTS = ("memory.remember", "memory.update", "memory.forget")
WEBHOOK_STORE_PATH = Path.home() / ".nexus-webhooks.json"


class WebhookStore:
    """Persistent store for webhook subscriptions.

    Subscriptions are kept in a single JSON file (``~/.nexus-webhooks.json``
    by default) so we don't need a new Qdrant collection, a new SQLite
    database, or any new runtime dependency. The file is small (handful of
    entries) and is read / written through an ``asyncio.Lock`` so a
    concurrent ``subscribe()`` and ``unsubscribe()`` cannot lose data.

    Schema on disk::

        {
          "subscriptions": [
            {
              "id": "uuid4-string",
              "event_type": "memory.remember",
              "webhook_url": "https://example.com/hook",
              "created_at": "2026-06-13T12:34:56+00:00"
            }
          ]
        }
    """

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path is not None else WEBHOOK_STORE_PATH
        self._lock = asyncio.Lock()

    # ---- low-level IO --------------------------------------------------

    def _read_sync(self) -> list[dict]:
        """Synchronous read of the JSON file. Caller must hold ``_lock``.

        Returns an empty list when the file is missing or unreadable so a
        fresh install / permission hiccup never breaks the MCP server.
        """
        if not self.path.exists():
            return []
        try:
            with self.path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            logging.warning(f"Webhook store read failed: {exc}; starting empty")
            return []
        if not isinstance(data, dict):
            return []
        subs = data.get("subscriptions", [])
        return subs if isinstance(subs, list) else []

    def _write_sync(self, subs: list[dict]) -> None:
        """Synchronous write of the JSON file. Caller must hold ``_lock``."""
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            with tmp.open("w", encoding="utf-8") as fh:
                json.dump({"subscriptions": subs}, fh, indent=2)
            tmp.replace(self.path)
        except OSError as exc:
            logging.error(f"Webhook store write failed: {exc}")

    # ---- public API ----------------------------------------------------

    async def list(self) -> list[dict]:
        """Return all subscriptions currently registered."""
        async with self._lock:
            return list(self._read_sync())

    async def subscribe(self, event_type: str, webhook_url: str) -> dict:
        """Register a new subscription and return it (with a fresh ``id``)."""
        if event_type not in WEBHOOK_EVENTS:
            raise ValueError(
                f"Unknown event_type {event_type!r}. "
                f"Valid: {', '.join(WEBHOOK_EVENTS)}"
            )
        if not webhook_url or not (
            webhook_url.startswith("http://") or webhook_url.startswith("https://")
        ):
            raise ValueError(
                "webhook_url must be a non-empty http:// or https:// URL"
            )

        sub = {
            "id": str(uuid.uuid4()),
            "event_type": event_type,
            "webhook_url": webhook_url,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        async with self._lock:
            subs = self._read_sync()
            subs.append(sub)
            self._write_sync(subs)
        logging.info(f"Webhook subscribed: {sub['id'][:8]} {event_type} -> {webhook_url}")
        return sub

    async def unsubscribe(self, subscription_id: str) -> bool:
        """Remove the subscription with ``subscription_id``. Returns True
        when something was actually removed, False when the id was unknown.
        """
        async with self._lock:
            subs = self._read_sync()
            kept = [s for s in subs if s.get("id") != subscription_id]
            if len(kept) == len(subs):
                return False
            self._write_sync(kept)
        logging.info(f"Webhook unsubscribed: {subscription_id[:8]}")
        return True

    async def matching(self, event_type: str) -> list:
        """Return subscriptions for ``event_type``. Used by the dispatcher."""
        async with self._lock:
            return [s for s in self._read_sync() if s.get("event_type") == event_type]


_webhook_store: Optional["WebhookStore"] = None


def get_webhook_store() -> WebhookStore:
    """Module-level singleton accessor — keeps the same pattern as
    ``get_store()`` so the webhook store can be patched in tests via
    ``monkeypatch.setattr(mcp, "_webhook_store", ...)``.
    """
    global _webhook_store
    if _webhook_store is None:
        _webhook_store = WebhookStore()
    return _webhook_store


async def dispatch_event(event_type: str, memory_id: str) -> None:
    """Fire-and-forget dispatch of ``event_type`` to every matching webhook.

    Each subscription is POSTed to in its own background task. Errors
    (timeouts, 4xx/5xx, connection failures) are caught and logged so
    a single broken subscriber can never crash the MCP server or block
    the main tool call.

    Body shape: ``{"event": ..., "memory_id": ..., "timestamp": ...}``.
    """
    if event_type not in WEBHOOK_EVENTS:
        return  # unknown events are silently ignored — defense in depth
    try:
        subs = await get_webhook_store().matching(event_type)
    except Exception as exc:
        logging.warning(f"Webhook lookup failed for {event_type}: {exc}")
        return
    if not subs:
        return

    payload = {
        "event": event_type,
        "memory_id": memory_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    for sub in subs:
        url = sub.get("webhook_url")
        if not url:
            continue
        # create_task is enough — the inner coroutine catches all errors.
        try:
            asyncio.create_task(_post_webhook(url, payload))
        except RuntimeError as exc:
            # No running loop (e.g. during interpreter shutdown).
            logging.debug(f"Skipping webhook fire (no loop): {exc}")


async def _post_webhook(url: str, payload: dict) -> None:
    """POST ``payload`` to ``url`` and swallow every error.

    Tried first with ``httpx.AsyncClient`` (already a transitive dep in
    the project's recall path); if it's not installed, falls back to
    ``urllib.request`` in a thread executor. Either way, every exception
    is caught and logged — webhooks are best-effort.
    """
    body = json.dumps(payload).encode("utf-8")
    try:
        try:
            import httpx  # type: ignore
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.post(url, content=body,
                                          headers={"Content-Type": "application/json"})
                if resp.status_code >= 400:
                    logging.warning(
                        f"Webhook {url} returned {resp.status_code} for "
                        f"{payload.get('event')}"
                    )
                return
        except ImportError:
            pass

        # Fallback: blocking urllib in a thread so the event loop stays free.
        loop = asyncio.get_running_loop()

        def _send():
            import urllib.request
            req = urllib.request.Request(
                url,
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                # Drain body so the connection can be reused.
                resp.read(1024)

        await loop.run_in_executor(None, _send)
    except Exception as exc:
        logging.warning(
            f"Webhook fire failed for {url} (event={payload.get('event')}): {exc}"
        )


# ── Embedding Provider ────────────────────────────────────────────

from nexus_memory.embeddings import EmbeddingProvider

# ── Access Levels ──────────────────────────────────────────────────

ACCESS_PUBLIC = "public"      # All agents can see
ACCESS_TRUSTED = "trusted"    # Only trusted agents
ACCESS_PRIVATE = "private"    # Only owner / explicitly permitted

ALL_ACCESS_LEVELS = [ACCESS_PUBLIC, ACCESS_TRUSTED, ACCESS_PRIVATE]
ACCESS_HIERARCHY = {
    ACCESS_PUBLIC: 0,
    ACCESS_TRUSTED: 1,
    ACCESS_PRIVATE: 2,
}


def _to_point_id(val):
    """Coerce a point ID to a Qdrant-friendly typed value.

    Qdrant accepts UUID strings, integer strings (auto-coerced to int), and
    raw ints. Anything else (None, unknown types) is returned as ``str(val)``.

    Returns:
        int | UUID | str
    """
    from uuid import UUID
    if isinstance(val, int):
        return val
    if isinstance(val, str):
        if "-" in val and len(val) == 36:
            return UUID(val)
        try:
            return int(val)
        except ValueError:
            return val
    if val is None:
        return ""  # callers filter None out before calling
    return str(val)

# ── Guardrails (from v2.8.0) ───────────────────────────────────────
MAX_CONTENT_LENGTH = 5000
PII_PATTERNS = {
    "email": re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"),
    "phone_e164": re.compile(r"\+[1-9]\d{6,14}"),
    "phone_local": re.compile(r"(?<!\w)(0\d{2,3}[/\s-]?\d{3,8}[/\s-]?\d{3,5})(?!\w)"),
}


def _check_content_guardrails(text: str) -> list[str]:
    """Return warnings for content that exceeds limits or contains PII hints."""
    warnings = []
    if len(text) > MAX_CONTENT_LENGTH:
        warnings.append(
            f"Content exceeds {MAX_CONTENT_LENGTH} chars ({len(text)}). "
            "This may impact search quality."
        )
    for label, pattern in PII_PATTERNS.items():
        if pattern.search(text):
            warnings.append(
                f"Detected {label} pattern in content. "
                "Consider using access_level='private' for sensitive data."
            )
    return warnings


# ── Storage ─────────────────────────────────────────────────────────

class MemoryStore:

    def __init__(self):
        self.client = QdrantClient(host=QDRANT_HOST, port=QDRANT_PORT)
        self._embedder = EmbeddingProvider()
        self._hybrid_retriever = None
        self._skill_graph = None
        self._ensure_collection()
        self._init_hybrid()
        self._init_skill_graph()
        self._update_check_result: dict | None = None
        self._update_check_time: float = 0
        self._update_nudged: bool = False  # Only nudge once per server lifetime
        self._backup_nudged: bool = False
        self._last_backup_path: str = ""
        self._last_backup_time: float = 0
        self._check_for_updates_async()
        self._start_auto_backup()

    def _check_for_updates_async(self):
        """Check GitHub for new releases on startup (non-blocking, cached 24h)."""
        import threading, time
        def _bg_check():
            try:
                import urllib.request
                req = urllib.request.Request(
                    "https://api.github.com/repos/Neboy72/nexus-memory/releases/latest",
                    headers={"Accept": "application/vnd.github.v3+json",
                             "User-Agent": f"nexus-memory/{nexus_version}"}
                )
                data = json.loads(urllib.request.urlopen(req, timeout=10).read().decode())
                latest_tag = data.get("tag_name", "").lstrip("v")
                latest_name = data.get("name", latest_tag)
                html_url = data.get("html_url", "")
                from packaging.version import parse
                is_newer = parse(latest_tag) > parse(nexus_version) if latest_tag else False
                self._update_check_result = {
                    "update_available": is_newer,
                    "latest_version": latest_tag,
                    "latest_name": latest_name,
                    "release_url": html_url,
                    "local_version": nexus_version,
                }
                if is_newer:
                    logging.info(f"📦 Nexus Memory update available: v{nexus_version} → v{latest_tag}")
            except Exception as e:
                logging.debug(f"Update check failed: {e}")
            finally:
                self._update_check_time = time.time()
        t = threading.Thread(target=_bg_check, daemon=True)
        t.start()

    def _start_auto_backup(self):
        """Start automatic daily backup of all memories."""
        import threading, time
        def _backup_loop():
            time.sleep(60)  # Wait 60s after startup
            while True:
                try:
                    self._do_backup()
                except Exception as e:
                    logging.warning(f"Auto-backup failed: {e}")
                for _ in range(360):  # 6h, check every 60s
                    time.sleep(60)
        threading.Thread(target=_backup_loop, name="nexus-backup", daemon=True).start()

    def _do_backup(self) -> str:
        """Create a full backup of all memories as JSON. Returns backup file path."""
        import os, time
        from datetime import datetime

        backup_dir = os.path.expanduser("~/.nexus-memory/backups")
        os.makedirs(backup_dir, exist_ok=True)

        all_points = []
        offset = None
        while True:
            results, offset = self.client.scroll(
                collection_name=COLLECTION_NAME,
                limit=100, offset=offset,
                with_payload=True, with_vectors=True,
            )
            for p in results:
                vec = p.vector if isinstance(p.vector, list) else None
                all_points.append({"id": str(p.id), "payload": p.payload or {}, "vector": vec})
            if not offset:
                break

        ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        backup_path = os.path.join(backup_dir, f"nexus-backup-{ts}.json")
        backup_data = {
            "version": nexus_version,
            "collection": COLLECTION_NAME,
            "created_at": datetime.now().isoformat(),
            "point_count": len(all_points),
            "points": all_points,
        }
        with open(backup_path, "w") as f:
            json.dump(backup_data, f, default=str)

        self._last_backup_time = time.time()
        self._last_backup_path = backup_path

        # Keep only last 7 backups
        backups = sorted(
            [f for f in os.listdir(backup_dir) if f.startswith("nexus-backup-")],
            reverse=True
        )
        for old in backups[7:]:
            try: os.remove(os.path.join(backup_dir, old))
            except OSError: pass

        logging.info(f"💾 Auto-backup: {len(all_points)} memories → {backup_path}")
        return backup_path

    def _ensure_collection(self):
        collections = [c.name for c in self.client.get_collections().collections]
        if COLLECTION_NAME not in collections:
            self.client.create_collection(
                collection_name=COLLECTION_NAME,
                vectors_config=qmodels.VectorParams(
                    size=self._embedder.dim,
                    distance=qmodels.Distance.COSINE,
                ),
            )
            self.client.create_payload_index(
                collection_name=COLLECTION_NAME,
                field_name="access_level",
                field_type=qmodels.PayloadSchemaType.KEYWORD,
            )
            logging.info(f"Created collection '{COLLECTION_NAME}' ({self._embedder.dim}d)")

    async def _embed(self, text: str) -> list[float]:
        return await self._embedder.embed(text)

    def _init_hybrid(self):
        """Initialize hybrid retriever (BM25 + Vector + RRF) if available."""
        try:
            from nexus.retrieval import HybridRetriever
            self._hybrid_retriever = HybridRetriever(
                qdrant_host=QDRANT_HOST,
                qdrant_port=QDRANT_PORT,
            )
            self._hybrid_retriever.index_memories()
            logging.info("Hybrid retriever initialized (BM25 + Vector + RRF)")
        except Exception as e:
            logging.warning(f"Hybrid retriever not available: {e}")
            self._hybrid_retriever = None

    def _init_skill_graph(self):
        """Initialize SkillGraph from nexus.graph.graph if available.

        Same defensive pattern as _init_hybrid(): if networkx is not
        installed or the graph package cannot be imported, log a warning
        and set _skill_graph to None so the rest of the server works
        without graph features.
        """
        try:
            from nexus.graph.graph import SkillGraph
            sg = SkillGraph(qdrant_url=f"http://{QDRANT_HOST}:{QDRANT_PORT}",
                            collection=COLLECTION_NAME)
            sg.initialize()
            self._skill_graph = sg
            logging.info("SkillGraph initialized (networkx-backed edge queries)")
        except Exception as e:
            logging.warning(f"SkillGraph not available: {e}")
            self._skill_graph = None

    async def _check_sources(self, source_urls: list[str]) -> dict[str, str]:
        """Check source URLs via async HTTP HEAD. Returns dict mapping url -> status."""
        if not source_urls:
            return {}
        unique_urls = list(set(u for u in source_urls if u and u.startswith("http")))
        if not unique_urls:
            return {}
        results = {}

        async def _check(url: str):
            try:
                import httpx
                async with httpx.AsyncClient(timeout=5.0) as client:
                    resp = await client.head(url, follow_redirects=True)
                    results[url] = "verified" if resp.status_code < 400 else "unreachable"
            except ImportError:
                proc = await asyncio.create_subprocess_exec(
                    "curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
                    "--max-time", "5", url,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
                )
                stdout, _ = await proc.communicate()
                code = int(stdout.decode().strip()) if stdout else 0
                results[url] = "verified" if code and code < 400 else "unreachable"
            except Exception:
                results[url] = "unreachable"

        await asyncio.gather(*[_check(url) for url in unique_urls])
        return results

    async def remember(
        self,
        text: str,
        access_level: str = ACCESS_PUBLIC,
        category: str = "fact",
        source: str = "",
        source_url: str = "",
        confidence: Optional[float] = None,
    ) -> dict:
        """Store a memory with full v2.8.0 metadata support."""
        # Validate category against MemoryCategory
        valid_categories = [c.value for c in MemoryCategory]
        if category not in valid_categories:
            category = MemoryCategory.FACT.value

        entry_id = str(uuid.uuid4())
        created_at = datetime.now(timezone.utc).isoformat()
        vector = await self._embed(text)

        # Build provenance (v2.8.0 feature)
        provenance = {
            "source_type": "mcp",
            "created_by": "nexus-memory-server",
            "timestamp": created_at,
            "confidence": confidence if confidence is not None else 0.7,
        }
        if source_url:
            provenance["source_url"] = source_url
        if source:
            provenance["source"] = source

        payload = {
            "id": entry_id,
            "content": text,
            "access_level": access_level,
            "category": category,
            "source": source,
            "source_url": source_url,
            "created_at": created_at,
            "provenance": provenance,
            "lifecycle_status": "canonical",  # New facts are canonical by default
        }

        self.client.upsert(
            collection_name=COLLECTION_NAME,
            points=[qmodels.PointStruct(
                id=entry_id,
                vector=vector,
                payload=payload,
            )],
        )
        logging.info(f"Stored memory {entry_id[:8]} [{access_level}] cat={category}")

        # ── Auto-Discovery: find related facts via SkillGraph ───────────
        # Lightweight: if the graph is available, discover edges for the
        # newly stored fact. Wrapped in try/except so a discovery failure
        # never breaks the remember() call.
        if self._skill_graph is not None:
            try:
                from nexus.discovery import AutoDiscovery
                ad = AutoDiscovery(
                    qdrant_url=f"http://{QDRANT_HOST}:{QDRANT_PORT}",
                    collection=COLLECTION_NAME,
                )
                candidates = ad.discover_for_fact(
                    fact_id=entry_id,
                    content=text,
                    category=category,
                    vector=vector,
                )
                if candidates:
                    logging.info(
                        f"Auto-discovery found {len(candidates)} edge(s) "
                        f"for memory {entry_id[:8]}"
                    )
            except Exception as disc_err:
                logging.warning(f"Auto-discovery failed for {entry_id[:8]}: {disc_err}")

        # ── Events: fire a CREATED event for audit trail ────────────────
        # Events are nice-to-have, not critical — swallow all errors.
        try:
            from nexus.events import create_event, EventType
            create_event(entry_id, EventType.CREATED, {"text": text[:100]})
        except Exception:
            pass  # Events are nice-to-have, not critical

        return {
            "status": "ok",
            "id": entry_id,
            "access_level": access_level,
            "category": category,
        }

    async def recall(
        self,
        query: str,
        agent_level: str = ACCESS_PUBLIC,
        limit: int = 5,
    ) -> list[dict]:
        """Search memories with hybrid search (BM25 + Vector + RRF) + access filtering."""
        allowed_levels = [ACCESS_PUBLIC]
        agent_lvl = ACCESS_HIERARCHY.get(agent_level, 0)
        if agent_lvl >= 1:
            allowed_levels.append(ACCESS_TRUSTED)
        if agent_lvl >= 2:
            allowed_levels.append(ACCESS_PRIVATE)

        raw_results = []
        query_vector = None
        try:
            # Compute the embedding once — reused in the fallback path below.
            query_vector = await self._embed(query)

            # Try hybrid search first
            if self._hybrid_retriever:
                h_results = self._hybrid_retriever.search(
                    query,
                    query_vector=query_vector,
                    top_k=limit * 2,  # Fetch extra for filtering
                )
                raw_results = h_results
                logging.debug(f"Hybrid search returned {len(raw_results)} results")

                # Enrich hybrid results with payload fields (source_url, access_level, etc.)
                if raw_results:
                    hybrid_ids_raw = [r.get("id") for r in raw_results if r.get("id") is not None]
                    if hybrid_ids_raw:
                        try:
                            parsed_ids = [_to_point_id(pid) for pid in hybrid_ids_raw]
                            points = self.client.retrieve(
                                collection_name=COLLECTION_NAME,
                                ids=parsed_ids,
                                with_payload=True,
                                with_vectors=False,
                            )
                            payload_map = {}
                            for pt in points:
                                pl = pt.payload or {}
                                payload_map[str(pt.id)] = pl
                            for r in raw_results:
                                rid = r.get("id")
                                if rid and rid in payload_map:
                                    pl = payload_map[rid]
                                    if not r.get("source_url"):
                                        r["source_url"] = pl.get("source_url")
                                    if not r.get("access_level"):
                                        r["access_level"] = pl.get("access_level")
                                    # State-prefixing: legacy entries written
                                    # before category was required are missing
                                    # the field — fill with the "fact" default
                                    # so consumers always see a valid value.
                                    if not r.get("category"):
                                        r["category"] = pl.get("category") or MemoryCategory.FACT.value
                                    if not r.get("source"):
                                        r["source"] = pl.get("source")
                                    if not r.get("created_at"):
                                        r["created_at"] = pl.get("created_at")
                                    if not r.get("provenance"):
                                        r["provenance"] = pl.get("provenance", {})
                                    if not r.get("lifecycle_status"):
                                        r["lifecycle_status"] = pl.get("lifecycle_status")
                        except Exception as enrich_err:
                            logging.warning(f"Payload enrichment failed: {enrich_err}")
        except Exception as e:
            logging.warning(f"Hybrid search failed, falling back to vector: {e}")

        # Fallback: vector-only search (reuses the embedding computed above)
        if not raw_results and query_vector is not None:
            response = self.client.query_points(
                collection_name=COLLECTION_NAME,
                query=query_vector,
                limit=limit * 2,
            )
            raw_results = []
            for point in response.points:
                payload = point.payload or {}
                raw_results.append({
                    "id": payload.get("id"),
                    "content": payload.get("content"),
                    "access_level": payload.get("access_level"),
                    # State-prefixing: legacy entries may not have a category —
                    # fall back to "fact" so the response is always well-typed.
                    "category": payload.get("category") or MemoryCategory.FACT.value,
                    "source": payload.get("source"),
                    "source_url": payload.get("source_url"),
                    "provenance": payload.get("provenance", {}),
                    "created_at": payload.get("created_at"),
                    "lifecycle_status": payload.get("lifecycle_status"),
                    "score": point.score,
                })

        # ── Lifecycle filtering: skip deprecated / rolled_back facts ────
        # Only "canonical" or missing lifecycle_status entries are included
        # (missing = backwards compat with old entries written before Phase 2).
        _suppressed_statuses = {"deprecated", "rolled_back"}
        raw_results = [
            r for r in raw_results
            if r.get("lifecycle_status") not in _suppressed_statuses
        ]

        # Normalize scores relative to max score in results
        # Handles both RRF scores (0.001-0.1) and Qdrant scores (0.0-1.0)
        raw_scores = []
        for r in raw_results:
            s = r.get("rrf_score") or r.get("score", 0)
            raw_scores.append(s if isinstance(s, (int, float)) else 0)
        max_raw = max(raw_scores, default=1)
        max_raw = max(max_raw, 0.001)  # Avoid division by zero

        # Justification-Check (Rung 2): Verify source URLs are still reachable
        source_urls = [r.get("source_url") for r in raw_results if r.get("source_url")]
        source_verification = await self._check_sources(source_urls)

        results = []
        seen_docs = set()
        for r in raw_results:
            mem_level = r.get("access_level", ACCESS_PUBLIC)
            if ACCESS_HIERARCHY.get(mem_level, 0) > ACCESS_HIERARCHY.get(agent_level, 0):
                continue
            
            doc_id = r.get("doc_id") or r.get("id")
            text = (r.get("content") or r.get("text") or "").strip()
            score = r.get("rrf_score") or r.get("score", 0)
            if not isinstance(score, (int, float)):
                score = 0
            
            # Normalize score relative to max in result set
            normalized_score = round(score / max_raw, 3)
            
            # Determine match type
            if normalized_score > 0.7:
                match_type = "high"
            elif normalized_score > 0.3:
                match_type = "medium"
            else:
                match_type = "low"
            
            prov = r.get("provenance", {})
            entry = {
                "id": r.get("id"),
                "text": text[:2000],  # Cap for readability
                "score": round(normalized_score, 3),
                "match": match_type,
                "source": r.get("source"),
                "source_url": r.get("source_url"),
                "verification": source_verification.get(r.get("source_url"), "unchecked"),
                "doc_id": doc_id,
                "access_level": mem_level,
                "category": r.get("category"),
                "confidence": prov.get("confidence"),
                "created_at": r.get("created_at"),
            }
            
            # If same doc_id already in results, append text instead of duplicate
            if doc_id and doc_id in seen_docs:
                for existing in results:
                    if existing.get("doc_id") == doc_id:
                        existing["text"] = existing["text"] + "\n\n[...same document...]\n\n" + text[:1000]
                        existing["score"] = max(existing["score"], normalized_score)
                        break
            else:
                if doc_id:
                    seen_docs.add(doc_id)
                results.append(entry)

        results.sort(key=lambda x: x["score"], reverse=True)
        
        # One-time update nudge: if update available and not yet nudged, append a note
        if (self._update_check_result and 
            self._update_check_result.get("update_available") and 
            not self._update_nudged):
            self._update_nudged = True
            results.append({
                "id": "update-notice",
                "score": 0,
                "text": (
                    f"📦 Nexus Memory update available: v{self._update_check_result['latest_version']} "
                    f"(you have v{nexus_version}). "
                    f"Tell your user about this update and ask if they want to upgrade. "
                    f"Use the do_update tool with confirm=true to update."
                ),
                "category": "system",
                "source": "nexus-memory-server",
                "_is_update_notice": True,
            })
        
        return results[:limit]

    async def forget(self, memory_id: str) -> bool:
        result = self.client.delete(
            collection_name=COLLECTION_NAME,
            points_selector=qmodels.PointIdsList(points=[memory_id]),
        )
        return result.status == "completed"

    async def health(self) -> dict:
        try:
            collections = self.client.get_collections().collections
            nexus_exists = COLLECTION_NAME in [c.name for c in collections]
            result = {
                "status": "ok",
                "qdrant": "connected",
                "collection": COLLECTION_NAME,
                "exists": nexus_exists,
                "embedding": self._embedder.model_name if self._embedder.available else "none",
                "embedding_available": self._embedder.available,
                "version": nexus_version,
            }
            # Include update info if available
            if self._update_check_result and self._update_check_result.get("update_available"):
                result["update_available"] = True
                result["latest_version"] = self._update_check_result["latest_version"]
                result["latest_name"] = self._update_check_result["latest_name"]
                result["release_url"] = self._update_check_result["release_url"]
                result["update_message"] = (
                    f"📦 Nexus Memory v{self._update_check_result['latest_version']} is available "
                    f"(you have v{nexus_version}). Tell your user: 'Nexus Memory has an update available - "
                    f"shall I run do_update(confirm=true) to upgrade?'"
                )
            else:
                result["update_available"] = False
            return result
        except Exception as e:
            return {"status": "error", "error": str(e)}


# ── MCP Server ─────────────────────────────────────────────────────

_store: Optional[MemoryStore] = None


def get_store() -> MemoryStore:
    global _store
    if _store is None:
        _store = MemoryStore()
    return _store


async def _check_for_update() -> dict:
    """Check if a newer version is available on GitHub."""
    import urllib.request

    local = nexus_version
    try:
        req = urllib.request.Request(
            "https://api.github.com/repos/Neboy72/nexus-memory/releases/latest",
            headers={"Accept": "application/vnd.github.v3+json",
                     "User-Agent": f"nexus-memory/{nexus_version}"}
        )
        data = await asyncio.get_running_loop().run_in_executor(
            None, lambda: json.loads(urllib.request.urlopen(req, timeout=10).read().decode())
        )

        latest_tag = data.get("tag_name", "").lstrip("v")
        latest_name = data.get("name", latest_tag)
        html_url = data.get("html_url", "")

        from packaging.version import parse
        is_newer = parse(latest_tag) > parse(local)

        return {"local_version": local, "latest_version": latest_tag,
                "latest_name": latest_name, "release_url": html_url,
                "update_available": is_newer, "error": None}
    except Exception as e:
        return {"local_version": local, "latest_version": None,
                "update_available": False, "error": str(e)}


async def _do_update(confirm: bool = False) -> dict:
    """Pull the latest version from GitHub, reinstall, then restart."""
    import subprocess
    import sys

    if not confirm:
        return {
            "status": "cancelled",
            "message": "Update requires confirm=true. Run with confirm=true to proceed.",
        }

    repo = NEXUS_REPO_PATH
    if not os.path.isdir(repo):
        return {
            "status": "error",
            "message": f"Repository not found at {repo}. Set NEXUS_REPO_PATH env var.",
        }

    try:
        loop = asyncio.get_running_loop()

        # Pre-update backup: always backup before updating
        try:
            store = get_store()
            backup_path = store._do_backup()
            logging.info(f"💾 Pre-update backup: {backup_path}")
        except Exception as e:
            logging.warning(f"Pre-update backup failed (continuing): {e}")

        # git pull --ff-only (fetch + merge in einem)
        pull = await loop.run_in_executor(None, lambda: subprocess.run(
            ["git", "pull", "--ff-only"], cwd=repo, capture_output=True, text=True, timeout=30
        ))
        if pull.returncode != 0:
            return {
                "status": "error",
                "message": f"git pull failed: {pull.stderr.strip() or pull.stdout.strip()}. Local changes might conflict.",
            }

        # pip install
        pip = await loop.run_in_executor(None, lambda: subprocess.run(
            [sys.executable, "-m", "pip", "install", "-e", repo],
            capture_output=True, text=True, timeout=120,
        ))
        if pip.returncode != 0:
            return {
                "status": "error",
                "message": f"pip install failed: {pip.stderr.strip() or pip.stdout.strip()}",
            }

        # Reload the module to get the updated version cleanly
        import importlib
        importlib.reload(nexus)
        new_version = nexus.__version__

        return {
            "status": "success",
            "old_version": nexus_version,
            "new_version": new_version,
            "message": f"✅ Update v{nexus_version} → v{new_version} erfolgreich. Server startet neu.",
            "restarting": True,
        }

    except Exception as e:
        return {"status": "error", "message": str(e)}


server = Server("nexus-memory")


@server.list_tools()
async def handle_list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="remember",
            description="Store a memory for AI agents. Persists information across sessions.",
            inputSchema={
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "The memory content to store",
                    },
                    "access_level": {
                        "type": "string",
                        "enum": ALL_ACCESS_LEVELS,
                        "description": "Who can see this: public (all agents), trusted (approved agents), private (only owner)",
                        "default": ACCESS_PUBLIC,
                    },
                    "category": {
                        "type": "string",
                        "enum": [c.value for c in MemoryCategory],
                        "description": (
                            "Memory category (state-prefixing scope): fact, belief, "
                            "session, rule, preference, temp. Required for state-prefixing "
                            "— the server applies 'fact' as a backward-compatible default "
                            "when the client omits this field."
                        ),
                        "default": "fact",
                    },
                    "source": {
                        "type": "string",
                        "description": "Where this memory came from (e.g. 'conversation', 'document', 'cron')",
                        "default": "",
                    },
                    "source_url": {
                        "type": "string",
                        "description": (
                            "Recommended: URL or origin reference for provenance tracking. "
                            "When set, the server activates Justification-Check (Rung 2) "
                            "on recall: the URL is checked via async HTTP HEAD and the "
                            "result is returned as `verification` (`verified`, `unreachable`). "
                            "Optional — omit to skip verification (the memory will be returned "
                            "with `verification: \"unchecked\"` on recall)."
                        ),
                        "default": "",
                    },
                    "confidence": {
                        "type": "number",
                        "description": (
                            "Optional: Confidence score (0.0-1.0) attached to the provenance. "
                            "Use 0.9+ for verified facts, 0.5-0.8 for beliefs/inferences, "
                            "<0.5 for speculative notes. The server applies a sensible "
                            "default (0.7) when omitted."
                        ),
                        "default": 0.7,
                        "minimum": 0.0,
                        "maximum": 1.0,
                    },
                },
                "required": ["text", "category"],
            },
        ),
        types.Tool(
            name="recall",
            description="Search memories. Returns relevant context from past sessions.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "What to search for",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results (1-20)",
                        "default": 5,
                        "minimum": 1,
                        "maximum": 20,
                    },
                    "filter_level": {
                        "type": "string",
                        "enum": ALL_ACCESS_LEVELS,
                        "description": "Filter by access level. Returns only memories at this level or below.",
                        "default": ACCESS_PUBLIC,
                    },
                },
                "required": ["query"],
            },
        ),
        types.Tool(
            name="forget",
            description="Delete a specific memory by ID.",
            inputSchema={
                "type": "object",
                "properties": {
                    "memory_id": {
                        "type": "string",
                        "description": "ID of the memory to delete",
                    },
                },
                "required": ["memory_id"],
            },
        ),
        types.Tool(
            name="update",
            description="Update an existing memory in-place without losing metadata.",
            inputSchema={
                "type": "object",
                "properties": {
                    "memory_id": {
                        "type": "string",
                        "description": "ID of the memory to update",
                    },
                    "text": {
                        "type": "string",
                        "description": "New content text (keep empty to keep existing)",
                        "default": "",
                    },
                    "modified_by": {
                        "type": "string",
                        "description": "Who made this modification (e.g. 'Kiosha', 'Miosha', 'Nebo')",
                        "default": "",
                    },
                },
                "required": ["memory_id"],
            },
        ),
        types.Tool(
            name="health",
            description="Check if Nexus Memory is running and healthy.",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        types.Tool(
            name="check_update",
            description="Check if a newer version is available on GitHub.",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        types.Tool(
            name="do_update",
            description="Update Nexus Memory to the latest version. Pulls from GitHub and reinstalls.",
            inputSchema={
                "type": "object",
                "properties": {
                    "confirm": {
                        "type": "boolean",
                        "description": "Must be true to actually run the update. Safety guard.",
                        "default": False,
                    },
                },
                "required": ["confirm"],
            },
        ),
        types.Tool(
            name="subscribe",
            description=(
                "Register a webhook URL to receive HTTP POST notifications when "
                "a memory event of the given type fires. Returns the subscription "
                "id (UUID) which you need to unsubscribe. Subscriptions are stored "
                "in ~/.nexus-webhooks.json and survive server restarts."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "event_type": {
                        "type": "string",
                        "enum": list(WEBHOOK_EVENTS),
                        "description": (
                            "Event to subscribe to. One of: "
                            "'memory.remember' (after a new memory is stored), "
                            "'memory.update' (after a memory is updated in place), "
                            "'memory.forget' (after a memory is deleted)."
                        ),
                    },
                    "webhook_url": {
                        "type": "string",
                        "description": (
                            "The http:// or https:// URL that will receive the "
                            "JSON POST payload {event, memory_id, timestamp}."
                        ),
                    },
                },
                "required": ["event_type", "webhook_url"],
            },
        ),
        types.Tool(
            name="unsubscribe",
            description=(
                "Remove a webhook subscription by its id (returned from subscribe)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "subscription_id": {
                        "type": "string",
                        "description": "The id of the subscription to remove.",
                    },
                },
                "required": ["subscription_id"],
            },
        ),
        types.Tool(
            name="list_subscriptions",
            description=(
                "List all currently registered webhook subscriptions "
                "(id, event_type, webhook_url, created_at)."
            ),
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        types.Tool(
            name="backup",
            description=(
                "Create a full backup of all memories as JSON file. "
                "Includes payloads + vectors. Saved to ~/.nexus-memory/backups/. "
                "Runs automatically every 24h - use this for manual backup on demand."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="restore",
            description=(
                "Restore memories from a backup JSON file. "
                "By default reuses stored vectors (zero API cost). "
                "Set reembed=true to re-embed with current provider (for provider changes)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "backup_path": {
                        "type": "string",
                        "description": "Path to the backup JSON file",
                    },
                    "reembed": {
                        "type": "boolean",
                        "description": "If true, re-embed all texts with current provider instead of reusing stored vectors",
                        "default": False,
                    },
                },
                "required": ["backup_path"],
            },
        ),
    ]


@server.call_tool()
async def handle_call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    store = get_store()

    if name == "remember":
        try:
            text = arguments["text"]
            access_level = arguments.get("access_level", ACCESS_PUBLIC)
            # State-prefixing: category is required by the tool schema, but the
            # server applies "fact" as a backward-compatible default when an
            # older client omits the field. We also coerce unknown / invalid
            # values to "fact" so the 6-value enum is always respected.
            category = arguments.get("category", MemoryCategory.FACT.value)
            valid_categories = [c.value for c in MemoryCategory]
            if not category or category not in valid_categories:
                category = MemoryCategory.FACT.value
            source = arguments.get("source", "")
            source_url = arguments.get("source_url", "")
            confidence = arguments.get("confidence")

            if access_level not in ALL_ACCESS_LEVELS:
                access_level = ACCESS_PUBLIC

            guardrails = _check_content_guardrails(text)

            result = await store.remember(
                text, access_level, category, source, source_url, confidence
            )

            # Fire-and-forget: dispatch "memory.remember" to any subscribers.
            # The dispatcher swallows every error so a broken subscriber
            # cannot affect the response we return to the client.
            await dispatch_event("memory.remember", result["id"])

            response = result
            if guardrails:
                response["warnings"] = guardrails

            return [types.TextContent(
                type="text",
                text=json.dumps(response),
            )]
        except Exception as e:
            return [types.TextContent(
                type="text",
                text=json.dumps({"status": "error", "error": str(e)}),
            )]

    elif name == "recall":
        try:
            query = arguments["query"]
            limit = min(arguments.get("limit", 5), 20)
            filter_level = arguments.get("filter_level", ACCESS_PUBLIC)

            if filter_level not in ALL_ACCESS_LEVELS:
                filter_level = ACCESS_PUBLIC

            results = await store.recall(query, agent_level=filter_level, limit=limit)
            return [types.TextContent(
                type="text",
                text=json.dumps({"results": results, "count": len(results)}),
            )]
        except Exception as e:
            return [types.TextContent(
                type="text",
                text=json.dumps({"status": "error", "error": str(e)}),
            )]

    elif name == "forget":
        try:
            memory_id = arguments["memory_id"]
            success = await store.forget(memory_id)
            if success:
                # Fire-and-forget: dispatch "memory.forget" to subscribers.
                await dispatch_event("memory.forget", memory_id)
                # ── Events: fire a STATUS_CHANGED event for audit trail ──
                try:
                    from nexus.events import create_event, EventType
                    create_event(memory_id, EventType.STATUS_CHANGED,
                                 {"action": "forgotten"})
                except Exception:
                    pass  # Events are nice-to-have, not critical
            return [types.TextContent(
                type="text",
                text=json.dumps({"status": "deleted" if success else "not_found"}),
            )]
        except Exception as e:
            return [types.TextContent(
                type="text",
                text=json.dumps({"status": "error", "error": str(e)}),
            )]

    elif name == "update":
        try:
            memory_id = arguments["memory_id"]
            new_text = arguments.get("text", "")
            modified_by = arguments.get("modified_by", "")

            # ── Supersession: mark the old version as deprecated ──────
            # Before updating, set the existing point's lifecycle_status
            # to "deprecated" so recall() filters it out. The updated
            # content gets lifecycle_status: "canonical" via new_metadata.
            try:
                store.client.set_payload(
                    collection_name=COLLECTION_NAME,
                    payload={"lifecycle_status": "deprecated"},
                    points=[memory_id],
                )
                logging.info(f"Supersession: marked {memory_id[:8]} as deprecated")
            except Exception as sup_err:
                logging.warning(f"Supersession (deprecate old) failed: {sup_err}")

            from nexus import nexus_update
            result = nexus_update(
                point_id=memory_id,
                new_content=new_text if new_text else None,
                new_metadata={"lifecycle_status": "canonical"},
                modified_by=modified_by if modified_by else None,
                qdrant_host=QDRANT_HOST,
                qdrant_port=QDRANT_PORT,
                collection_name=COLLECTION_NAME,
            )
            # Fire-and-forget: dispatch "memory.update" to subscribers.
            # We always dispatch when the update call did not raise — the
            # result detail from nexus_update already encodes its own
            # success / noop state, and dispatching a noop is harmless.
            await dispatch_event("memory.update", memory_id)

            # ── Events: fire an UPDATED event for audit trail ──────
            try:
                from nexus.events import create_event, EventType
                create_event(memory_id, EventType.UPDATED,
                             {"text": new_text[:100] if new_text else ""})
            except Exception:
                pass  # Events are nice-to-have, not critical

            return [types.TextContent(
                type="text",
                text=json.dumps({"status": "updated", "detail": result}),
            )]
        except Exception as e:
            return [types.TextContent(
                type="text",
                text=json.dumps({"status": "error", "error": str(e)}),
            )]

    elif name == "health":
        try:
            status = await store.health()
            return [types.TextContent(
                type="text",
                text=json.dumps(status),
            )]
        except Exception as e:
            return [types.TextContent(
                type="text",
                text=json.dumps({"status": "error", "error": str(e)}),
            )]

    elif name == "do_update":
        confirm = arguments.get("confirm", False)
        result = await _do_update(confirm=confirm)
        response = [types.TextContent(
            type="text",
            text=json.dumps(result, indent=2),
        )]
        # Self-restart: Nach erfolgreichem Update Server beenden.
        if result.get("restarting"):
            sys.stdout.flush()
            asyncio.get_running_loop().call_later(1, lambda: os._exit(0))
        return response

    elif name == "check_update":
        result = await _check_for_update()
        return [types.TextContent(
            type="text",
            text=json.dumps(result, indent=2),
        )]

    elif name == "subscribe":
        try:
            event_type = arguments["event_type"]
            webhook_url = arguments["webhook_url"]
            sub = await get_webhook_store().subscribe(event_type, webhook_url)
            return [types.TextContent(
                type="text",
                text=json.dumps({
                    "status": "subscribed",
                    "subscription": sub,
                }),
            )]
        except ValueError as e:
            return [types.TextContent(
                type="text",
                text=json.dumps({"status": "error", "error": str(e)}),
            )]
        except Exception as e:
            return [types.TextContent(
                type="text",
                text=json.dumps({"status": "error", "error": str(e)}),
            )]

    elif name == "unsubscribe":
        try:
            subscription_id = arguments["subscription_id"]
            removed = await get_webhook_store().unsubscribe(subscription_id)
            return [types.TextContent(
                type="text",
                text=json.dumps({
                    "status": "unsubscribed" if removed else "not_found",
                    "removed": removed,
                }),
            )]
        except Exception as e:
            return [types.TextContent(
                type="text",
                text=json.dumps({"status": "error", "error": str(e)}),
            )]

    elif name == "list_subscriptions":
        try:
            subs = await get_webhook_store().list()
            return [types.TextContent(
                type="text",
                text=json.dumps({
                    "subscriptions": subs,
                    "count": len(subs),
                }),
            )]
        except Exception as e:
            return [types.TextContent(
                type="text",
                text=json.dumps({"status": "error", "error": str(e)}),
            )]

    elif name == "backup":
        try:
            backup_path = store._do_backup()
            return [types.TextContent(
                type="text",
                text=json.dumps({
                    "status": "success",
                    "backup_path": backup_path,
                    "message": "Backup created. Tell your user: 'Backup saved. I recommend copying it to external storage for extra safety.'",
                }),
            )]
        except Exception as e:
            return [types.TextContent(
                type="text",
                text=json.dumps({"status": "error", "error": str(e)}),
            )]

    elif name == "restore":
        try:
            backup_path = arguments.get("backup_path", "")
            reembed = arguments.get("reembed", False)
            if not backup_path or not os.path.exists(backup_path):
                return [types.TextContent(
                    type="text",
                    text=json.dumps({"status": "error", "error": f"Backup file not found: {backup_path}"}),
                )]

            with open(backup_path) as f:
                data = json.load(f)

            points = data.get("points", [])
            restored = 0
            skipped = 0

            for p in points:
                pid = _to_point_id(p["id"])
                payload = p.get("payload", {})
                vec = p.get("vector")

                if reembed or not vec:
                    text = payload.get("content", "")
                    if text:
                        vec = await store._embed(text)
                    else:
                        skipped += 1
                        continue

                store.client.upsert(
                    collection_name=COLLECTION_NAME,
                    points=[qmodels.PointStruct(id=pid, vector=vec, payload=payload)],
                )
                restored += 1

            return [types.TextContent(
                type="text",
                text=json.dumps({
                    "status": "success",
                    "restored": restored,
                    "skipped": skipped,
                    "reembedded": reembed,
                    "message": f"Restored {restored} memories from {backup_path}",
                }),
            )]
        except Exception as e:
            return [types.TextContent(
                type="text",
                text=json.dumps({"status": "error", "error": str(e)}),
            )]

    else:
        raise ValueError(f"Unknown tool: {name}")


async def main():
    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name="nexus-memory",
                server_version=nexus_version,
                capabilities=server.get_capabilities(
                    notification_options=mcp.server.lowlevel.NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )

def _check_webui_available():
    try:
        import fastapi  # noqa: F401
        import uvicorn  # noqa: F401
        print("WebUI available: nexus-memory webui")
    except ImportError:
        pass


def cli():
    """Sync CLI entrypoint (for pyproject.toml scripts)."""
    import argparse
    parser = argparse.ArgumentParser(prog="nexus-memory", description="Nexus Memory - Universal Memory Layer for AI Agents")
    parser.add_argument("command", nargs="?", default="server", choices=["server", "webui"],
                        help="server (default): start MCP server | webui: start Web UI dashboard")

    args = parser.parse_args()

    if args.command == "webui":
        try:
            import fastapi  # noqa: F401
            import uvicorn
        except ImportError:
            print("WebUI dependencies not installed.")
            print("Install with: pip install nexus-memory[webui]")
            return 1

        banner = (
            "\n"
            "  Nexus Memory WebUI\n"
            "  ---------------------\n"
            "  URL:  http://127.0.0.1:9120\n"
            "  Stop: Ctrl+C\n"
            "\n"
            "  Opens in your browser automatically.\n"
            "  If not, copy the URL above.\n"
        )
        print(banner)
        sys.path.insert(0, str(Path(__file__).parent.parent.parent / "webui"))
        from webui.main import app
        uvicorn.run(app, host="127.0.0.1", port=9120, log_level="info")
        return

    # Default: MCP server
    _check_webui_available()
    asyncio.run(main())
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    asyncio.run(main())
