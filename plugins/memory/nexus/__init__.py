"""NexusMemoryProvider — Hermes Agent MemoryProvider plugin for Nexus Memory.

Speaks directly to Qdrant (via qdrant_client), reusing the same "nexus" collection
and embedding logic as the MCP server so all agents share the same memory.
"""

from __future__ import annotations
import json, logging, os, threading, time, uuid
from typing import Any, Dict, List, Optional
from qdrant_client import QdrantClient
from qdrant_client.http import models as qmodels

logger = logging.getLogger(__name__)
_HOST = os.environ.get("NEXUS_QDRANT_HOST", "localhost")
_PORT = int(os.environ.get("NEXUS_QDRANT_PORT", "6333"))
_COLLECTION = os.environ.get("NEXUS_COLLECTION", "nexus")

# Tool schemas (OpenAI function-calling format)
RECALL_SCHEMA = {"name": "nexus_recall", "description": "Search Nexus Memory for relevant past memories, facts, or context.", "parameters": {"type": "object", "properties": {"query": {"type": "string", "description": "What to search for."}, "limit": {"type": "integer", "description": "Max results (default 5).", "default": 5}}, "required": ["query"]}}
REMEMBER_SCHEMA = {"name": "nexus_remember", "description": "Store a memory in Nexus Memory for future recall across all agents.", "parameters": {"type": "object", "properties": {"text": {"type": "string", "description": "The memory content to store."}, "category": {"type": "string", "description": "Memory category: fact, belief, session, rule, preference, temp.", "default": "fact"}, "access_level": {"type": "string", "description": "Visibility: public, trusted, private.", "default": "public"}, "source": {"type": "string", "description": "Where this memory came from.", "default": ""}, "source_url": {"type": "string", "description": "URL for verification (optional).", "default": ""}, "confidence": {"type": "number", "description": "Confidence score 0.0-1.0.", "default": 0.7}}, "required": ["text"]}}
FORGET_SCHEMA = {"name": "nexus_forget", "description": "Delete a memory from Nexus Memory by ID.", "parameters": {"type": "object", "properties": {"memory_id": {"type": "string", "description": "The memory ID to delete."}}, "required": ["memory_id"]}}


class _Embedder:
    """Auto-detect embedding provider — reuses the shared EmbeddingProvider.

    Priority: Voyage (1024d) → OpenAI (1536d) → Google (768d) → Jina (1024d)
    → Ollama (768d) → sentence-transformers (384d). Same logic as the MCP
    server so both paths produce compatible vectors for the same collection.
    """
    def __init__(self) -> None:
        self._impl: Any = None
        try:
            from nexus_memory.embeddings import EmbeddingProvider
            self._impl = EmbeddingProvider()
            logger.info("Nexus plugin embedder: %s (%dd)", self._impl.model_name, self._impl.dim)
        except Exception as exc:
            raise RuntimeError(f"Could not init embedding provider: {exc}")

    def embed(self, text: str) -> List[float]:
        import asyncio
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(self._impl.embed(text))
        finally:
            loop.close()

    @property
    def dim(self) -> int: return self._impl.dim


class NexusMemoryProvider:
    """MemoryProvider backed by Nexus Memory + Qdrant. Shares collection with MCP server."""

    def __init__(self) -> None:
        self._session_id = ""; self._hermes_home = ""; self._agent_context = "primary"
        self._qdrant: Optional[QdrantClient] = None; self._embedder: Optional[_Embedder] = None
        self._collection = _COLLECTION; self._prefetch_result = ""
        self._prefetch_lock = threading.Lock(); self._write_queue: List[Dict[str, Any]] = []
        self._write_lock = threading.Lock(); self._write_stop = threading.Event()
        self._write_thread: Optional[threading.Thread] = None

    @property
    def name(self) -> str: return "nexus"

    def is_available(self) -> bool:
        try:
            import qdrant_client  # noqa: F401
            from nexus_memory.embeddings import EmbeddingProvider  # noqa: F401
            c = QdrantClient(host=_HOST, port=_PORT)
            c.get_collections(); c.close(); return True
        except Exception: return False

    def initialize(self, session_id: str, **kwargs: Any) -> None:
        self._session_id = session_id; self._hermes_home = kwargs.get("hermes_home", "")
        self._agent_context = kwargs.get("agent_context", "primary")
        cfg = self._load_config(); self._collection = cfg.get("collection_name", _COLLECTION)
        self._qdrant = QdrantClient(host=_HOST, port=_PORT)
        self._embedder = _Embedder()
        self._ensure_collection()
        self._check_dimension_compat()
        self._write_stop.clear()
        self._write_thread = threading.Thread(target=self._write_loop, name="nexus-writer", daemon=True)
        self._write_thread.start()
        logger.info("NexusMemoryProvider init (collection=%s, dim=%d)", self._collection, self._embedder.dim)

    def system_prompt_block(self) -> str:
        return "Nexus Memory active. Relevant memories are automatically injected. Use nexus_recall to search manually, nexus_remember to store."

    def shutdown(self) -> None:
        self._write_stop.set()
        if self._write_thread and self._write_thread.is_alive():
            self._write_thread.join(timeout=5.0)
        if self._qdrant: self._qdrant.close(); self._qdrant = None
        logger.info("NexusMemoryProvider shut down")

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        with self._prefetch_lock: return self._prefetch_result

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        threading.Thread(target=self._do_prefetch, args=(query,), name="nexus-prefetch", daemon=True).start()

    def _do_prefetch(self, query: str) -> None:
        if not self._embedder or not self._qdrant: return
        try:
            vector = self._embedder.embed(query)
            pts = self._qdrant.query_points(collection_name=self._collection, query=vector, limit=5).points
            items: List[str] = []
            for p in pts:
                pl = p.payload or {}; text = pl.get("content", "")
                if text:
                    items.append(f"[{pl.get('category','fact')}] score={p.score or 0:.2f}: {text[:500]}")
            with self._prefetch_lock: self._prefetch_result = "\n".join(items) if items else ""
        except Exception as exc:
            logger.warning("Prefetch failed: %s", exc)
            with self._prefetch_lock: self._prefetch_result = ""

    def sync_turn(self, user_content: str, assistant_content: str, *, session_id: str = "",
                  messages: Optional[List[Dict[str, Any]]] = None) -> None:
        if self._agent_context != "primary": return
        with self._write_lock:
            self._write_queue.append({"text": f"User: {user_content}\nAssistant: {assistant_content}",
                                       "category": "session", "access_level": "public",
                                       "source": "hermes-plugin", "confidence": 0.5})

    def _write_loop(self) -> None:
        while not self._write_stop.is_set():
            entry = None
            with self._write_lock:
                if self._write_queue: entry = self._write_queue.pop(0)
            if entry and self._embedder and self._qdrant:
                try: self._upsert(**entry)
                except Exception as exc: logger.warning("Background write failed: %s", exc)
            else: time.sleep(0.5)

    def _upsert(self, text: str, category: str = "fact", access_level: str = "public",
                source: str = "", confidence: float = 0.7, **_: Any) -> Dict[str, Any]:
        if not self._embedder or not self._qdrant: raise RuntimeError("Provider not initialized")
        eid = str(uuid.uuid4()); ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        vector = self._embedder.embed(text)
        payload = {"id": eid, "content": text, "access_level": access_level, "category": category,
                    "source": source, "source_url": "", "created_at": ts,
                    "provenance": {"source_type": "hermes-plugin", "created_by": "nexus-memory-provider",
                                   "timestamp": ts, "confidence": confidence}}
        self._qdrant.upsert(collection_name=self._collection,
                            points=[qmodels.PointStruct(id=eid, vector=vector, payload=payload)])
        return {"status": "ok", "id": eid, "category": category}

    def _recall(self, query: str, limit: int = 5) -> List[Dict[str, Any]]:
        if not self._embedder or not self._qdrant: return []
        vector = self._embedder.embed(query)
        pts = self._qdrant.query_points(collection_name=self._collection, query=vector, limit=max(limit, 1)).points
        results: List[Dict[str, Any]] = []
        for p in pts:
            pl = p.payload or {}
            results.append({"id": pl.get("id"), "text": (pl.get("content") or "")[:2000],
                            "score": round(float(p.score or 0.0), 3), "source": pl.get("source"),
                            "source_url": pl.get("source_url"), "access_level": pl.get("access_level"),
                            "category": pl.get("category", "fact"),
                            "confidence": (pl.get("provenance") or {}).get("confidence"),
                            "created_at": pl.get("created_at")})
        results.sort(key=lambda r: r["score"], reverse=True); return results[:limit]

    def _forget(self, memory_id: str) -> Dict[str, Any]:
        if not self._qdrant: raise RuntimeError("Provider not initialized")
        self._qdrant.delete(collection_name=self._collection,
                            points_selector=qmodels.PointIdsList(points=[memory_id]))
        return {"status": "ok", "id": memory_id}

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return [RECALL_SCHEMA, REMEMBER_SCHEMA, FORGET_SCHEMA]

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs: Any) -> str:
        try:
            if tool_name == "nexus_recall":
                result = self._recall(args.get("query", ""), args.get("limit", 5))
            elif tool_name == "nexus_remember":
                result = self._upsert(text=args.get("text", ""), category=args.get("category", "fact"),
                                      access_level=args.get("access_level", "public"),
                                      source=args.get("source", ""))
            elif tool_name == "nexus_forget":
                result = self._forget(args.get("memory_id", ""))
            else: return json.dumps({"error": f"Unknown tool: {tool_name}"})
            return json.dumps(result)
        except Exception as exc:
            logger.warning("Tool call %s failed: %s", tool_name, exc)
            return json.dumps({"error": str(exc)})

    def get_config_schema(self) -> List[Dict[str, Any]]:
        return [
            {"key": "qdrant_url", "description": "Qdrant server URL", "secret": False,
             "required": False, "default": f"http://{_HOST}:{_PORT}"},
            {"key": "voyage_api_key", "description": "Voyage AI API key (1024d cloud embeddings). Optional - auto-detects OpenAI, Google, Jina, Ollama, or sentence-transformers if not set.",
             "secret": True, "required": False, "env_var": "VOYAGE_API_KEY", "url": "https://docs.voyageai.com"},
            {"key": "collection_name", "description": "Qdrant collection name", "secret": False,
             "required": False, "default": _COLLECTION},
        ]

    def save_config(self, values: Dict[str, Any], hermes_home: str) -> None:
        d = os.path.join(hermes_home, "nexus"); os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "config.json"), "w") as f:
            json.dump({"qdrant_url": values.get("qdrant_url", ""),
                        "collection_name": values.get("collection_name", _COLLECTION)}, f, indent=2)
        logger.info("Nexus config saved to %s/nexus/config.json", hermes_home)

    def on_memory_write(self, action: str, target: str, content: str,
                        metadata: Optional[Dict[str, Any]] = None) -> None:
        if action in ("add", "replace") and content:
            try: self._upsert(text=content, category=(metadata or {}).get("category", "fact"),
                              access_level="public", source="hermes-builtin")
            except Exception as exc: logger.warning("on_memory_write mirror failed: %s", exc)

    def _ensure_collection(self) -> None:
        if not self._qdrant or not self._embedder: return
        cols = [c.name for c in self._qdrant.get_collections().collections]
        if self._collection not in cols:
            self._qdrant.create_collection(
                collection_name=self._collection,
                vectors_config=qmodels.VectorParams(size=self._embedder.dim, distance=qmodels.Distance.COSINE))
            self._qdrant.create_payload_index(
                collection_name=self._collection, field_name="access_level",
                field_type=qmodels.PayloadSchemaType.KEYWORD)
            logger.info("Created collection '%s' (%dd)", self._collection, self._embedder.dim)

    def _check_dimension_compat(self) -> None:
        """Warn if the current embedder dimension doesn't match an existing collection.

        Qdrant rejects upserts/query_points when the vector size doesn't match
        the collection's configured size. This happens when a user switches
        embedding providers (e.g. sentence-transformers 384d → Voyage 1024d)
        without creating a new collection. We log a clear warning instead of
        crashing so the user can fix it (delete + recreate the collection).
        """
        if not self._qdrant or not self._embedder: return
        try:
            info = self._qdrant.get_collection(self._collection)
            existing_dim = info.config.params.vectors.size
            if existing_dim is not None and existing_dim != self._embedder.dim:
                logger.warning(
                    "Nexus dimension mismatch! Collection '%s' has %dd vectors but "
                    "current embedder '%s' produces %dd. Memories cannot be stored "
                    "or searched. Delete the collection and restart to fix: "
                    "curl -X DELETE http://%s:%d/collections/%s",
                    self._collection, existing_dim, self._embedder._impl.model_name,
                    self._embedder.dim, _HOST, _PORT, self._collection,
                )
        except Exception:
            pass  # Collection might not exist yet, _ensure_collection handles that

    def _load_config(self) -> Dict[str, Any]:
        if not self._hermes_home: return {}
        try:
            with open(os.path.join(self._hermes_home, "nexus", "config.json")) as f: return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError): return {}


def register(ctx: Any) -> None:
    ctx.register_memory_provider(NexusMemoryProvider())
