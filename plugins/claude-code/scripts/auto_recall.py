#!/usr/bin/env python3
"""Nexus Memory Auto-Recall Hook for Claude Code.

Fires on UserPromptSubmit. Reads the user's prompt from stdin,
searches Qdrant for relevant memories, and injects them as
additional context for Claude to use.

Output JSON with additionalContext field injects text into Claude's context.
"""

import sys
import json
import os
import urllib.request
import urllib.error
from pathlib import Path

# Config
QDRANT_URL = os.getenv("NEXUS_QDRANT_URL", "http://localhost:6333")
COLLECTION = os.getenv("NEXUS_COLLECTION", "nexus")
EMBEDDING_PROVIDER = os.getenv("NEXUS_EMBEDDING_PROVIDER", "voyage")
VOYAGE_API_KEY = os.getenv("VOYAGE_API_KEY", "")
EMBEDDING_MODEL = os.getenv("NEXUS_EMBEDDING_MODEL", "voyage-3-large")
MAX_RESULTS = int(os.getenv("NEXUS_MAX_RECALL", "5"))
AGENTS_FILE = Path.home() / ".nexus-memory" / "agents.json"

def _resolve_trust_level() -> str:
    """Gatekeeper: resolve this agent's trust level from agents.json.

    Uses NEXUS_AGENT_ID env var to identify the caller.
    Falls back to 'public' for unknown agents (safest default).
    """
    agent_id = os.getenv("NEXUS_AGENT_ID", "claude-code")
    if not agent_id:
        return "public"

    try:
        with open(AGENTS_FILE) as f:
            registry = json.load(f)
        for agent in registry.get("agents", []):
            if agent.get("id") == agent_id:
                trust = agent.get("trust_level", "public")
                if trust in ("public", "trusted", "private"):
                    return trust
                return "public"
        return "public"  # Agent not found in registry
    except Exception:
        return "public"

def _get_trust_filter() -> dict:
    """Return Qdrant filter for the resolved trust level.

    Hierarchy: public (0) < trusted (1) < private (2).
    An agent at level N can see memories at levels 0..N.
    """
    trust_level = _resolve_trust_level()
    level_order = ["public", "trusted", "private"]
    idx = level_order.index(trust_level) if trust_level in level_order else 0
    allowed = level_order[:idx + 1]

    return {
        "should": [
            {"key": "access_level", "match": {"value": lvl}}
            for lvl in allowed
        ]
    }

def get_embedding(text: str) -> list:
    """Get embedding from configured provider."""
    if EMBEDDING_PROVIDER == "voyage" and VOYAGE_API_KEY:
        req_data = json.dumps({
            "input": [text],
            "model": EMBEDDING_MODEL,
            "input_type": "document"
        }).encode()
        req = urllib.request.Request(
            "https://api.voyageai.com/v1/embeddings",
            data=req_data,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {VOYAGE_API_KEY}"
            }
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            return data["data"][0]["embedding"]
    elif EMBEDDING_PROVIDER == "ollama":
        req_data = json.dumps({
            "model": os.getenv("NEXUS_OLLAMA_EMBED_MODEL", "nomic-embed-text"),
            "input": text
        }).encode()
        req = urllib.request.Request(
            f"http://localhost:11434/api/embeddings",
            data=req_data,
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            return data["embedding"]
    else:
        # Fallback: no embedding, skip recall
        return None

def search_qdrant(query_embedding: list, limit: int = 5) -> list:
    """Search Qdrant for relevant memories, filtered by trust level.

    Over-fetches (limit * 8) then filters client-side for access level,
    matching the MCP server gatekeeper behavior.
    """
    trust_level = _resolve_trust_level()
    level_order = ["public", "trusted", "private"]
    agent_idx = level_order.index(trust_level) if trust_level in level_order else 0

    fetch_n = limit * 8
    search_data = json.dumps({
        "vector": query_embedding,
        "limit": fetch_n,
        "with_payload": True,
        "score_threshold": 0.3,
        "filter": _get_trust_filter()
    }).encode()
    req = urllib.request.Request(
        f"{QDRANT_URL}/collections/{COLLECTION}/points/search",
        data=search_data,
        headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
            results = data.get("result", [])
    except Exception:
        return []

    # Client-side filter: only return memories the agent is allowed to see
    filtered = []
    for hit in results:
        payload = hit.get("payload", {})
        mem_level = payload.get("access_level", "private")
        mem_idx = level_order.index(mem_level) if mem_level in level_order else 2
        if mem_idx <= agent_idx:
            filtered.append(hit)
        if len(filtered) >= limit:
            break

    return filtered

def main():
    # Read hook input from stdin
    try:
        hook_input = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    prompt = hook_input.get("prompt", "")
    if not prompt or len(prompt) < 10:
        sys.exit(0)

    # Get embedding
    embedding = get_embedding(prompt)
    if not embedding:
        sys.exit(0)

    # Search Qdrant
    results = search_qdrant(embedding, MAX_RESULTS)
    if not results:
        sys.exit(0)

    # Build context block
    memories = []
    for hit in results:
        payload = hit.get("payload", {})
        text = payload.get("text") or payload.get("content", "")
        category = payload.get("category", "fact")
        score = hit.get("score", 0)
        if text and score > 0.3:
            memories.append(f"[{category}] (score: {score:.2f}) {text[:200]}")

    if not memories:
        sys.exit(0)

    context = "\n--- Nexus Memory (Auto-Recall) ---\n"
    context += f"Found {len(memories)} relevant memories:\n\n"
    context += "\n\n".join(memories)
    context += "\n--- End Nexus Memory ---\n"

    # Output: inject context into Claude
    output = {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": context
        }
    }
    print(json.dumps(output))

if __name__ == "__main__":
    main()