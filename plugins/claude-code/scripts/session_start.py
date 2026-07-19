#!/usr/bin/env python3
"""Nexus Memory Session Start Hook for Claude Code.

Fires on SessionStart. Loads recent session context and project
memories from Qdrant to give Claude immediate context.
"""

import sys
import json
import os
import urllib.request
from pathlib import Path

QDRANT_URL = os.getenv("NEXUS_QDRANT_URL", "http://localhost:6333")
COLLECTION = os.getenv("NEXUS_COLLECTION", "nexus")
VOYAGE_API_KEY = os.getenv("VOYAGE_API_KEY", "")
EMBEDDING_MODEL = os.getenv("NEXUS_EMBEDDING_MODEL", "voyage-3-large")
EMBEDDING_PROVIDER = os.getenv("NEXUS_EMBEDDING_PROVIDER", "voyage")
AGENTS_FILE = Path.home() / ".nexus-memory" / "agents.json"

def _resolve_trust_level() -> str:
    """Gatekeeper: resolve this agent's trust level from agents.json."""
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

def get_embedding(text: str) -> list:
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
    return None

def _get_trust_filter() -> dict:
    """Return Qdrant filter for the resolved trust level."""
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

def search_qdrant(query_embedding: list, limit: int = 5) -> list:
    """Search Qdrant with trust-level filter + client-side defense-in-depth.

    Over-fetches (limit * 8) then filters client-side, matching auto_recall.py.
    """
    trust_level = _resolve_trust_level()
    level_order = ["public", "trusted", "private"]
    agent_idx = level_order.index(trust_level) if trust_level in level_order else 0

    fetch_n = limit * 8
    search_data = json.dumps({
        "vector": query_embedding,
        "limit": fetch_n,
        "with_payload": True,
        "score_threshold": 0.25,
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

    # Client-side defense-in-depth filter
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
    try:
        hook_input = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    cwd = hook_input.get("cwd", "")

    # Search for project-related memories
    query = f"project context {os.path.basename(cwd)} recent work decisions"
    embedding = get_embedding(query)
    if not embedding:
        sys.exit(0)

    results = search_qdrant(embedding, 5)
    if not results:
        sys.exit(0)

    memories = []
    for hit in results:
        payload = hit.get("payload", {})
        text = payload.get("text") or payload.get("content", "")
        category = payload.get("category", "fact")
        if text:
            memories.append(f"[{category}] {text[:150]}")

    if not memories:
        sys.exit(0)

    context = "\n--- Nexus Memory (Session Context) ---\n"
    context += f"Recent memories related to {os.path.basename(cwd)}:\n\n"
    context += "\n\n".join(memories)
    context += "\n--- End Nexus Memory ---\n"

    output = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": context
        }
    }
    print(json.dumps(output))

if __name__ == "__main__":
    main()