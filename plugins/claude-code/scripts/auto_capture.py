#!/usr/bin/env python3
"""Nexus Memory Auto-Capture Hook for Claude Code.

Fires on Stop (when Claude finishes responding). Reads the transcript,
extracts notable facts/decisions, and stores them in Qdrant.

Uses a lightweight extraction approach: looks for key patterns in the
conversation and stores them as memories.
"""

import sys
import json
import logging
import os
import urllib.request
import re
from datetime import datetime, timezone
from pathlib import Path

# Config
QDRANT_URL = os.getenv("NEXUS_QDRANT_URL", "http://localhost:6333")
COLLECTION = os.getenv("NEXUS_COLLECTION", "nexus")
VOYAGE_API_KEY = os.getenv("VOYAGE_API_KEY", "")
EMBEDDING_MODEL = os.getenv("NEXUS_EMBEDDING_MODEL", "voyage-4")
EMBEDDING_PROVIDER = os.getenv("NEXUS_EMBEDDING_PROVIDER", "voyage")
AGENTS_FILE = Path.home() / ".nexus-memory" / "agents.json"

_SCOPE_RE = __import__("re").compile(r"^[a-z0-9][a-z0-9-]{0,39}$")


def _resolve_capture_scope(embedding) -> str:
    """Self-organizing memory scope resolution for auto-capture.

    Explicit NEXUS_SCOPE wins; else infer from scoped centroids (clear match
    via the shared scope_auto lib); else 'default'. Fail-open everywhere.
    """
    manual = os.getenv("NEXUS_SCOPE", "").strip().lower()
    if manual_scope_ok(manual):
        return manual
    try:
        import scope_auto as _scope_auto  # same dir
        cents = _scope_auto.fetch_centroids(QDRANT_URL, COLLECTION)
        return _scope_auto.infer_scope(embedding, cents)
    except Exception as exc:
        logging.info("scope_auto: capture inference skipped (%s) — default", exc)
        return "default"


def manual_scope_ok(s: str) -> bool:
    """Public scope validator — the single source of truth (H252).

    Applies the shared normalization (``_normalize_scope``: strip + lowercase)
    and then the scope contract ``^[a-z0-9][a-z0-9-]{0,39}$``. Empty or
    otherwise invalid input → False, so the caller falls through to centroid
    inference / 'default'.

    H252: a hand-built per-char check previously accepted a leading dash
    ("-foo", "-" alone) and rejected uppercase — diverging from ``_SCOPE_RE``
    and from the MCP server, so the same scope could be accepted on one path
    and silently dropped on another.

    NB: validates the normalized value directly rather than via
    ``_normalize_scope`` — that helper maps anything invalid to ``"default"``,
    which is itself a valid scope and would make every input pass.
    """
    if not isinstance(s, str):
        return False
    normalized = s.strip().lower()
    if not normalized:
        return False
    return bool(_SCOPE_RE.match(normalized))


# Backwards-compatible alias (older callers used the underscore name).
_manual_scope_ok = manual_scope_ok


def _normalize_scope(scope) -> str:
    """Normalize a scope label (project/agent areas). Fail-open to 'default'.

    Same contract as the MCP server's _normalize_scope: valid = non-empty
    [a-z0-9-] string, max 40 chars; anything else (None, empty, uppercase,
    too long, non-str) degrades to 'default' so callers never break.
    """
    if isinstance(scope, str):
        s = scope.strip().lower()
        if s and _SCOPE_RE.match(s):
            return s
    return "default"


def _resolve_trust_level() -> str:
    """Gatekeeper: resolve this agent's trust level from agents.json.

    Uses NEXUS_AGENT_ID env var to identify the caller. This hook inherits the
    shell's value (a deliberate override), unlike the MCP server which
    plugin.json pins to "claude-code". Set NEXUS_AGENT_ID shell-wide for
    consistent attribution across both paths.
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
    """Get embedding from configured provider.

    Returns None (→ nothing stored) on misconfiguration or failure. H253:
    misconfiguration is now reported on stderr instead of silently returning
    None, which made auto-capture quietly store nothing.
    """
    try:
        if EMBEDDING_PROVIDER == "voyage":
            if not VOYAGE_API_KEY:
                print(
                    "[nexus auto-capture] NEXUS_EMBEDDING_PROVIDER=voyage but "
                    "VOYAGE_API_KEY is unset — memory not stored",
                    file=sys.stderr,
                )
                return None
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
            # W32-9: endpoint ↔ field mismatch. The legacy /api/embeddings
            # endpoint reads the text from "prompt" and returns a single
            # "embedding"; the current /api/embed reads "input" and returns
            # "embeddings". We called the legacy URL with the new "input"
            # field, so Ollama received no prompt and returned no vector.
            # Migrated to the new API (same one confidence._embed uses).
            req_data = json.dumps({
                "model": os.getenv("NEXUS_OLLAMA_EMBED_MODEL", "nomic-embed-text"),
                "input": text
            }).encode()
            req = urllib.request.Request(
                "http://localhost:11434/api/embed",
                data=req_data,
                headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
            embeddings = data.get("embeddings") or []
            return embeddings[0] if embeddings else None
        else:
            print(
                f"[nexus auto-capture] unknown NEXUS_EMBEDDING_PROVIDER="
                f"{EMBEDDING_PROVIDER!r} — memory not stored",
                file=sys.stderr,
            )
            return None
    except Exception as exc:
        print(f"[nexus auto-capture] embedding failed: {exc}", file=sys.stderr)
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
                "agent": "claude-code",
                # H257: fetch_centroids filters on lifecycle_status ==
                # "canonical". Without this field these memories never feed the
                # scope centroids, which breaks the self-organizing feedback
                # loop (parity with the mcp_server.py writer).
                "lifecycle_status": "canonical",
                # Scope (self-organizing memory, Nebo law 07.09): explicit
                # NEXUS_SCOPE wins; else infer from scoped centroids on a
                # CLEAR match; else 'default'. Fail-open, zero config.
                "scope": _resolve_capture_scope(embedding),
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
    except Exception as exc:
        # H254: never drop a memory silently — report to stderr (stdout is the
        # hook/MCP protocol channel and must stay clean).
        print(f"[nexus auto-capture] memory not stored: {exc}", file=sys.stderr)
        return False

def _iter_content_blocks(msg: dict):
    """Yield content blocks from one transcript line.

    Real Claude-Code transcripts nest content under message.content
    (message.role = "user"/"assistant"); legacy/synthetic lines keep it at
    the top level. Both are supported so extraction works either way.
    """
    if not isinstance(msg, dict):
        return
    message = msg.get("message") if isinstance(msg.get("message"), dict) else {}
    content = message.get("content")
    if content is None:
        content = msg.get("content")
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict):
                yield block
    elif isinstance(content, str) and content:
        yield {"type": "text", "text": content}


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
                if not isinstance(msg, dict):
                    continue
                msg_type = msg.get("type", "")
                message = msg.get("message") if isinstance(msg.get("message"), dict) else {}
                mtype = message.get("role") or msg_type

                for block in _iter_content_blocks(msg):
                    btype = block.get("type", "")

                    # Tool results (user-side): look for file operations.
                    if btype == "tool_result" or msg_type == "tool_result":
                        raw = block.get("content", "")
                        if not raw:
                            raw = msg.get("content", "")  # legacy top-level
                        # tool_result content may be a plain string or a
                        # list of dicts with a "text" field — str() of either
                        # is enough for the path scan.
                        content_str = str(raw)
                        if "created" in content_str.lower() or "modified" in content_str.lower():
                            path_match = re.search(r'["\']?(/[^"\']+\.\w+)["\']?', content_str)
                            if path_match:
                                facts.append({
                                    "text": f"File modified in Claude Code session {session_id}: {path_match.group(1)}",
                                    "category": "session"
                                })
                        continue

                    # Assistant text blocks: look for decisions.
                    if btype == "text" and mtype == "assistant":
                        text = block.get("text", "")
                        if not isinstance(text, str):
                            continue
                        if any(kw in text.lower() for kw in ["decided", "chose", "will use", "implemented", "fixed"]):
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