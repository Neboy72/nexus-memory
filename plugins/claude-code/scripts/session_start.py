#!/usr/bin/env python3
"""Nexus Memory Session Start Hook for Claude Code.

Fires on SessionStart. Loads recent session context and project
memories from Qdrant to give Claude immediate context.
"""

import sys
import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

QDRANT_URL = os.getenv("NEXUS_QDRANT_URL", "http://localhost:6333")
COLLECTION = os.getenv("NEXUS_COLLECTION", "nexus")
VOYAGE_API_KEY = os.getenv("VOYAGE_API_KEY", "")
EMBEDDING_MODEL = os.getenv("NEXUS_EMBEDDING_MODEL", "voyage-4")
EMBEDDING_PROVIDER = os.getenv("NEXUS_EMBEDDING_PROVIDER", "ollama")
AGENTS_FILE = Path.home() / ".nexus-memory" / "agents.json"


def _central_env() -> dict:
    """Read the shared ``~/.hermes/.env`` so the hook follows the host.

    Hooks run under the app's environment, not a login shell, so they never
    see an exported or sourced variable. Without this the hook silently reads
    a different collection than the MCP server of the same install. Only the
    three Nexus keys are honoured and a real env var always wins.
    """
    out = {}
    path = os.environ.get("NEXUS_ENV_FILE",
                          str(Path.home() / ".hermes" / ".env"))
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                if key.startswith("export "):
                    key = key[7:].strip()
                if key in ("NEXUS_COLLECTION", "NEXUS_EMBEDDING_PROVIDER",
                           "NEXUS_EMBEDDING_MODEL"):
                    out.setdefault(key, val.strip().strip('"').strip("'"))
    except OSError:
        pass
    return out


_CENTRAL = _central_env()
COLLECTION = os.getenv("NEXUS_COLLECTION") or _CENTRAL.get("NEXUS_COLLECTION") or COLLECTION
EMBEDDING_PROVIDER = (os.getenv("NEXUS_EMBEDDING_PROVIDER")
                      or _CENTRAL.get("NEXUS_EMBEDDING_PROVIDER")
                      or EMBEDDING_PROVIDER)
EMBEDDING_MODEL = (os.getenv("NEXUS_EMBEDDING_MODEL")
                   or _CENTRAL.get("NEXUS_EMBEDDING_MODEL")
                   or EMBEDDING_MODEL)


def _vector_body(query_embedding):
    """The Qdrant `vector` value: list for anonymous, {name, vector} for named.

    A named collection (the engine stores one name per embedding fingerprint)
    rejects a bare vector with 400 "Not existing vector name error", which the
    caller turns into an empty result — memory simply stops appearing. The
    shared scope_auto helper reads the layout from the collection itself.
    """
    try:
        import scope_auto as _scope_auto
        return _scope_auto.search_vector_body(query_embedding, QDRANT_URL, COLLECTION)
    except Exception:
        return query_embedding

def _resolve_trust_level() -> str:
    """Gatekeeper: resolve this agent's trust level from agents.json.

    NEXUS_AGENT_ID comes from the shell (hooks honor an override); the MCP
    server path is pinned to "claude-code" by plugin.json. Set it shell-wide
    for consistent attribution across both paths.
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
    except Exception as exc:
        # W40-1: a missing/corrupt/unreadable agents.json must not be
        # indistinguishable from a genuinely low-privilege agent — report the
        # misconfigured gatekeeper on stderr (hooks tolerate stderr) before
        # falling back to least privilege.
        print(
            f"[nexus session-start] trust registry unreadable: {exc}",
            file=sys.stderr,
        )
        return "public"

def get_embedding(text: str) -> Optional[list]:
    """Embed the session query. W32-7: fail-soft.

    A Voyage timeout / non-2xx / malformed body previously escaped as an
    unhandled exception and crashed the SessionStart hook — search_qdrant is
    fail-soft, the embed call was not. Any transport or shape failure now
    returns None, and the caller treats that exactly like today (no recall).

    W40-1: the annotation says so too — the ``return None`` fall-throughs at
    the end are part of the contract, not an oversight. A missing
    VOYAGE_API_KEY or a mistyped provider disables the hook by design
    (documented here rather than failing silently).

    NB: this hook uses urllib (not requests), so the caught network errors
    are ``urllib.error.URLError`` (HTTPError is a subclass) plus the generic
    OSError and the body-shape errors (KeyError/IndexError/ValueError).
    """
    try:
        if EMBEDDING_PROVIDER == "ollama":
            # Local default: no API key, nothing leaves the machine.
            req_data = json.dumps({
                "model": os.getenv("NEXUS_OLLAMA_EMBED_MODEL", "qwen3-embedding:0.6b"),
                "input": text,
            }).encode()
            req = urllib.request.Request(
                os.getenv("NEXUS_OLLAMA_URL", "http://localhost:11434") + "/api/embed",
                data=req_data,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
            if isinstance(data, dict):
                embs = data.get("embeddings")
                if isinstance(embs, list) and embs and isinstance(embs[0], list):
                    return embs[0]
            print("[nexus session-start] ollama embedding response shape unexpected", file=sys.stderr)
            return None
        if EMBEDDING_PROVIDER == "voyage" and VOYAGE_API_KEY:
            req_data = json.dumps({
                "input": [text],
                "model": EMBEDDING_MODEL,
                # Voyage is asymmetric: this embeds the session QUERY, so it must
                # use input_type="query" (H182), not "document".
                "input_type": "query"
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
                # W40-scan (high): the response is untrusted JSON. A top-level
                # array/string/number or "data": null raises TypeError on the
                # subscript chain — absent from the tuple below, so the hook
                # aborted instead of failing soft. Validate the shape instead
                # of widening the except (KeyError/IndexError/ValueError stay
                # caught for genuinely malformed bodies).
                if (
                    isinstance(data, dict)
                    and isinstance(data.get("data"), list)
                    and len(data["data"]) > 0
                    and isinstance(data["data"][0], dict)
                ):
                    emb = data["data"][0].get("embedding")
                    if isinstance(emb, list) and emb:
                        return emb
                print("[nexus session-start] embedding response shape unexpected", file=sys.stderr)
                return None
    except (urllib.error.URLError, OSError, KeyError, IndexError, ValueError) as exc:
        print(f"[nexus session-start] embedding failed: {exc}", file=sys.stderr)
        return None
    return None

def _get_trust_filter(trust_level: str) -> dict:
    """Return Qdrant filter for an already-resolved trust level.

    W40-1: the level is resolved once by the caller. Resolving it again here
    re-read the registry and could disagree with the client-side cutoff.
    """
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

    W40-1: the trust level is resolved exactly once and reused for both the
    server-side filter and the client-side cutoff — two independent lookups
    re-read agents.json and could disagree about which levels are allowed.
    """
    trust_level = _resolve_trust_level()
    level_order = ["public", "trusted", "private"]
    agent_idx = level_order.index(trust_level) if trust_level in level_order else 0

    fetch_n = limit * 8
    search_data = json.dumps({
        "vector": _vector_body(query_embedding),
        "limit": fetch_n,
        "with_payload": True,
        "score_threshold": 0.25,
        "filter": _get_trust_filter(trust_level)
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
    except Exception as exc:
        # W40-1: an unreachable Qdrant / malformed response must not be
        # indistinguishable from a legitimately empty memory store — say so on
        # stderr before degrading to no recall.
        print(f"[nexus session-start] search failed: {exc}", file=sys.stderr)
        return []

    # Client-side defense-in-depth filter
    filtered = []
    for hit in results:
        # H250: Qdrant can store points without a payload — ``"payload": null``
        # makes the default-arg form return None, then ``payload.get`` raises
        # AttributeError outside any try/except and crashes the hook.
        # W40-scan (medium): same guard as in main() — a truthy NON-dict
        # payload (e.g. a list) also has no .get and crashed the filter loop.
        payload = hit.get("payload")
        if not isinstance(payload, dict):
            payload = {}
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
        payload = hit.get("payload") or {}  # H250: null payload → {} not None
        if not isinstance(payload, dict):
            # W40-1: same payload-variance class as the `text` guard below —
            # a non-dict payload has no `.get` and would abort the hook.
            payload = {}
        text = payload.get("text") or payload.get("content", "")
        category = payload.get("category", "fact")
        # W40-1: the payload is untrusted JSON — a truthy non-string `text`
        # (e.g. an int) raised TypeError on the slice and aborted the hook
        # after the embeddings/search cost was already paid.
        if isinstance(text, str) and text:
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