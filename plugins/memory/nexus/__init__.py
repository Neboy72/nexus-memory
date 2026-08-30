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
RECALL_SCHEMA = {"name": "nexus_recall", "description": "Search Nexus Memory for relevant past memories, facts, or context.", "parameters": {"type": "object", "properties": {"query": {"type": "string", "description": "What to search for."}, "limit": {"type": "integer", "description": "Max results (default 5).", "default": 5},
                  "as_of": {"type": "string", "description": "Point-in-time: YYYY-MM-DD - only memories created on/before this date.", "default": ""}}, "required": ["query"]}}
REMEMBER_SCHEMA = {"name": "nexus_remember", "description": "Store a memory in Nexus Memory for future recall across all agents.", "parameters": {"type": "object", "properties": {"text": {"type": "string", "description": "The memory content to store."}, "category": {"type": "string", "description": "Memory category: fact, belief, session, rule, preference, temp.", "default": "fact"}, "access_level": {"type": "string", "description": "Visibility: public, trusted, private.", "default": "public"}, "source": {"type": "string", "description": "Where this memory came from.", "default": ""}, "source_url": {"type": "string", "description": "URL for verification (optional).", "default": ""}, "confidence": {"type": "number", "description": "Confidence score 0.0-1.0.", "default": 0.7}}, "required": ["text"]}}
FORGET_SCHEMA = {"name": "nexus_forget", "description": "Delete a memory from Nexus Memory by ID.", "parameters": {"type": "object", "properties": {"memory_id": {"type": "string", "description": "The memory ID to delete."}}, "required": ["memory_id"]}}
GUARDRAIL_CHECK_SCHEMA = {"name": "nexus_guardrail_check", "description": "Active Guardrails: Check if an action is safe before executing it. Queries Nexus Memory for protection rules. Use before destructive operations (rm, drop, kill, overwrite).", "parameters": {"type": "object", "properties": {"command": {"type": "string", "description": "The command string to check (e.g. 'rm -rf ~/project/')"}, "tool_name": {"type": "string", "description": "The tool being called (e.g. 'terminal', 'write_file')", "default": ""}, "tool_input": {"type": "object", "description": "Full tool input dict for path-based checks", "default": {}}}, "required": ["command"]}}
GUARDRAIL_OVERRIDE_SCHEMA = {"name": "nexus_guardrail_override", "description": "Active Guardrails: Record a guardrail override with full audit trail. Required when guardrail_check returns 'block' but the action is explicitly authorized.", "parameters": {"type": "object", "properties": {"command": {"type": "string", "description": "The command that was blocked"}, "reasoning": {"type": "string", "description": "Explicit reasoning why this action is safe despite the guardrail block. Minimum 10 characters."}, "matched_rules": {"type": "array", "items": {"type": "object"}, "description": "The matched_rules array from the guardrail_check response", "default": []}, "agent_id": {"type": "string", "description": "Agent identifier for audit trail", "default": "unknown"}}, "required": ["command", "reasoning"]}}


GRAPH_TRAVERSE_SCHEMA = {"name": "nexus_graph_traverse", "description": "Knowledge Graph: Multi-hop traversal from a starting fact. Answers 'what is connected to X?' across the entity graph.", "parameters": {"type": "object", "properties": {"fact_id": {"type": "string", "description": "The Qdrant point ID to start traversal from"}, "max_depth": {"type": "integer", "description": "Maximum hops (default 3)", "default": 3}, "relation": {"type": "string", "description": "Only follow edges with this relation (e.g. 'manages', 'runs_on')", "default": ""}, "target_type": {"type": "string", "description": "Only return targets with this entity_type (e.g. 'device', 'service')", "default": ""}}, "required": ["fact_id"]}}
FIND_ENTITIES_SCHEMA = {"name": "nexus_find_entities", "description": "Knowledge Graph: Find all entity-typed memories. Returns list of {id, name, entity_type, content, attributes}.", "parameters": {"type": "object", "properties": {"entity_type": {"type": "string", "description": "Filter by entity type: device, service, person, location, organization, concept, software, protocol", "default": ""}, "limit": {"type": "integer", "description": "Max results (default 50)", "default": 50}}, "required": []}}
GET_SUBGRAPH_SCHEMA = {"name": "nexus_get_subgraph", "description": "Knowledge Graph: Get a subgraph centered on a fact for visualization. Returns {nodes, edges}.", "parameters": {"type": "object", "properties": {"fact_id": {"type": "string", "description": "The Qdrant point ID to center the subgraph on"}, "max_depth": {"type": "integer", "description": "Maximum hops (default 2)", "default": 2}}, "required": ["fact_id"]}}
GET_RELATED_SCHEMA = {"name": "nexus_get_related", "description": "Knowledge Graph: Get directly related facts (1-hop, bidirectional). Returns list of {fact_id, relation, direction}.", "parameters": {"type": "object", "properties": {"fact_id": {"type": "string", "description": "The Qdrant point ID to find neighbors for"}, "relation": {"type": "string", "description": "Only return edges with this relation (e.g. 'manages')", "default": ""}}, "required": ["fact_id"]}}
COST_ROUTING_STATS_SCHEMA = {"name": "nexus_cost_routing_stats", "description": "Cost-Aware Routing: Get statistics about embedding provider routing.", "parameters": {"type": "object", "properties": {}, "required": []}}
COST_ROUTING_EXPLAIN_SCHEMA = {"name": "nexus_cost_routing_explain", "description": "Cost-Aware Routing: Explain the routing decision for a memory category.", "parameters": {"type": "object", "properties": {"category": {"type": "string", "description": "Memory category: fact, rule, preference, belief, session, temp, entity, procedure"}}, "required": ["category"]}}
SICA_RUN_SCHEMA = {"name": "nexus_sica_run", "description": "SICA Self-Improvement: Run a self-improvement cycle that scans memories for drift, stale facts, low confidence, and contradictions. Auto-patches non-destructive issues (stale temp deletion). Returns issues found, auto-patches applied, and suggestions for review.", "parameters": {"type": "object", "properties": {"auto_patch": {"type": "boolean", "description": "Apply non-destructive patches automatically (default true)", "default": True}}, "required": []}}


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
        self._backup_nudged = False
        self._last_backup_time: float = 0
        self._last_backup_path: str = ""
        self._skill_graph = None  # cached SkillGraph for graph-boost
        self._skill_graph_lock = threading.Lock()
        self._rerank_cfg = None  # cached rerank config (lazy, roadmap 1.2)
        self._embed_cache = None  # roadmap 3.1 L0: lazy EmbedCache
        self._embed_cache_lock = threading.Lock()
        self._entity_extract_lock = threading.Lock()  # single-flight enrich (1.1)
        self._rerank_lock = threading.Lock()

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
        self._update_nudged = False
        self._check_nexus_update()
        self._start_auto_backup()
        logger.info("NexusMemoryProvider init (collection=%s, dim=%d)", self._collection, self._embedder.dim)

    def _start_auto_backup(self) -> None:
        """Start automatic daily backup of all memories."""
        import threading, time, json, os
        from datetime import datetime

        def _backup_loop():
            # Wait 60s after startup before first backup
            time.sleep(60)
            while not self._write_stop.is_set():
                try:
                    self._do_backup()
                except Exception as e:
                    logger.warning(f"Auto-backup failed: {e}")
                # Sleep 24h (check stop flag every 60s for responsive shutdown)
                for _ in range(360):  # 6h, check every 60s
                    if self._write_stop.is_set():
                        return
                    time.sleep(60)

        threading.Thread(target=_backup_loop, name="nexus-backup", daemon=True).start()

    def _do_backup(self) -> str:
        """Create a full backup of all memories as JSON. Returns backup file path."""
        import json, os, time
        from datetime import datetime

        backup_dir = os.path.expanduser("~/.nexus-memory/backups")
        os.makedirs(backup_dir, exist_ok=True)

        # Scroll all points from Qdrant
        all_points = []
        offset = None
        while True:
            from qdrant_client import models as qm
            results, offset = self._qdrant.scroll(
                collection_name=self._collection,
                limit=100,
                offset=offset,
                with_payload=True,
                with_vectors=True,  # Include vectors for zero-cost restore
            )
            for p in results:
                all_points.append({
                    "id": str(p.id),
                    "payload": p.payload or {},
                    "vector": p.vector if isinstance(p.vector, list) else None,
                })
            if not offset:
                break

        # Write backup file
        ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        backup_path = os.path.join(backup_dir, f"nexus-backup-{ts}.json")
        backup_data = {
            "version": "0.4.0",
            "collection": self._collection,
            "created_at": datetime.now().isoformat(),
            "point_count": len(all_points),
            "points": all_points,
        }
        with open(backup_path, "w") as f:
            json.dump(backup_data, f, default=str)

        self._last_backup_time = time.time()
        self._last_backup_path = backup_path

        # Keep only last 7 backups
        backups = sorted(
            [f for f in os.listdir(backup_dir) if f.startswith("nexus-backup-")],
            reverse=True
        )
        for old in backups[7:]:
            try:
                os.remove(os.path.join(backup_dir, old))
            except OSError:
                pass

        logger.info("💾 Auto-backup: %d memories → %s", len(all_points), backup_path)
        return backup_path

    def _check_nexus_update(self) -> None:
        """Background check for Nexus Memory updates on GitHub."""
        import threading, json, urllib.request
        def _bg():
            try:
                from nexus import __version__ as ver
                req = urllib.request.Request(
                    "https://api.github.com/repos/Neboy72/nexus-memory/releases/latest",
                    headers={"Accept": "application/vnd.github.v3+json", "User-Agent": f"nexus-memory/{ver}"}
                )
                data = json.loads(urllib.request.urlopen(req, timeout=10).read().decode())
                latest = data.get("tag_name", "").lstrip("v")
                from packaging.version import parse
                if parse(latest) > parse(ver):
                    self._update_available = {"version": latest, "url": data.get("html_url", "")}
                    logger.info("📦 Nexus Memory update available: v%s → v%s", ver, latest)
                else:
                    self._update_available = None
            except Exception:
                self._update_available = None
        threading.Thread(target=_bg, daemon=True).start()

    def system_prompt_block(self) -> str:
        base = "Nexus Memory active. Relevant memories are automatically injected. Use nexus_recall to search manually, nexus_remember to store."
        # Update nudge (once per lifetime)
        if hasattr(self, '_update_available') and self._update_available and not self._update_nudged:
            self._update_nudged = True
            base += f"\n\n📦 Nexus Memory update available: v{self._update_available['version']}. Tell your user about this update and ask if they want to upgrade."
        # Backup info nudge (once per lifetime)
        if not self._backup_nudged:
            self._backup_nudged = True
            base += (
                "\n\n💾 Nexus Memory has automatic daily backups enabled. "
                "Backups are saved to ~/.nexus-memory/backups/. "
                "Tell your user: 'Nexus Memory backs up your memories automatically every day. "
                "I recommend copying the backup to external storage (USB, NAS, cloud) for extra safety. "
                "Latest backup: " + (self._last_backup_path or "pending (first backup runs 60s after startup)") + ". "
                "Shall I help you set up external backup?'"
            )
        return base

    def shutdown(self) -> None:
        self._write_stop.set()
        if self._write_thread and self._write_thread.is_alive():
            self._write_thread.join(timeout=5.0)
        with self._skill_graph_lock:
            if self._skill_graph is not None:
                try: self._skill_graph.store.close()
                except Exception: pass
                self._skill_graph = None
        if self._qdrant: self._qdrant.close(); self._qdrant = None
        logger.info("NexusMemoryProvider shut down")

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        with self._prefetch_lock: return self._prefetch_result

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        threading.Thread(target=self._do_prefetch, args=(query,), name="nexus-prefetch", daemon=True).start()

    def _get_embed_cache(self):
        """Roadmap 3.1 L0: lazy EmbedCache (repeated queries skip Voyage)."""
        if getattr(self, "_embed_cache", None) is None:
            lock = getattr(self, "_embed_cache_lock", None) or threading.Lock()
            with lock:
                if getattr(self, "_embed_cache", None) is None:
                    from nexus_memory.embed_cache import EmbedCache
                    self._embed_cache = EmbedCache()
        return self._embed_cache

    def _embed_cached(self, text: str) -> List[float]:
        """Embed with L0 cache: hit = no cloud call (~256ms saved)."""
        cache = self._get_embed_cache()
        vec = cache.get(text)
        if vec is None:
            vec = self._embedder.embed(text)
            cache.put(text, vec)
        return vec

    def _get_skill_graph(self):
        """Get or create a cached SkillGraph instance."""
        with self._skill_graph_lock:
            if self._skill_graph is None:
                from nexus.graph.graph import SkillGraph
                self._skill_graph = SkillGraph(
                    qdrant_url=f"http://{_HOST}:{_PORT}",
                    collection=self._collection,
                )
                self._skill_graph.initialize()
            return self._skill_graph

    def _graph_boost(self, top_points: list, max_boost: int = 3,
                     out_pids: Optional[set] = None) -> List[str]:
        """Fetch 1-hop graph neighbors for the top vector search results.

        For each of the top ``max_boost`` points, queries the Knowledge Graph
        for directly related facts (1-hop, bidirectional). Returns a list of
        context strings prefixed with ``[graph]`` so the agent can distinguish
        graph-boosted results from pure vector hits.

        Access-level filtering: the Hermes plugin has full access as the
        primary agent, so no filtering is needed here.

        Failures are logged and silently skipped - vector results alone are
        always returned without the graph boost.
        """
        boosted: List[str] = []
        if not self._qdrant: return boosted
        try:
            from nexus.graph.traversal import GraphTraversal
            sg = self._get_skill_graph()
            gt = GraphTraversal(sg)
            seen_ids: set = set()
            for p in top_points[:max_boost]:
                pid = str(p.id)
                if pid in seen_ids: continue
                seen_ids.add(pid)
                neighbors = gt.get_related(pid)
                for n in neighbors:
                    nid = n.get("fact_id", "")
                    if not nid or nid in seen_ids: continue
                    seen_ids.add(nid)
                    pt = sg.get_point(nid)
                    if not pt: continue
                    pt_payload = pt.get("payload") or {}
                    # 4.6: deprecated neighbors never surface as graph-boost
                    if (pt_payload.get("lifecycle_status") or "canonical") in ("deprecated", "rolled_back"):
                        continue
                    text = pt_payload.get("content", "")
                    if text:
                        rel = n.get("relation", "related")
                        boosted.append(f"[graph:{rel}] {text[:400]}")
                        if out_pids is not None:
                            out_pids.add(nid)
        except Exception as exc:
            logger.warning("Graph boost skipped: %s", exc)
        return boosted

    def _do_prefetch(self, query: str) -> None:
        if not self._embedder or not self._qdrant: return
        try:
            vector = self._embed_cached(query)
            pts = self._qdrant.query_points(collection_name=self._collection, query=vector, limit=10).points
            budget = int(os.environ.get("NEXUS_PREFETCH_CHARS", "2400"))
            total = 0
            items: List[str] = []
            for p in pts:
                if total >= budget:
                    break
                pl = p.payload or {}; text = pl.get("content", "")
                # Roadmap 4.6: superseded facts never surface in prefetch.
                if (pl.get("lifecycle_status") or "canonical") in ("deprecated", "rolled_back"):
                    continue
                if text:
                    item = f"[{pl.get('category','fact')}] score={p.score or 0:.2f}: {text[:500]}"
                    # Review fix R2: whole-item slicing (konsistent mit recall-Slice)
                    if total + len(item) > budget:
                        if budget - total < 80:
                            break
                        item = item[: budget - total].rstrip() + " …"
                    items.append(item)
                    total += len(item)
            # Graph-boost: add 1-hop neighbors from top 3 vector hits
            graph_items = self._graph_boost(pts, max_boost=3)
            for gi in graph_items:
                if total >= budget:
                    break
                item = gi[: budget - total]
                if item:
                    items.append(item)
                    total += len(item)
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

        # Auto-Entity-Detection (Nebo 30.08.2026): Hardware-Fakten sofort als Entity speichern,
        # nicht nur bei session_end. Pattern: "Ich habe X" / "Ich nutze X" / "Ich habe X per Y"
        if any(sig in user_content.lower() for sig in ["ich habe ", "ich nutze ", "ich hab ", "ich nutz "]):
            try:
                self._maybe_extract_hardware_entities(user_content, session_id)
            except Exception as exc:
                logger.debug("Hardware-entity auto-extract failed (non-fatal): %s", exc)

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
                    "lifecycle_status": "canonical",
                    "provenance": {"source_type": "hermes-plugin", "created_by": "nexus-memory-provider",
                                   "timestamp": ts, "confidence": confidence}}
        self._qdrant.upsert(collection_name=self._collection,
                            points=[qmodels.PointStruct(id=eid, vector=vector, payload=payload)])
        return {"status": "ok", "id": eid, "category": category}

    def _recall(self, query: str, limit: int = 5, as_of: str = "") -> List[Dict[str, Any]]:
        """Roadmap 4.5: as_of='YYYY-MM-DD' limits recall to memories
        created on/before that date (point-in-time view). Empty = no filter."""
        if not self._embedder or not self._qdrant: return []
        flywheel: List[str] = []  # roadmap 4.9: top-3 recalled point ids
        # Rerank config is read once and cached (double-checked lock,
        # mirrors the _skill_graph caching pattern in this class).
        if self._rerank_cfg is None:
            with self._rerank_lock:
                if self._rerank_cfg is None:
                    from nexus_memory.reranker import load_rerank_config
                    self._rerank_cfg = load_rerank_config()
        vector = self._embed_cached(query)
        cfg = self._rerank_cfg
        fetch_k = max(limit, 1)
        if cfg.get("enabled"):
            # Fetch a larger pool so the reranker can reorder beyond limit.
            from nexus_memory.reranker import DEFAULT_POOL_K
            fetch_k = max(limit, int(cfg.get("pool_k", DEFAULT_POOL_K)))
        pts = self._qdrant.query_points(collection_name=self._collection, query=vector, limit=fetch_k).points
        # Roadmap 4.6 + review: filter BEFORE rerank so deprecated points
        # don't burn rerank-pool slots (cost/latency) or shrink results.
        _suppressed = {"deprecated", "rolled_back"}
        pts = [p for p in pts if (p.payload or {}).get("lifecycle_status") not in _suppressed]
        if cfg.get("enabled"):
            from nexus_memory.reranker import rerank_points, DEFAULT_POOL_K
            pts = rerank_points(
                query, pts,
                reranker=cfg.get("reranker", "voyage"),
                pool_k=int(cfg.get("pool_k", DEFAULT_POOL_K)),
                voyage_api_key=cfg.get("voyage_api_key") or None,
            )
        results: List[Dict[str, Any]] = []
        seen_ids: set = set()
        for p in pts:
            pl = p.payload or {}
            # Roadmap 4.6: superseded/rolled-back facts stay in Qdrant for
            # audit but never surface in recall (mirrors MCP server filter).
            # Missing lifecycle_status (legacy points) stays visible.
            if (pl.get("lifecycle_status") or "canonical") in ("deprecated", "rolled_back"):
                continue
            # Roadmap 4.5: point-in-time - skip memories newer than as_of
            if as_of and (pl.get("created_at") or "")[:10] > as_of:
                continue
            pid = pl.get("id") or str(p.id)
            seen_ids.add(pid)
            if len(flywheel) < 3:
                flywheel.append((pid, pl.get("access_count", 0) or 0,
                                 (pl.get("lifecycle_status") or "canonical")))
            results.append({"id": pid, "text": (pl.get("content") or "")[:2000],
                            "score": round(float(p.score or 0.0), 3), "source": pl.get("source"),
                            "source_url": pl.get("source_url"), "access_level": pl.get("access_level"),
                            "category": pl.get("category", "fact"),
                            "confidence": (pl.get("provenance") or {}).get("confidence"),
                            "created_at": pl.get("created_at")})
        # Graph-boost: add 1-hop neighbors from top 3 vector hits
        # Graph items are APPENDED (not sorted into vector results) so they
        # survive the limit slice regardless of their 0.0 score.
        graph_pids: set = set()
        graph_items = self._graph_boost(pts, max_boost=3, out_pids=graph_pids)
        # Review fix B1 (blocker): graph-boosted neighbors count as accessed -
        # ohne Bump stuft autonomous purge aktiv genutzte Nachbarn als
        # "never accessed" ein und loescht sie (Datenverlust).
        for gpid in list(graph_pids)[:3]:
            if len(flywheel) < 6:
                flywheel.append((gpid, 0, "canonical"))
        # Roadmap 4.9: fire-and-forget access bump for the top recall hits
        # (payloads carried inline - no extra retrieve roundtrips, review fix)
        if flywheel and self._qdrant:
            threading.Thread(target=self._flywheel_bump, args=(flywheel,),
                             name="nexus-flywheel", daemon=True).start()
        vector_results = results[:limit]
        for gi in graph_items:
            vector_results.append({"id": "", "text": gi, "score": 0.0, "source": "graph-boost",
                            "source_url": "", "access_level": "public",
                            "category": "graph", "confidence": None,
                            "created_at": ""})
        return vector_results

    def _flywheel_bump(self, entries: List[tuple]) -> None:
        """Roadmap 4.9: increment access_count on recalled points.

        Fire-and-forget ( payloads passed in - zero retrieve roundtrips
        since _recall already had them). Skips points deprecated between
        recall and this bump (review fix B2). SICA uses access_count
        later as trust signal for retrieval weighting.
        """
        for pid, count, status in entries:
            try:
                # Review fix: skip facts deprecated after the recall snapshot
                if status in ("deprecated", "rolled_back"):
                    continue
                self._qdrant.set_payload(
                    collection_name=self._collection,
                    payload={"access_count": count + 1,
                             "last_accessed": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
                    points=[pid], wait=False)
            except Exception as exc:
                logger.debug("flywheel bump skip %s: %s", str(pid)[:8], exc)

    def _forget(self, memory_id: str) -> Dict[str, Any]:
        if not self._qdrant: raise RuntimeError("Provider not initialized")
        if not memory_id:
            return {"status": "error", "error": "Empty memory_id - graph-boosted entries cannot be deleted"}
        self._qdrant.delete(collection_name=self._collection,
                            points_selector=qmodels.PointIdsList(points=[memory_id]))
        return {"status": "ok", "id": memory_id}

    def _guardrail_check(self, command: str, tool_name: str = "",
                         tool_input: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Check if an action is safe before executing it."""
        if not command:
            return {"verdict": "allow", "reason": "Empty command"}
        try:
            from nexus_memory.guardrails import GuardrailEngine
            if not self._qdrant: raise RuntimeError("Provider not initialized")
            vector_dim = self._embedder.dim if self._embedder else 384
            engine = GuardrailEngine(self._qdrant, self._collection, vector_dim=vector_dim)
            result = engine.check_action(command, tool_name, tool_input or {})
            return result.to_dict()
        except Exception as exc:
            logger.warning("Guardrail check failed (fail-open): %s", exc)
            return {"verdict": "allow", "reason": f"Guardrail check failed (fail-open): {exc}"}

    def _guardrail_override(self, command: str, matched_rules: List[Dict[str, Any]],
                            reasoning: str, agent_id: str = "unknown") -> Dict[str, Any]:
        """Record a guardrail override with audit trail."""
        try:
            from nexus_memory.guardrails import GuardrailEngine
            if not self._qdrant: raise RuntimeError("Provider not initialized")
            vector_dim = self._embedder.dim if self._embedder else 384
            engine = GuardrailEngine(self._qdrant, self._collection, vector_dim=vector_dim)
            override_id = engine.record_override(
                command=command,
                matched_rules=matched_rules,
                reasoning=reasoning,
                agent_id=agent_id,
            )
            return {"status": "override_recorded", "override_id": override_id}
        except Exception as exc:
            logger.warning("Guardrail override failed: %s", exc)
            return {"status": "error", "error": str(exc)}

    def _graph_traverse(self, fact_id: str, max_depth: int = 3,
                        relation: Optional[str] = None,
                        target_type: Optional[str] = None) -> Dict[str, Any]:
        """Multi-hop graph traversal from a starting fact."""
        try:
            from nexus.graph.graph import SkillGraph
            from nexus.graph.traversal import GraphTraversal
            if not self._qdrant: raise RuntimeError("Provider not initialized")
            sg = SkillGraph(
                qdrant_url=f"http://{_HOST}:{_PORT}",
                collection=self._collection,
            )
            sg.initialize()
            gt = GraphTraversal(sg)
            results = gt.traverse(fact_id, max_depth=max_depth, relation=relation, target_type=target_type)
            sg.store.close()
            return {"results": results}
        except Exception as exc:
            logger.warning("Graph traverse failed: %s", exc)
            return {"status": "error", "error": str(exc)}

    def _find_entities(self, entity_type: Optional[str] = None,
                       limit: int = 50) -> Dict[str, Any]:
        """Find all entity-typed memories in Qdrant."""
        try:
            from nexus.graph.graph import SkillGraph
            from nexus.graph.traversal import GraphTraversal
            if not self._qdrant: raise RuntimeError("Provider not initialized")
            sg = SkillGraph(
                qdrant_url=f"http://{_HOST}:{_PORT}",
                collection=self._collection,
            )
            sg.initialize()
            gt = GraphTraversal(sg)
            results = gt.find_entities(entity_type=entity_type, limit=limit)
            sg.store.close()
            return {"entities": results}
        except Exception as exc:
            logger.warning("Find entities failed: %s", exc)
            return {"status": "error", "error": str(exc)}

    def _get_subgraph(self, fact_id: str, max_depth: int = 2) -> Dict[str, Any]:
        """Get a subgraph centered on a fact."""
        try:
            from nexus.graph.graph import SkillGraph
            from nexus.graph.traversal import GraphTraversal
            if not self._qdrant: raise RuntimeError("Provider not initialized")
            sg = SkillGraph(
                qdrant_url=f"http://{_HOST}:{_PORT}",
                collection=self._collection,
            )
            sg.initialize()
            gt = GraphTraversal(sg)
            result = gt.get_subgraph(fact_id, max_depth=max_depth)
            sg.store.close()
            return result
        except Exception as exc:
            logger.warning("Get subgraph failed: %s", exc)
            return {"status": "error", "error": str(exc)}

    def _get_related(self, fact_id: str, relation: Optional[str] = None) -> Dict[str, Any]:
        """Get directly related facts (1-hop)."""
        try:
            from nexus.graph.graph import SkillGraph
            from nexus.graph.traversal import GraphTraversal
            if not self._qdrant: raise RuntimeError("Provider not initialized")
            sg = SkillGraph(
                qdrant_url=f"http://{_HOST}:{_PORT}",
                collection=self._collection,
            )
            sg.initialize()
            gt = GraphTraversal(sg)
            results = gt.get_related(fact_id, relation=relation)
            sg.store.close()
            return {"results": results}
        except Exception as exc:
            logger.warning("Get related failed: %s", exc)
            return {"status": "error", "error": str(exc)}

    def _cost_routing_stats(self) -> Dict[str, Any]:
        """Get cost-aware routing statistics."""
        try:
            from nexus_memory.cost_router import CostAwareRouter
            router = CostAwareRouter(hermes_home=self._hermes_home)
            router.initialize()
            return router.stats()
        except Exception as exc:
            logger.warning("Cost routing stats failed: %s", exc)
            return {"status": "error", "error": str(exc)}

    def _cost_routing_explain(self, category: str) -> Dict[str, Any]:
        """Explain the routing decision for a memory category."""
        try:
            from nexus_memory.cost_router import CostAwareRouter
            router = CostAwareRouter(hermes_home=self._hermes_home)
            router.initialize()
            return {"explanation": router.explain(category)}
        except Exception as exc:
            logger.warning("Cost routing explain failed: %s", exc)
            return {"status": "error", "error": str(exc)}

    def _sica_run(self, auto_patch: bool = True) -> Dict[str, Any]:
        """Run a SICA self-improvement cycle."""
        try:
            from nexus.sica import run_sica, _get_config
            if not self._qdrant: raise RuntimeError("Provider not initialized")
            # Pass our embedder to avoid dimension mismatch in session storage
            result = run_sica(client=self._qdrant, collection=self._collection,
                            auto_patch=auto_patch, embedder=self._embedder)
            cfg = _get_config()
            return result.to_dict(max_suggestions=cfg["max_suggestions"])
        except Exception as exc:
            logger.warning("SICA run failed: %s", exc)
            return {"status": "error", "error": str(exc)}

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return [RECALL_SCHEMA, REMEMBER_SCHEMA, FORGET_SCHEMA,
                GUARDRAIL_CHECK_SCHEMA, GUARDRAIL_OVERRIDE_SCHEMA,
                GRAPH_TRAVERSE_SCHEMA, FIND_ENTITIES_SCHEMA,
                GET_SUBGRAPH_SCHEMA, GET_RELATED_SCHEMA,
                COST_ROUTING_STATS_SCHEMA, COST_ROUTING_EXPLAIN_SCHEMA,
                SICA_RUN_SCHEMA]

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs: Any) -> str:
        try:
            if tool_name == "nexus_recall":
                result = self._recall(args.get("query", ""), args.get("limit", 5),
                                      as_of=args.get("as_of", ""))
            elif tool_name == "nexus_remember":
                result = self._upsert(text=args.get("text", ""), category=args.get("category", "fact"),
                                      access_level=args.get("access_level", "public"),
                                      source=args.get("source", ""))
                # Roadmap 1.1/4.1: auto-enrich with entities + edges (async, fail-open)
                try:
                    self._enqueue_entity_extraction(args.get("text", ""))
                except Exception as exc:
                    logger.warning("Auto entity enrichment skipped: %s", exc)
            elif tool_name == "nexus_forget":
                result = self._forget(args.get("memory_id", ""))
            elif tool_name == "nexus_guardrail_check":
                result = self._guardrail_check(
                    args.get("command", ""),
                    args.get("tool_name", ""),
                    args.get("tool_input", {}),
                )
            elif tool_name == "nexus_guardrail_override":
                reasoning = args.get("reasoning", "").strip()
                if not reasoning or len(reasoning) < 10:
                    result = {"status": "error", "error": "Override requires explicit reasoning (min 10 chars)."}
                else:
                    result = self._guardrail_override(
                        args.get("command", ""),
                        args.get("matched_rules", []),
                        reasoning,
                        args.get("agent_id", "unknown"),
                    )
            elif tool_name == "nexus_graph_traverse":
                result = self._graph_traverse(
                    args.get("fact_id", ""),
                    args.get("max_depth", 3),
                    args.get("relation") or None,
                    args.get("target_type") or None,
                )
            elif tool_name == "nexus_find_entities":
                result = self._find_entities(
                    args.get("entity_type") or None,
                    args.get("limit", 50),
                )
            elif tool_name == "nexus_get_subgraph":
                result = self._get_subgraph(
                    args.get("fact_id", ""),
                    args.get("max_depth", 2),
                )
            elif tool_name == "nexus_get_related":
                result = self._get_related(
                    args.get("fact_id", ""),
                    args.get("relation") or None,
                )
            elif tool_name == "nexus_cost_routing_stats":
                result = self._cost_routing_stats()
            elif tool_name == "nexus_cost_routing_explain":
                result = self._cost_routing_explain(args.get("category", "fact"))
            elif tool_name == "nexus_sica_run":
                result = self._sica_run(args.get("auto_patch", True))
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

    def _enqueue_entity_extraction(self, text: str, source: str = "nexus_remember") -> None:
        """Roadmap 1.1/4.1: Auto-enrich nexus_remember with entities + edges.

        Runs entity extraction in a daemon thread so the tool call returns
        immediately. Fail-open: any extraction failure is logged and dropped.
        Short texts are skipped (noise guard). Single-flight: only one
        extraction runs at a time (skip instead of queue-storm).
        """
        if not text or len(text.strip()) < 80:
            return
        # Opt-out (review F7): NEXUS_AUTO_ENRICH=0 disables enrichment entirely
        if os.environ.get("NEXUS_AUTO_ENRICH", "1").strip().lower() in ("0", "false", "no", "off"):
            return
        # Efficiency (review): hash-dedup BEFORE lock so duplicate texts don't
        # block the single-flight slot with a no-op extraction.
        import hashlib
        h = hashlib.sha256(text.encode("utf-8", "ignore")[:4000]).hexdigest()[:16]
        seen = getattr(self, "_extract_hashes", None)
        if seen is None:
            seen = self._extract_hashes = set()
        if h in seen:
            return
        seen.add(h)
        if len(seen) > 500:
            seen.clear(); seen.add(h)
        if not self._entity_extract_lock.acquire(blocking=False):
            return  # one flight at a time; skipping is better than stacking

        def _run():
            try:
                self._extract_entities_from_text(text, source=source)
            except Exception as exc:
                logger.warning("Auto entity extraction failed: %s", exc)
            finally:
                self._entity_extract_lock.release()
        try:
            threading.Thread(target=_run, name="nexus-entity-extract", daemon=True).start()
        except Exception as exc:
            # Thread spawn failed: release the lock or enrichment dies forever
            logger.warning("Entity extraction thread start failed: %s", exc)
            self._entity_extract_lock.release()

    def _maybe_extract_hardware_entities(self, text: str, session_id: str) -> None:
        """Hardware-Pattern (Nebo 30.08.2026): "ich habe X", "ich nutze Y" sofort extrahieren.

        Triggert NUR auf deklarative Hardware-Sätze, niemals auf Fragen ("Hast du...?").
        Speichert als nexus_remember mit confidence=0.9 (User-deklariert, kein LLM-Guess).
        """
        # Skip Fragensätze (beginnen mit Fragewort oder haben Fragezeichen-Pattern)
        if text.strip().startswith(("Hast", "Kannst", "Bist", "Wie ", "Was ", "Wo ", "Warum ")):
            return

        # Hardware-Keywords die Entity-Extraktion auslösen
        hw_keywords = ["Bose", "Razer", "Mikrofon", "Mikro", "USB", "Bluetooth", "BT", "Lautsprecher",
                       "Headset", "Kopfhörer", "SoundLink", "Webcam", "Monitor", "Tastatur", "Maus"]

        if not any(kw.lower() in text.lower() for kw in hw_keywords):
            return

        # Text auf 500 Zeichen begrenzen (Kosten + Signal-Rausch-Verhältnis)
        snippet = text[:500]
        try:
            er = self._extract_entities_from_text(snippet, source="auto-hardware-detection")
            if er.get("entities", 0) > 0:
                logger.info("Auto-hardware extraction: %d entities aus User-Aussage gespeichert",
                            er.get("entities"))
        except Exception as exc:
            logger.warning("Hardware-auto-extract failed: %s", exc)

    def _extract_entities_from_text(self, text: str, source: str = "nexus_remember",
                                     access_level: str = "public") -> Dict[str, Any]:
        """Roadmap 1.1/4.1: extract entities + edges from text and store them.

        Shared by auto-enrich (nexus_remember) and session-end extraction.
        access_level propagates from the source memory so private content
        never leaks into public entity points (4-bot review F2).
        Fail-open per item; returns summary dict.
        """
        if not text or not text.strip() or not self._qdrant:
            return {"entities": 0, "edges": 0}
        from nexus_memory.entity_extractor import extract_entities
        result = extract_entities(text[:4000], hermes_home=self._hermes_home)
        if result.is_empty():
            return {"entities": 0, "edges": 0}
        entity_ids: Dict[str, str] = {}
        for entity in result.entities:
            if self._write_stop.is_set() or not self._qdrant:
                break
            try:
                store_result = self._upsert_entity(entity, access_level=access_level, source=source)
                entity_ids[entity.name] = store_result["id"]
            except Exception as exc:
                logger.warning("Entity store failed: %s", exc)
        edge_count = 0
        store = None
        try:
            from nexus.graph.store import EdgeStore
            store = EdgeStore(qdrant_url=f"{_HOST}:{_PORT}", collection=self._collection)
        except Exception as exc:
            logger.warning("EdgeStore init failed: %s", exc)
        try:
            for rel in result.relationships:
                if self._write_stop.is_set() or not self._qdrant:
                    break
                source_id = entity_ids.get(rel.source)
                target_id = entity_ids.get(rel.target)
                if not source_id or not target_id:
                    continue
                if store is None:
                    break
                try:
                    store.add_edge(
                        source_fact_id=source_id,
                        target_fact_id=target_id,
                        relation=rel.relation,
                        reason=source,
                        metadata={"confidence": rel.confidence},
                    )
                    edge_count += 1
                except Exception as exc:
                    logger.warning("Relationship store failed: %s", exc)
        finally:
            if store is not None:
                try:
                    store.close()
                except Exception:
                    pass
        if entity_ids or edge_count:
            logger.info("Auto-enrich stored %d entities, %d edges", len(entity_ids), edge_count)
        return {"entities": len(entity_ids), "edges": edge_count}

    def on_session_end(self, messages: List[Dict[str, Any]]) -> None:
        """Extract and persist durable facts at session end.

        Called by MemoryManager when a session ends (CLI exit, /reset, gateway
        session expiry). Uses the Session→Memory Pipeline (extractor.py) to
        identify durable facts, then stores them with proper categorization
        and confidence. Runs inline (MemoryManager already provides background
        execution via its single-worker executor for /new and /reset paths).
        """
        if self._agent_context != "primary":
            return
        if not messages:
            return
        self._extract_and_store(list(messages))

    def _extract_and_store(self, messages: List[Dict[str, Any]]) -> None:
        """Background extraction: LLM first, heuristic fallback, store in Qdrant.

        Extracts both durable facts (via extractor.py) and entities/relationships
        (via entity_extractor.py). Entities are stored as Qdrant points with
        category="entity". Relationships are stored as graph edges.
        """
        try:
            # ── Fact extraction ───────────────────────────────────────────
            from nexus_memory.extractor import extract_facts
            facts = extract_facts(messages, hermes_home=self._hermes_home)
            if facts:
                if self._write_stop.is_set() or not self._qdrant:
                    return
                stored = 0
                for fact in facts:
                    if self._write_stop.is_set() or not self._qdrant:
                        break
                    try:
                        self._upsert(
                            text=fact["text"],
                            category=fact["category"],
                            access_level="public",
                            source="hermes-plugin-session-end",
                            confidence=fact["confidence"],
                        )
                        stored += 1
                    except Exception as exc:
                        logger.warning("Session fact store failed: %s", exc)
                if stored:
                    logger.info(
                        "NexusMemoryProvider on_session_end: extracted+stored %d facts "
                        "(from %d messages)", stored, len(messages),
                    )

            # ── Entity extraction (Knowledge Graph Layer) ─────────────────
            if self._write_stop.is_set() or not self._qdrant:
                return
            try:
                conv_parts = []
                for msg in messages:
                    role = msg.get("role", "")
                    content = msg.get("content", "")
                    if isinstance(content, str) and role in ("user", "assistant") and content.strip():
                        conv_parts.append(content[:2000])
                conv_text = " ".join(conv_parts)[:4000]
                if conv_text:
                    er = self._extract_entities_from_text(
                        conv_text, source="session-end-entity-extraction"
                    )
                    if er.get("entities") or er.get("edges"):
                        logger.info(
                            "NexusMemoryProvider on_session_end: extracted+stored "
                            "%d entities, %d relationships",
                            er.get("entities", 0), er.get("edges", 0),
                        )
            except Exception as exc:
                logger.warning("Entity extraction in on_session_end failed: %s", exc)

        except Exception as exc:
            logger.warning("on_session_end extraction failed: %s", exc)

    def _upsert_entity(self, entity: Any, access_level: str = "public",
                       source: str = "hermes-plugin-session-end") -> Dict[str, Any]:
        """Store an entity as a Qdrant point with category='entity'.

        Uses uuid5 (deterministic) so re-extracting the same entity across
        sessions updates the existing point instead of creating duplicates.
        """
        if not self._embedder or not self._qdrant:
            raise RuntimeError("Provider not initialized")
        # Deterministic ID: same entity_type + name → same point ID
        entity_key = f"{entity.entity_type}:{entity.name}"
        eid = str(uuid.uuid5(uuid.NAMESPACE_DNS, entity_key))
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        text = f"{entity.entity_type}: {entity.name}"
        if entity.attributes:
            attr_str = ", ".join(f"{k}={v}" for k, v in entity.attributes.items())
            text += f" ({attr_str})"
        # Review fix R1: deterministic entity text - cache the embed
        vector = self._embed_cached(text)
        payload = {
            "id": eid,
            "content": text,
            "access_level": access_level,
            "category": "entity",
            "entity_type": entity.entity_type,
            "entity_name": entity.name,
            "entity_attributes": entity.attributes,
            "source": source,
            "source_url": "",
            "created_at": ts,
            "provenance": {
                "source_type": "hermes-plugin",
                "created_by": "nexus-memory-entity-extractor",
                "timestamp": ts,
                "confidence": entity.confidence,
            },
        }
        self._qdrant.upsert(
            collection_name=self._collection,
            points=[qmodels.PointStruct(id=eid, vector=vector, payload=payload)],
        )
        return {"status": "ok", "id": eid, "entity_type": entity.entity_type}

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
