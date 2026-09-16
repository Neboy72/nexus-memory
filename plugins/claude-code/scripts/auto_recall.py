#!/usr/bin/env python3
"""Nexus Memory Auto-Recall Hook for Claude Code.

Fires on UserPromptSubmit. Reads the user's prompt from stdin,
searches Qdrant for relevant memories, and injects them as
additional context for Claude to use.

Output JSON with additionalContext field injects text into Claude's context.
"""

import sys
import json
import logging
import os
import urllib.request
import urllib.error
from pathlib import Path

# Config
QDRANT_URL = os.getenv("NEXUS_QDRANT_URL", "http://localhost:6333")
COLLECTION = os.getenv("NEXUS_COLLECTION", "nexus")

# Self-organizing memory (Nebo law 07.09: full automation): shared scope-auto
# lib provides centroids + clear-match inference for recall gating.
# H241: import guard. A missing/broken scope_auto (partial install, syntax
# error) must NOT kill the whole hook — fail-open means recall without a scope
# filter. auto_capture does the same import inside try/except.
try:
    import scope_auto as _scope_auto  # noqa: E402 (same dir)
except Exception as _scope_import_exc:  # pragma: no cover - defensive
    _scope_auto = None
    print(
        f"[nexus auto-recall] scope_auto unavailable ({_scope_import_exc}) — "
        "recalling without scope filter",
        file=sys.stderr,
    )
EMBEDDING_PROVIDER = os.getenv("NEXUS_EMBEDDING_PROVIDER", "voyage")
VOYAGE_API_KEY = os.getenv("VOYAGE_API_KEY", "")
EMBEDDING_MODEL = os.getenv("NEXUS_EMBEDDING_MODEL", "voyage-4")


def _env_int(name: str, default: int, minimum: int | None = None) -> int:
    """Read an integer env var defensively (H242).

    An empty/malformed value (or a non-numeric typo) must not raise at import
    time — that would kill the hook for every prompt. Falls back to ``default``
    and clamps to ``minimum`` when given.
    """
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    if minimum is not None and value < minimum:
        value = minimum
    return value


MAX_RESULTS = _env_int("NEXUS_MAX_RECALL", 5, minimum=1)
AGENTS_FILE = Path.home() / ".nexus-memory" / "agents.json"

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

def get_embedding(text: str, input_type: str = "query") -> list:
    """Get embedding from configured provider.

    H243: every failure path (URL error, non-2xx, malformed body such as a
    missing ``data[0].embedding``) degrades to ``None`` — the hook then skips
    recall instead of aborting. Mirrors auto_capture.get_embedding's contract.

    W32-8: Voyage embeddings are asymmetric. Memories are STORED with
    input_type="document" (auto_capture, mcp_server), so embedding the recall
    query as a document too degrades every score. The default here is
    therefore "query"; auto_capture keeps "document" for the write path.
    """
    try:
        if EMBEDDING_PROVIDER == "voyage" and VOYAGE_API_KEY:
            req_data = json.dumps({
                "input": [text],
                "model": EMBEDDING_MODEL,
                "input_type": input_type
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
            # W33-10: same endpoint/field mismatch auto_capture fixed in
            # W32-9. The legacy /api/embeddings endpoint reads "prompt" and
            # returns a single "embedding"; the current /api/embed reads
            # "input" and returns "embeddings". This branch called the legacy
            # URL with the NEW field, so Ollama received no prompt and
            # returned no vector (recall silently skipped). Migrated.
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
    except Exception as exc:
        print(f"[nexus auto-recall] embedding failed: {exc}", file=sys.stderr)
        return None
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
    # Scope gating (project/agent areas): skip memories scoped to a different
    # area. Core principle: scopes steer AUTOMATIC recall only — explicit
    # search (MCP recall tool) is NEVER scope-filtered. Self-organizing
    # memory (Nebo law 07.09): the allowed set comes from the query itself
    # (clear match against scoped centroids) — manual NEXUS_SCOPE overrides.
    # Fail-open: no centroids / ambiguous query → no filtering (old behavior).
    my_scope = os.getenv("NEXUS_SCOPE", "").strip().lower()
    allowed_scopes = None
    if _scope_auto is not None:
        try:
            cents = _scope_auto.fetch_centroids(QDRANT_URL, COLLECTION)
            allowed_scopes = _scope_auto.prefetch_allowed_scopes(query_embedding, cents, my_scope)
        except Exception as exc:
            logging.info("scope_auto: recall gating skipped (%s) — fail-open", exc)
    filtered = []
    for hit in results:
        payload = hit.get("payload") or {}
        mem_level = payload.get("access_level", "private")
        mem_idx = level_order.index(mem_level) if mem_level in level_order else 2
        if mem_idx > agent_idx:
            continue
        if allowed_scopes is not None:
            p_scope = (payload.get("scope") or "default").strip().lower() or "default"
            if p_scope not in allowed_scopes:
                continue
        elif my_scope:
            p_scope = (payload.get("scope") or "default").strip().lower() or "default"
            if p_scope != "default" and p_scope != my_scope:
                continue
        filtered.append(hit)
        if len(filtered) >= limit:
            break

    return filtered


def graph_boost(top_results: list, max_boost: int = 3, access_level: str = "public",
                query_embedding: list = None) -> list:
    """Fetch 1-hop graph neighbors for the top vector search results.

    For each of the top `max_boost` results, reads the point's payload edges
    and fetches the connected facts' content. Returns formatted strings
    prefixed with [graph:<relation>] so the agent can distinguish graph-
    boosted results from pure vector hits.

    Access-level filtering: only returns memories the agent is allowed
    to see based on the resolved trust level.

    Scope gating (Nr 440): a graph hop is still an automatic recall, so it
    obeys the SAME scope filter as a vector hit — `query_embedding` feeds the
    identical `prefetch_allowed_scopes` check; the access level applies on top.

    Failures are silently skipped - vector results alone are always returned.
    """
    level_order = ["public", "trusted", "private"]
    agent_idx = level_order.index(access_level) if access_level in level_order else 0

    # Mirror search_qdrant's scope resolution so a boosted hop cannot leak a
    # scope that the vector path would have filtered out. If scope_auto is
    # unavailable or the query is ambiguous, `allowed_scopes` stays None and
    # only the explicit NEXUS_SCOPE fallback below applies (fail-open).
    my_scope = os.getenv("NEXUS_SCOPE", "").strip().lower()
    allowed_scopes = None
    if _scope_auto is not None and query_embedding:
        try:
            cents = _scope_auto.fetch_centroids(QDRANT_URL, COLLECTION)
            allowed_scopes = _scope_auto.prefetch_allowed_scopes(
                query_embedding, cents, my_scope
            )
        except Exception as exc:
            logging.info("scope_auto: graph-boost gating skipped (%s) — fail-open", exc)

    boosted = []
    seen_ids = set()

    try:
        for hit in top_results[:max_boost]:
            pid = hit.get("id", "")
            if not pid or pid in seen_ids:
                continue
            seen_ids.add(pid)

            # Fetch the point to read its edges
            scroll_req = urllib.request.Request(
                f"{QDRANT_URL}/collections/{COLLECTION}/points/scroll",
                data=json.dumps({
                    "limit": 1,
                    "with_payload": True,
                    "with_vectors": False,
                    "filter": {"must": [{"has_id": [pid]}]}
                }).encode(),
                headers={"Content-Type": "application/json"}
            )
            try:
                with urllib.request.urlopen(scroll_req, timeout=5) as resp:
                    data = json.loads(resp.read())
                    points = data.get("result", {}).get("points", [])
                    if not points:
                        continue
                    edges = (points[0].get("payload") or {}).get("edges") or []
            except Exception:
                continue
            if not isinstance(edges, list):
                continue
            for edge in edges:
                if not isinstance(edge, dict):
                    continue
                if edge.get("status", "active") != "active":
                    continue
                target_id = edge.get("target_fact_id", "")
                if not target_id or target_id in seen_ids:
                    continue
                seen_ids.add(target_id)

                # Fetch the target point's content
                try:
                    target_req = urllib.request.Request(
                        f"{QDRANT_URL}/collections/{COLLECTION}/points/scroll",
                        data=json.dumps({
                            "limit": 1,
                            "with_payload": True,
                            "with_vectors": False,
                            "filter": {"must": [{"has_id": [target_id]}]}
                        }).encode(),
                        headers={"Content-Type": "application/json"}
                    )
                    with urllib.request.urlopen(target_req, timeout=5) as resp:
                        data = json.loads(resp.read())
                        tpoints = data.get("result", {}).get("points", [])
                        if not tpoints:
                            continue
                        tp_payload = tpoints[0].get("payload") or {}
                        # Access-level check
                        tp_access = tp_payload.get("access_level", "private")
                        mem_idx = level_order.index(tp_access) if tp_access in level_order else 2
                        if mem_idx > agent_idx:
                            continue
                        # Scope check (Nr 440) — identical to the vector path:
                        # with an inferred allowed set, a non-allowed scope is
                        # skipped; otherwise only an explicit NEXUS_SCOPE gates,
                        # and "default" is always allowed.
                        p_scope = (tp_payload.get("scope") or "default").strip().lower() or "default"
                        if allowed_scopes is not None:
                            if p_scope not in allowed_scopes:
                                continue
                        elif my_scope and p_scope != "default" and p_scope != my_scope:
                            continue
                        text = tp_payload.get("content", "")
                        if text:
                            rel = edge.get("relation", "related")
                            boosted.append(f"[graph:{rel}] {text[:400]}")
                except Exception:
                    continue
    except Exception as exc:
        import sys
        print(f"[nexus graph-boost] skipped: {exc}", file=sys.stderr)

    return boosted

def main():
    # Read hook input from stdin
    try:
        hook_input = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    prompt = hook_input.get("prompt", "")
    if not prompt or len(prompt) < 10:
        sys.exit(0)

    # Get embedding (W32-8: explicit "query" — stored memories are documents)
    embedding = get_embedding(prompt, input_type="query")
    if not embedding:
        sys.exit(0)

    # Search Qdrant
    results = search_qdrant(embedding, MAX_RESULTS)
    if not results:
        sys.exit(0)

    # Build context block
    memories = []
    for hit in results:
        payload = hit.get("payload") or {}
        text = payload.get("text") or payload.get("content", "")
        category = payload.get("category", "fact")
        score = hit.get("score", 0)
        if text and score > 0.3:
            memories.append(f"[{category}] (score: {score:.2f}) {text[:200]}")

    # Graph-boost: add 1-hop neighbors from top 3 vector hits (max 5 to prevent context bloat)
    # NOTE (Nr 440): graph hops are now scope-filtered like vector hits (same
    # prefetch_allowed_scopes check, query embedding passed through); access
    # level remains an additional gate.
    trust_level = _resolve_trust_level()
    graph_items = graph_boost(
        results, max_boost=3, access_level=trust_level, query_embedding=embedding
    )[:5]
    for gi in graph_items:
        memories.append(gi)

    if not memories:
        sys.exit(0)

    context = "\n--- Nexus Memory (Auto-Recall) ---\n"
    context += f"Found {len(memories)} relevant memories ({len(graph_items)} graph-boosted):\n\n"
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