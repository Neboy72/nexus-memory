"""
nexus-memory — MCP Server
Universal Memory Layer for AI Agents
"""

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Optional
from pathlib import Path

import mcp.server.stdio
import mcp.types as types
import voyageai
from mcp.server import Server
from mcp.server.models import InitializationOptions
from qdrant_client import QdrantClient
from qdrant_client.http import models as qmodels

# ── Auto-load .env files ──────────────────────────────────────────
for env_path in [Path.home() / ".hermes" / ".env", Path.cwd() / ".env"]:
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

# ── Config ──────────────────────────────────────────────────────────────

QDRANT_HOST = os.environ.get("NEXUS_QDRANT_HOST", "localhost")
QDRANT_PORT = int(os.environ.get("NEXUS_QDRANT_PORT", "6333"))
COLLECTION_NAME = os.environ.get("NEXUS_COLLECTION", "nexus")
VOYAGE_MODEL = os.environ.get("NEXUS_VOYAGE_MODEL", "voyage-3-large")
EMBEDDING_DIM = int(os.environ.get("NEXUS_EMBEDDING_DIM", "1024"))
VOYAGE_API_KEY = os.environ.get("VOYAGE_API_KEY", "")

# ── Access Levels ──────────────────────────────────────────────────────

ACCESS_PUBLIC = "public"     # All agents can see
ACCESS_TRUSTED = "trusted"   # Only trusted agents
ACCESS_PRIVATE = "private"   # Only owner / explicitly permitted

ALL_ACCESS_LEVELS = [ACCESS_PUBLIC, ACCESS_TRUSTED, ACCESS_PRIVATE]

ACCESS_HIERARCHY = {
    ACCESS_PUBLIC: 0,
    ACCESS_TRUSTED: 1,
    ACCESS_PRIVATE: 2,
}

# ── Dataclasses ────────────────────────────────────────────────────────

@dataclass
class AgentIdentity:
    """Who is asking"""
    agent_id: str
    access_level: str = ACCESS_PUBLIC

    def can_access(self, memory_level: str) -> bool:
        """Can this agent see a memory with the given access_level?"""
        return ACCESS_HIERARCHY.get(self.access_level, 0) >= ACCESS_HIERARCHY.get(memory_level, 0)


@dataclass
class MemoryEntry:
    """A single memory entry"""
    id: str
    text: str
    access_level: str = ACCESS_PUBLIC
    category: str = "fact"
    source: str = ""
    created_at: str = ""
    metadata: dict = field(default_factory=dict)

# ── Storage ─────────────────────────────────────────────────────────────

class MemoryStore:
    """Qdrant-backed storage for memories"""

    def __init__(self):
        self.client = QdrantClient(host=QDRANT_HOST, port=QDRANT_PORT)
        self.vo = voyageai.Client(api_key=VOYAGE_API_KEY) if VOYAGE_API_KEY else None
        self._ensure_collection()

    def _ensure_collection(self):
        """Create collection if it doesn't exist"""
        collections = [c.name for c in self.client.get_collections().collections]
        if COLLECTION_NAME not in collections:
            self.client.create_collection(
                collection_name=COLLECTION_NAME,
                vectors_config=qmodels.VectorParams(
                    size=EMBEDDING_DIM,
                    distance=qmodels.Distance.COSINE,
                ),
            )
            # Create payload index for filtering
            self.client.create_payload_index(
                collection_name=COLLECTION_NAME,
                field_name="access_level",
                field_type=qmodels.PayloadSchemaType.KEYWORD,
            )
            logging.info(f"Created collection '{COLLECTION_NAME}' ({EMBEDDING_DIM}d)")

    async def _embed(self, text: str) -> list[float]:
        """Get embedding vector for text"""
        if self.vo:
            result = await asyncio.to_thread(
                self.vo.embed, [text], model=VOYAGE_MODEL
            )
            return result.embeddings[0]
        else:
            raise RuntimeError("VOYAGE_API_KEY not set — cannot generate embeddings")

    async def remember(self, text: str, access_level: str = ACCESS_PUBLIC,
                       category: str = "fact", source: str = "",
                       metadata: Optional[dict] = None) -> str:
        """Store a memory and return its ID"""
        import uuid
        from datetime import datetime, timezone

        entry_id = str(uuid.uuid4())
        created_at = datetime.now(timezone.utc).isoformat()

        vector = await self._embed(text)

        payload = {
            "id": entry_id,
            "text": text,
            "access_level": access_level,
            "category": category,
            "source": source,
            "created_at": created_at,
            "metadata": json.dumps(metadata or {}),
        }

        self.client.upsert(
            collection_name=COLLECTION_NAME,
            points=[qmodels.PointStruct(
                id=entry_id,
                vector=vector,
                payload=payload,
            )],
        )
        logging.info(f"Stored memory {entry_id} [{access_level}]")
        return entry_id

    async def recall(self, query: str, agent_level: str = ACCESS_PUBLIC,
                     limit: int = 5) -> list[dict]:
        """Search memories, filtered by what the agent can see"""
        vector = await self._embed(query)

        allowed_levels = [ACCESS_PUBLIC]
        if ACCESS_HIERARCHY.get(agent_level, 0) >= 1:
            allowed_levels.append(ACCESS_TRUSTED)
        if ACCESS_HIERARCHY.get(agent_level, 0) >= 2:
            allowed_levels.append(ACCESS_PRIVATE)

        response = self.client.query_points(
            collection_name=COLLECTION_NAME,
            query=vector,
            limit=limit,
            query_filter=qmodels.Filter(
                must=[
                    qmodels.FieldCondition(
                        key="access_level",
                        match=qmodels.MatchAny(any=allowed_levels),
                    ),
                ],
            ),
        )

        memories = []
        for point in response.points:
            payload = point.payload
            memories.append({
                "id": payload.get("id"),
                "text": payload.get("text"),
                "access_level": payload.get("access_level"),
                "category": payload.get("category"),
                "source": payload.get("source"),
                "created_at": payload.get("created_at"),
                "score": point.score,
            })
        return memories

    async def forget(self, memory_id: str) -> bool:
        """Delete a memory by ID"""
        result = self.client.delete(
            collection_name=COLLECTION_NAME,
            points_selector=qmodels.PointIdsList(
                points=[memory_id],
            ),
        )
        return result.status == "completed"

    async def health(self) -> dict:
        """Check system health"""
        try:
            collections = self.client.get_collections().collections
            nexus_exists = COLLECTION_NAME in [c.name for c in collections]
            return {
                "status": "ok",
                "qdrant": "connected",
                "collection": COLLECTION_NAME,
                "exists": nexus_exists,
                "voyage": self.vo is not None,
            }
        except Exception as e:
            return {"status": "error", "error": str(e)}


# ── MCP Server ─────────────────────────────────────────────────────────

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
                        "description": "Category: fact, preference, session, rule, etc.",
                        "default": "fact",
                    },
                    "source": {
                        "type": "string",
                        "description": "Where this memory came from (e.g. 'conversation', 'document', 'cron')",
                        "default": "",
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
        category = arguments.get("category", "fact")
        source = arguments.get("source", "")

        if access_level not in ALL_ACCESS_LEVELS:
            access_level = ACCESS_PUBLIC

        memory_id = await store.remember(text, access_level, category, source)
        return [types.TextContent(
            type="text",
            text=json.dumps({
                "status": "ok",
                "id": memory_id,
                "access_level": access_level,
            }),
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
                server_version="0.1.0",
                capabilities=server.get_capabilities(
                    notification_options=mcp.server.lowlevel.NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    asyncio.run(main())
