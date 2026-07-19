#!/usr/bin/env python3
"""Nexus Memory Auto-Capture Hook for Claude Code.

Fires on Stop (when Claude finishes responding). Reads the transcript,
extracts notable facts/decisions, and stores them in Qdrant.

Uses a lightweight extraction approach: looks for key patterns in the
conversation and stores them as memories.
"""

import sys
import json
import os
import urllib.request
import re
from datetime import datetime, timezone
from pathlib import Path

# Config
QDRANT_URL = os.getenv("NEXUS_QDRANT_URL", "http://localhost:6333")
COLLECTION = os.getenv("NEXUS_COLLECTION", "nexus")
VOYAGE_API_KEY = os.getenv("VOYAGE_API_KEY", "")
EMBEDDING_MODEL = os.getenv("NEXUS_EMBEDDING_MODEL", "voyage-3-large")
EMBEDDING_PROVIDER = os.getenv("NEXUS_EMBEDDING_PROVIDER", "voyage")
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
            "http://localhost:11434/api/embeddings",
            data=req_data,
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            return data["embedding"]
    return None

def store_memory(text: str, category: str = "session", point_id: str = None):
    """Store a memory in Qdrant."""
    embedding = get_embedding(text)
    if not embedding:
        return False

    import uuid
    point_id = point_id or str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    point_data = json.dumps({
        "points": [{
            "id": point_id,
            "vector": embedding,
            "payload": {
                "text": text,
                "content": text,
                "category": category,
                "source": "claude-code",
                "access_level": _resolve_trust_level(),
                "created_at": now,
                "agent": "claude-code"
            }
        }]
    }).encode()

    req = urllib.request.Request(
        f"{QDRANT_URL}/collections/{COLLECTION}/points",
        data=point_data,
        headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return True
    except Exception:
        return False

def extract_facts_from_transcript(transcript_path: str, session_id: str) -> list:
    """Extract notable facts from the conversation transcript.

    Reads the last few messages and looks for:
    - Decisions made
    - Files created/modified
    - Bugs fixed
    - Key learnings
    """
    facts = []
    try:
        with open(transcript_path, 'r') as f:
            lines = f.readlines()

        # Look at last 20 lines for recent activity
        recent = lines[-20:] if len(lines) > 20 else lines

        for line in recent:
            try:
                msg = json.loads(line)
                msg_type = msg.get("type", "")

                # Look for tool results with file operations
                if msg_type == "tool_result":
                    content = str(msg.get("content", ""))
                    if "created" in content.lower() or "modified" in content.lower():
                        # Extract file path
                        path_match = re.search(r'["\']?(/[^"\']+\.\w+)["\']?', content)
                        if path_match:
                            facts.append({
                                "text": f"File modified in Claude Code session {session_id}: {path_match.group(1)}",
                                "category": "session"
                            })

                # Look for assistant messages with key phrases
                elif msg_type == "assistant":
                    content = msg.get("content", "")
                    if isinstance(content, list):
                        for block in content:
                            if isinstance(block, dict) and block.get("type") == "text":
                                text = block.get("text", "")
                                # Look for decisions
                                if any(kw in text.lower() for kw in ["decided", "chose", "will use", "implemented", "fixed"]):
                                    # Take first 200 chars as a fact
                                    clean = text.strip()[:200]
                                    if len(clean) > 20:
                                        facts.append({
                                            "text": f"Claude Code session {session_id}: {clean}",
                                            "category": "session"
                                        })
            except (json.JSONDecodeError, KeyError):
                continue
    except Exception:
        pass

    return facts[:3]  # Max 3 facts per turn to avoid noise

def main():
    # Read hook input from stdin
    try:
        hook_input = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    session_id = hook_input.get("session_id", "unknown")
    transcript_path = hook_input.get("transcript_path", "")

    if not transcript_path:
        sys.exit(0)

    # Extract facts from transcript
    facts = extract_facts_from_transcript(transcript_path, session_id)

    if not facts:
        sys.exit(0)

    # Store each fact in Qdrant
    stored = 0
    for fact in facts:
        if store_memory(fact["text"], fact["category"]):
            stored += 1

    # Output summary (not injected into context, just logged)
    if stored > 0:
        output = {
            "hookSpecificOutput": {
                "hookEventName": "Stop",
                "additionalContext": f"Nexus: stored {stored} new memor{'y' if stored == 1 else 'ies'} from this turn."
            }
        }
        print(json.dumps(output))

if __name__ == "__main__":
    main()