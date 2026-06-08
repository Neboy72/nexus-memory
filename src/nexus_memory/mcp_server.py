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
import nexus
from nexus import MemoryCategory, __version__ as nexus_version
from nexus.provenance import attach_source

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

                # Enrich hybrid results with payload fields (source_url, access_level, etc.)
                if raw_results:
                    hybrid_ids_raw = [r.get("id") for r in raw_results if r.get("id") is not None]
                    if hybrid_ids_raw:
                        try:
                            def _to_point_id(val):
                                if isinstance(val, int):
                                    return val
                                if isinstance(val, str):
                                    # UUID string
                                    if "-" in val and len(val) == 36:
                                        from uuid import UUID
                                        return UUID(val)
                                    # Numeric string → int
                                    try:
                                        return int(val)
                                    except ValueError:
                                        return val
                                return val
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
                                    if not r.get("category"):
                                        r["category"] = pl.get("category")
                                    if not r.get("source"):
                                        r["source"] = pl.get("source")
                                    if not r.get("created_at"):
                                        r["created_at"] = pl.get("created_at")
                                    if not r.get("provenance"):
                                        r["provenance"] = pl.get("provenance", {})
                        except Exception as enrich_err:
                            logging.warning(f"Payload enrichment failed: {enrich_err}")
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
        loop = asyncio.get_event_loop()
        data = await loop.run_in_executor(None, lambda: (
            json.loads(urllib.request.urlopen(req, timeout=10).read().decode())
        ))
        
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
        loop = asyncio.get_event_loop()
        
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

    elif name == "do_update":
        confirm = arguments.get("confirm", False)
        result = await _do_update(confirm=confirm)
        response = [types.TextContent(
            type="text",
            text=json.dumps(result, indent=2),
        )]
        # Self-restart: Nach erfolgreichem Update Server beenden.
        if result.get("restarting"):
            import sys as _sys
            import asyncio as _asyncio
            _sys.stdout.flush()
            _asyncio.get_event_loop().call_later(1, lambda: os._exit(0))
        return response

    elif name == "check_update":
        result = await _check_for_update()
        return [types.TextContent(
            type="text",
            text=json.dumps(result, indent=2),
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

def cli():
    """Sync CLI entrypoint (for pyproject.toml scripts)."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    asyncio.run(main())


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    asyncio.run(main())
