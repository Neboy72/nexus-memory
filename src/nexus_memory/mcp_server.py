"""
nexus-memory — MCP Server (v0.2.0)
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
from nexus import MemoryCategory
from nexus.provenance import attach_source

# ── Auto-load .env files ──────────────────────────────────────────
# Load from NEXUS_ENV_FILE explicit path, then fall back to cwd/.env
env_paths = []
custom_env = os.environ.get("NEXUS_ENV_FILE")
if custom_env:
    env_paths.append(Path(custom_env))
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

# ── Embedding Provider ────────────────────────────────────────────

class EmbeddingProvider:
    """Auto-detect best embedding provider.
    
    Priority: Voyage (cloud, 1024d) → OpenAI (cloud, 1536d) → 
    Ollama (local, 768d) → sentence-transformers (local, 384d).
    """

    def __init__(self):
        self._name = "none"
        self._dim = 384
        self._client = None
        self._model = None
        self._detect()

    def _detect(self):
        """Detect best available embedding backend."""
        # 1. Voyage (cloud, best quality)
        if VOYAGE_API_KEY and (VOYAGE_API_KEY.startswith("vo-") or VOYAGE_API_KEY.startswith("pa-")):
            try:
                import voyageai
                self._client = voyageai.Client(api_key=VOYAGE_API_KEY)
                self._name = "voyage-3-large"
                self._dim = 1024
                logging.info(f"Embedding: {self._name} (1024d, cloud)")
                return
            except Exception:
                pass

        # 2. OpenAI (cloud)
        if OPENAI_API_KEY and OPENAI_API_KEY.startswith("sk-"):
            try:
                from openai import OpenAI
                self._client = OpenAI(api_key=OPENAI_API_KEY)
                self._name = "text-embedding-3-small"
                self._dim = 1536
                logging.info(f"Embedding: {self._name} (1536d, cloud)")
                return
            except Exception:
                pass


        # 3. Google / Vertex AI (cloud)
        if GOOGLE_API_KEY and GOOGLE_API_KEY.startswith("AIza"):
            try:
                import google.generativeai as genai
                genai.configure(api_key=GOOGLE_API_KEY)
                self._client = genai
                self._name = "text-embedding-004"
                self._dim = 768
                logging.info(f"Embedding: Google/{self._name} (768d, cloud)")
                return
            except Exception:
                pass


        # 4. Jina (cloud, best value)
        JINA_API_KEY = os.environ.get("JINA_API_KEY", "")
        if JINA_API_KEY:
            try:
                self._client = {"api_key": JINA_API_KEY, "base_url": "https://api.jina.ai/v1"}
                self._name = "jina-embeddings-v3"
                self._dim = 1024
                logging.info(f"Embedding: Jina/{self._name} (1024d, cloud)")
                return
            except Exception:
                pass

        # 5. Ollama (local service)
        try:
            import requests
            r = requests.get("http://localhost:11434/api/tags", timeout=2)
            if r.status_code == 200:
                models = [m["name"] for m in r.json().get("models", [])]
                emb_model = next((m for m in models if "embed" in m.lower()), None)
                if emb_model:
                    self._client = {"base_url": "http://localhost:11434"}
                    self._name = emb_model
                    self._dim = 768
                    logging.info(f"Embedding: Ollama/{emb_model} (768d, local)")
                    return
        except Exception:
            pass

        # 6. sentence-transformers (local, zero-setup fallback)
        try:
            from sentence_transformers import SentenceTransformer
            self._model = SentenceTransformer("all-MiniLM-L6-v2")
            self._name = "all-MiniLM-L6-v2"
            self._dim = 384
            logging.info(f"Embedding: {self._name} (384d, local)")
        except ImportError:
            logging.warning(
                "No embedding provider found.\n"
                "Install: pip install sentence-transformers  (local, free)\n"
                "Or set VOYAGE_API_KEY or OPENAI_API_KEY in ~/.hermes/.env"
            )

    async def embed(self, text: str) -> list[float]:
        if "voyage" in (self._name or ""):
            import voyageai
            result = await asyncio.to_thread(self._client.embed, [text], model=self._name)
            return result.embeddings[0]
        elif "text-embedding" in (self._name or ""):
            result = await asyncio.to_thread(
                self._client.embeddings.create,
                model=self._name, input=[text]
            )
            return result.data[0].embedding
        elif self._model:
            vector = await asyncio.to_thread(self._model.encode, text)
            return vector.tolist()
        elif "jina" in (self._name or ""):
            import requests as _req
            r = _req.post(
                f"{self._client['base_url']}/embeddings",
                json={"model": self._name, "input": [text]},
                headers={"Authorization": f"Bearer {self._client['api_key']}"},
                timeout=30,
            )
            return r.json()["data"][0]["embedding"]
        elif isinstance(self._client, dict):  # Ollama
            import requests as _req
            r = _req.post(
                f"{self._client['base_url']}/api/embeddings",
                json={"model": self._name, "prompt": text},
                timeout=30,
            )
            return r.json()["embedding"]

        elif "google" in str(type(self._client)).lower() or "generativeai" in str(type(self._client)).lower():
            result = self._client.embed_content(model=self._name, content=text)
            return result["embedding"]
        raise RuntimeError(
            f"No embedding provider available ({self._name}).\n"
            "Install: pip install sentence-transformers\n"
            "Or set VOYAGE_API_KEY or OPENAI_API_KEY"
        )

    @property
    def name(self) -> str: return self._name
    @property
    def dim(self) -> int: return self._dim
    @property
    def available(self) -> bool:
        return self._name != "none"

    @property
    def model_name(self) -> str:
        return self._name

# ── Access Levels ─────────────────────────────────────────────────

ACCESS_PUBLIC = "public"      # All agents can see
ACCESS_TRUSTED = "trusted"    # Only trusted agents
ACCESS_PRIVATE = "private"    # Only owner / explicitly permitted

ALL_ACCESS_LEVELS = [ACCESS_PUBLIC, ACCESS_TRUSTED, ACCESS_PRIVATE]
ACCESS_HIERARCHY = {
    ACCESS_PUBLIC: 0,
    ACCESS_TRUSTED: 1,
    ACCESS_PRIVATE: 2,
}

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
        self._ensure_collection()
        self._init_hybrid()

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
        try:
            # Try hybrid search first
            if self._hybrid_retriever:
                # Re-index periodically (every 50 calls or if collection changed)
                query_vector = await self._embed(query)
                h_results = self._hybrid_retriever.search(
                    query,
                    query_vector=query_vector,
                    top_k=limit * 2,  # Fetch extra for filtering
                )
                raw_results = h_results
                logging.debug(f"Hybrid search returned {len(raw_results)} results")
        except Exception as e:
            logging.warning(f"Hybrid search failed, falling back to vector: {e}")

        # Fallback: vector-only search
        if not raw_results:
            vector = await self._embed(query)
            response = self.client.query_points(
                collection_name=COLLECTION_NAME,
                query=vector,
                limit=limit * 2,
            )
            raw_results = []
            for point in response.points:
                payload = point.payload or {}
                raw_results.append({
                    "id": payload.get("id"),
                    "content": payload.get("content"),
                    "access_level": payload.get("access_level"),
                    "category": payload.get("category"),
                    "source": payload.get("source"),
                    "source_url": payload.get("source_url"),
                    "provenance": payload.get("provenance", {}),
                    "created_at": payload.get("created_at"),
                    "score": point.score,
                })

        # Normalize scores relative to max score in results
        # Handles both RRF scores (0.001-0.1) and Qdrant scores (0.0-1.0)
        max_raw = max((r.get("score", 0) for r in raw_results), default=1)
        max_raw = max(max_raw, 0.001)  # Avoid division by zero
        
        results = []
        seen_docs = set()
        for r in raw_results:
            mem_level = r.get("access_level", ACCESS_PUBLIC)
            if ACCESS_HIERARCHY.get(mem_level, 0) > ACCESS_HIERARCHY.get(agent_level, 0):
                continue
            
            doc_id = r.get("doc_id") or r.get("id")
            text = (r.get("content") or r.get("text") or "").strip()
            score = r.get("score", 0)
            
            # Normalize score relative to max in result set (handles RRF + Qdrant scales)
            normalized_score = round(score / max_raw, 3) if isinstance(score, (int, float)) else 0.0
            
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
            return {
                "status": "ok",
                "qdrant": "connected",
                "collection": COLLECTION_NAME,
                "exists": nexus_exists,
                "embedding": self._embedder.model_name if self._embedder.available else "none",
                "embedding_available": self._embedder.available,
            }
        except Exception as e:
            return {"status": "error", "error": str(e)}


# ── MCP Server ─────────────────────────────────────────────────────

_store: Optional[MemoryStore] = None


def get_store() -> MemoryStore:
    global _store
    if _store is None:
        _store = MemoryStore()
    return _store


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
                        "description": "Memory category: fact, belief, session, rule, preference, temp",
                        "default": "fact",
                    },
                    "source": {
                        "type": "string",
                        "description": "Where this memory came from (e.g. 'conversation', 'document', 'cron')",
                        "default": "",
                    },
                    "source_url": {
                        "type": "string",
                        "description": "URL or origin reference for provenance tracking",
                        "default": "",
                    },
                    "confidence": {
                        "type": "number",
                        "description": "Confidence score (0.0-1.0) for provenance",
                        "default": 0.7,
                        "minimum": 0.0,
                        "maximum": 1.0,
                    },
                },
                "required": ["text"],
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
    ]


@server.call_tool()
async def handle_call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    store = get_store()

    if name == "remember":
        text = arguments["text"]
        access_level = arguments.get("access_level", ACCESS_PUBLIC)
        category = arguments.get("category", MemoryCategory.FACT.value)
        source = arguments.get("source", "")
        source_url = arguments.get("source_url", "")
        confidence = arguments.get("confidence")

        if access_level not in ALL_ACCESS_LEVELS:
            access_level = ACCESS_PUBLIC

        # Guardrails check (v2.8.0 feature)
        guardrails = _check_content_guardrails(text)

        result = await store.remember(
            text, access_level, category, source, source_url, confidence
        )

        response = result
        if guardrails:
            response["warnings"] = guardrails

        return [types.TextContent(
            type="text",
            text=json.dumps(response),
        )]

    elif name == "recall":
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

    elif name == "forget":
        memory_id = arguments["memory_id"]
        success = await store.forget(memory_id)
        return [types.TextContent(
            type="text",
            text=json.dumps({"status": "deleted" if success else "not_found"}),
        )]

    elif name == "update":
        memory_id = arguments["memory_id"]
        new_text = arguments.get("text", "")
        modified_by = arguments.get("modified_by", "")

        from nexus import nexus_update
        result = nexus_update(
            point_id=memory_id,
            new_content=new_text if new_text else None,
            modified_by=modified_by if modified_by else None,
            qdrant_host=QDRANT_HOST,
            qdrant_port=QDRANT_PORT,
            collection_name=COLLECTION_NAME,
        )
        return [types.TextContent(
            type="text",
            text=json.dumps({"status": "updated", "detail": result}),
        )]

    elif name == "health":
        status = await store.health()
        return [types.TextContent(
            type="text",
            text=json.dumps(status),
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
                server_version="0.2.0",
                capabilities=server.get_capabilities(
                    notification_options=mcp.server.lowlevel.NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )

def cli():
    """Sync CLI entrypoint (for pyproject.toml scripts)."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    asyncio.run(main())


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    asyncio.run(main())
