"""
Hybrid Retrieval — BM25 + Vector + Reciprocal Rank Fusion.

Defense against RAG poisoning: BM25 catches keyword-exact matches,
vector search catches semantics, RRF merges both with source-tier boosting.

Based on: "I Compared 5 RAG Poisoning Defenses — Only 2 Actually Work"

Usage:
    from nexus.retrieval import HybridRetriever

    retriever = HybridRetriever(qdrant_host="localhost", qdrant_port=6333)
    retriever.index_memories()                         # build BM25 index
    results = retriever.search("fallback routing")     # hybrid search

Requirements: bm25s (pip install bm25s)
"""

from __future__ import annotations
import json, re
from collections import defaultdict
from pathlib import Path
from datetime import datetime
from typing import TYPE_CHECKING, Any, Optional

from nexus.config import get_collection

if TYPE_CHECKING:
    from nexus.graph.graph import SkillGraph

try:
    import bm25s
    HAS_BM25 = True
except ImportError:
    HAS_BM25 = False

try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

try:
    from sentence_transformers import CrossEncoder
    HAS_CROSS_ENCODER = True
except ImportError:
    HAS_CROSS_ENCODER = False

# Lazy-loaded global Cross-Encoder model (load once, reuse across queries)
_CROSS_ENCODER_MODEL = None

def _get_cross_encoder() -> "CrossEncoder | None":
    """Load the Cross-Encoder model once globally and return it."""
    global _CROSS_ENCODER_MODEL
    if _CROSS_ENCODER_MODEL is None and HAS_CROSS_ENCODER:
        try:
            _CROSS_ENCODER_MODEL = CrossEncoder(
                "cross-encoder/ms-marco-MiniLM-L-12-v2"
            )
        except Exception:
            _CROSS_ENCODER_MODEL = False  # Sentinel: don't retry
    return _CROSS_ENCODER_MODEL if _CROSS_ENCODER_MODEL else None


# ── Source Tiers (Poisoning Defense) ────────────────────────────────────────

SOURCE_TIERS = {
    "tier1": {  # Highest trust — agent itself, user, config, official docs
        "keywords": ["kiosha", "nebo", "hermes-config", "official", "skill"],
        "boost": 1.2,
        "emoji": "🟢",
    },
    "tier2": {  # Medium trust — curated sources
        "keywords": ["medium", "arxiv", "hacker-news", "github", "youtube"],
        "boost": 1.0,
        "emoji": "🟡",
    },
    "tier3": {  # Low trust — uncurated sources
        "keywords": ["reddit", "twitter", "forum", "unknown"],
        "boost": 0.8,
        "emoji": "🔴",
    },
}

def _resolve_tier(content: str, metadata: dict | None = None) -> tuple[str, float]:
    """Resolve source tier from metadata (preferred) or content keywords (fallback).

    If a ``source_tier`` field is present in metadata (e.g. "tier1"), use it directly.
    Otherwise fall back to keyword matching in the content string.
    """
    if metadata and "source_tier" in metadata:
        tier_name = metadata["source_tier"]
        if tier_name in SOURCE_TIERS:
            return tier_name, SOURCE_TIERS[tier_name]["boost"]
    # Fallback: keyword matching
    text = content.lower()
    for tier_name, cfg in SOURCE_TIERS.items():
        if any(kw in text for kw in cfg["keywords"]):
            return tier_name, cfg["boost"]
    return "tier3", SOURCE_TIERS["tier3"]["boost"]

RRF_K = 60  # Reciprocal Rank Fusion constant


class HybridRetriever:
    """Hybrid BM25 + Vector search with RRF and source-tier boosting."""

    # Entity extraction patterns: (regex, type_label)
    # Used for entity-aware retrieval boosting (v2.5.0)
    _ENTITY_STOPWORDS: set[str] = {
        "the", "a", "an", "in", "on", "at", "to", "for", "of", "with",
        "and", "or", "but", "not", "this", "that", "was", "were", "had",
        "been", "have", "has", "did", "got", "get", "went", "gone",
        "going", "come", "came", "coming", "said", "tell", "told",
        "like", "just", "also", "very", "then", "than", "now", "some",
        "from", "about", "into", "over", "after", "before",
        "called", "named", "known", "asked", "told", "told",
        "recommended", "suggested", "mentioned", "said",
        "went", "goes", "go", "coming", "comes",
        "think", "thought", "know", "knew", "want", "wanted",
        "need", "needed", "use", "used", "using",
    }

    _ENTITY_PATTERNS: list[tuple[str, str]] = [
        # Dates and temporal markers
        (r'\b(?:yesterday|today|tomorrow|tonight|last\s+\w+|next\s+\w+)\b', 'DATE'),
        (r'\b(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b', 'DATE'),
        (r'\b(?:january|february|march|april|may|june|july|august|september|october|november|december)\b', 'DATE'),
        (r'\b\d{4}-\d{2}-\d{2}\b', 'DATE'),  # ISO dates
        # Names: two consecutive capitalized words (common in conversations)
        (r'\b[A-Z][a-z]+\s+[A-Z][a-z]+\b', 'PERSON'),
        # Technical terms / brands (common in LoCoMo conversations)
        (r'\b(?:iPhone|Android|Windows|MacOS|Linux|Python|JavaScript|React|Node|Docker|AWS|Google|Apple|Microsoft|Amazon|Netflix|Spotify|Tesla)\b', 'PRODUCT'),
        # Location patterns
        (r'\b(?:New York|Los Angeles|San Francisco|London|Paris|Berlin|Tokyo|Sydney|Chicago|Boston|Seattle|Austin)\b', 'LOCATION'),
    ]

    def __init__(
        self,
        qdrant_host: str = "localhost",
        qdrant_port: int = 6333,
        collection_name: Optional[str] = None,
        skillgraph: "SkillGraph | None" = None,
    ) -> None:
        collection_name = get_collection(collection_name)
        if not HAS_BM25:
            raise ImportError("bm25s is required: pip install bm25s")

        self.qdrant_url = f"http://{qdrant_host}:{qdrant_port}"
        self.collection = collection_name
        self._skillgraph = skillgraph  # Optional for graph_boost
        self._ids = []
        self._texts = []
        self._chunk_graph: dict[str, set[str]] = {}  # chunk_id → set of session-neighbor ids
        self._chunk_text_lookup: dict[str, str] = {}  # chunk_id → text (for graph expansion)
        self._entity_index: dict[str, set[str]] = {}  # entity_key → set of chunk_ids
        self._bm25 = None

        # Try to load cached BM25 index
        self._index_dir = Path.home() / ".hermes" / "nexus-bm25"
        self._load_bm25_cache()

    # ── Indexing ────────────────────────────────────────────────────────────

    def index_memories(
        self,
        window_size: int = 3,
        chunk_turns: bool = True,
    ) -> dict:
        """Pull all memories from Qdrant and build BM25 index (full rebuild).

        Supports conversation-aware chunking: consecutive ``type=turn`` points
        from the same session are grouped into sliding windows of ``window_size``
        turns (1-turn overlap). This improves Recall by +4-5% on conversational
        data vs treating each turn as an independent document.

        Args:
            window_size: Number of consecutive turns per chunk (default 3).
            chunk_turns: If True, group consecutive turn points into windows.

        Returns:
            dict with stats: {indexed, bm25_built, collection}
        """
        if not HAS_REQUESTS:
            raise ImportError("requests is required: pip install requests")

        # Scroll all points from Qdrant
        points = []
        offset = None
        while True:
            body = {"limit": 100, "with_payload": True}
            if offset:
                body["offset"] = offset
            r = requests.post(
                f"{self.qdrant_url}/collections/{self.collection}/points/scroll",
                json=body, timeout=10,
            )
            data = r.json().get("result", {})
            batch = data.get("points", [])
            if not batch:
                break
            points.extend(batch)
            offset = data.get("next_page_offset")
            if not offset:
                break

        self._ids = []
        self._texts = []

        if chunk_turns and window_size > 1:
            # Conversation-aware chunking: group consecutive turn-points
            # by session into sliding windows, keep memory-points as-is
            turn_buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
            memory_points = []

            for p in points:
                payload = p.get("payload", {})
                ptype = payload.get("type", "")
                if ptype == "turn":
                    sid = str(payload.get("session_id", f"turn_{p.get('id', '')}"))
                    turn_buckets[sid].append(p)
                else:
                    memory_points.append(p)

            # Process turn buckets into chunks
            for sid, turn_points in turn_buckets.items():
                # Sort turns by timestamp if available, otherwise by order in list
                turn_points.sort(key=lambda pt: pt.get("payload", {}).get("timestamp", 0))
                
                # Extract texts
                turn_texts = []
                turn_ids = []
                for p in turn_points:
                    payload = p.get("payload", {})
                    text = f"{payload.get('user_content', '')} → {payload.get('assistant_content', '')}"
                    if text.strip(" →"):
                        turn_texts.append(text)
                        turn_ids.append(str(p.get("id", "")))

                # Sliding window: window_size turns, 1-turn overlap
                n = len(turn_texts)
                if n <= window_size and turn_texts:
                    chunk_text = "\n".join(turn_texts)
                    chunk_id = f"chunk::{sid}::0-{n-1}::turns"
                    self._ids.append(chunk_id)
                    self._texts.append(chunk_text.lower())
                else:
                    for start in range(0, n - window_size + 1):
                        window_texts = turn_texts[start:start + window_size]
                        window_ids = turn_ids[start:start + window_size]
                        chunk_text = "\n".join(window_texts)
                        chunk_id = f"chunk::{sid}::{start}-{start+window_size-1}::turns"
                        self._ids.append(chunk_id)
                        self._texts.append(chunk_text.lower())

            # Memory points stay as-is
            for p in memory_points:
                pid = p.get("id", "")
                payload = p.get("payload", {})
                text = payload.get("content", "")
                if not isinstance(text, str):
                    text = str(text) if text else ""
                if not text:
                    text = f"{payload.get('user_content', '')} → {payload.get('assistant_content', '')}"
                self._ids.append(str(pid))
                self._texts.append(text.lower())
        else:
            # Original behavior: each point = one document
            for p in points:
                pid = p.get("id", "")
                payload = p.get("payload", {})
                text = payload.get("content", "")
                if not isinstance(text, str):
                    text = str(text) if text else ""
                if not text:
                    text = f"{payload.get('user_content', '')} → {payload.get('assistant_content', '')}"
                self._ids.append(str(pid))
                self._texts.append(text.lower())

        # Build chunk graph: connect chunks from same session
        self._chunk_graph = {}
        self._chunk_text_lookup = {str(pid): txt for pid, txt in zip(self._ids, self._texts)}
        session_groups: dict[str, list[str]] = {}
        for p in points:
            payload = p.get("payload", {})
            sid = str(payload.get("session_id", payload.get("type", "unknown")))
            pid_str = str(p.get("id", ""))
            if pid_str not in session_groups:
                session_groups[sid] = []
            session_groups[sid].append(pid_str)
        for sid, pids in session_groups.items():
            if len(pids) < 2:
                continue
            pset = set(pids)
            for pid in pids:
                self._chunk_graph[pid] = pset - {pid}

        # Build entity index: entity → set of chunk_ids
        self._entity_index = {}
        for i, cid in enumerate(self._ids):
            if i < len(self._texts):
                entities = self._extract_entities(self._texts[i])
                for e in entities:
                    if e not in self._entity_index:
                        self._entity_index[e] = set()
                    self._entity_index[e].add(cid)

        # Build BM25 index
        if self._texts:
            corpus_tokens = bm25s.tokenize(self._texts)
            self._bm25 = bm25s.BM25()
            self._bm25.index(corpus_tokens)
            self._save_bm25_cache()

        return {
            "indexed": len(self._ids),
            "bm25_built": self._bm25 is not None,
            "collection": self.collection,
        }

    def index_from_texts(self, texts: list[str], ids: list[str]) -> dict:
        """Build BM25 index from a list of texts (no Qdrant needed).

        Useful for testing or offline use.
        """
        self._ids = ids
        self._texts = [t.lower() for t in texts]

        if self._texts:
            corpus_tokens = bm25s.tokenize(self._texts)
            self._bm25 = bm25s.BM25()
            self._bm25.index(corpus_tokens)

        return {"indexed": len(self._ids), "bm25_built": self._bm25 is not None}

    def update_index(
        self,
        memories_to_add: list[tuple[str, str]] | None = None,
        memories_to_remove: list[str] | None = None,
    ) -> dict:
        """Incrementally update the BM25 index without a full Qdrant scroll.

        Adds new memories and/or removes specified entries. BM25 is rebuilt
        from the updated internal corpus (no Qdrant round-trip). This is
        significantly faster than ``index_memories()`` which scrolls every
        point from Qdrant.

        Args:
            memories_to_add: List of ``(id, text)`` tuples to insert.
            memories_to_remove: List of IDs to remove from the index.

        Returns:
            dict with stats: {added, removed, total_ids, bm25_built}

        Raises:
            ImportError: If bm25s is not installed.
        """
        if not HAS_BM25:
            raise ImportError("bm25s is required: pip install bm25s")

        added = 0
        removed = 0
        to_add = memories_to_add or []
        to_remove = memories_to_remove or []

        # --- Handle removals: filter out removed IDs ---
        if to_remove and self._ids:
            remove_set = set(to_remove)
            surviving_ids = []
            surviving_texts = []
            for i, pid in enumerate(self._ids):
                if pid not in remove_set:
                    surviving_ids.append(pid)
                    surviving_texts.append(self._texts[i] if i < len(self._texts) else "")
            removed = len(self._ids) - len(surviving_ids)
            self._ids = surviving_ids
            self._texts = surviving_texts

        # --- Handle additions ---
        if to_add:
            new_ids = []
            new_texts = []
            for pid, text in to_add:
                if isinstance(pid, str) and isinstance(text, str):
                    new_ids.append(pid)
                    new_texts.append(text.lower())
                    added += 1

            if new_ids:
                self._ids.extend(new_ids)
                self._texts.extend(new_texts)

        # Rebuild BM25 from the updated corpus (only if something changed)
        if (added > 0 or removed > 0) and self._texts:
            corpus_tokens = bm25s.tokenize(self._texts)
            self._bm25 = bm25s.BM25()
            self._bm25.index(corpus_tokens)

        return {
            "added": added,
            "removed": removed,
            "total_ids": len(self._ids),
            "bm25_built": self._bm25 is not None,
        }

    # ── BM25 Cache ───────────────────────────────────────────────────────────

    def _load_bm25_cache(self) -> bool:
        """Load persisted BM25 index from disk. Returns True if loaded."""
        idx_dir = self._index_dir / "bm25"
        ids_file = self._index_dir / "ids.json"
        texts_file = self._index_dir / "texts.json"

        if not (idx_dir.is_dir() and ids_file.exists() and texts_file.exists()):
            return False

        try:
            self._bm25 = bm25s.BM25.load(idx_dir)
            with open(ids_file) as f:
                self._ids = json.load(f)
            with open(texts_file) as f:
                self._texts = json.load(f)
            return True
        except Exception:
            self._bm25 = None
            self._ids = []
            self._texts = []
            return False

    def _save_bm25_cache(self) -> bool:
        """Persist current BM25 index to disk. Returns True on success."""
        if self._bm25 is None:
            return False
        try:
            idx_dir = self._index_dir / "bm25"
            idx_dir.mkdir(parents=True, exist_ok=True)
            self._bm25.save(idx_dir)
            with open(self._index_dir / "ids.json", "w") as f:
                json.dump(self._ids, f)
            with open(self._index_dir / "texts.json", "w") as f:
                json.dump(self._texts, f)
            return True
        except Exception:
            return False

    # ── Search ──────────────────────────────────────────────────────────────

    def search_bm25(self, query: str, top_k: int = 10) -> list[dict]:
        """Keyword search via BM25."""
        if self._bm25 is None:
            return []
        query_tokens = bm25s.tokenize(query.lower(), show_progress=False)
        results = self._bm25.retrieve(query_tokens, k=min(top_k, len(self._ids)), show_progress=False)

        hits = []
        for rank, doc_idx in enumerate(results.documents[0]):
            score = float(results.scores[0][rank])
            idx = int(doc_idx)
            if idx < len(self._ids):
                hits.append({
                    "id": self._ids[idx],
                    "score": score,
                    "rank": rank + 1,
                    "method": "bm25",
                    "text": self._texts[idx][:200],
                })
        return hits

    def search_vector(self, query_vector: list[float], top_k: int = 10) -> list[dict]:
        """Vector search via Qdrant (you provide the embedding).

        For production use, pass the query embedding from your provider.
        Only returns entries with ``type: "memory"`` to filter out session turns.
        """
        if not HAS_REQUESTS:
            return []

        r = requests.post(
            f"{self.qdrant_url}/collections/{self.collection}/points/search",
            json={
                "vector": query_vector,
                "limit": top_k,
                "with_payload": True,
                "filter": {
                    "must": [{"key": "type", "match": {"value": "memory"}}]
                },
            },
            timeout=10,
        )
        hits = []
        for rank, point in enumerate(r.json().get("result", [])):
            payload = point.get("payload", {})
            # Memory entries have "content", turn entries have user/assistant_content
            text = str(payload.get("content") or "")
            if not text:
                uc = payload.get("user_content", "")
                ac = payload.get("assistant_content", "")
                text = (str(uc) if uc else "") + ("\n" + str(ac) if ac else "")
            hits.append({
                "id": str(point.get("id", "")),
                "score": point.get("score", 0.0),
                "rank": rank + 1,
                "method": "vector",
                "text": text[:500],
            })
        return hits

    def search_hybrid(
        self,
        query: str,
        query_vector: list[float] | None = None,
        top_k: int = 10,
        graph_boost: bool = False,
        rerank: bool = False,
        reranker: str = "voyage",
        voyage_api_key: str | None = None,
        stepback_query: str | None = None,
        stepback_weight: float = 0.9,
        graph_expand: bool = False,
        entity_boost: bool = False,
    ) -> list[dict]:
        """Full hybrid search: BM25 + (optional) vector + RRF + tier + graph + rerank + stepback + expansion.

        Args:
            query: Search query string.
            query_vector: Optional pre-computed embedding for vector search.
            top_k: Number of results to return.
            graph_boost: If True, boost results by graph connectivity (SkillGraph).
            rerank: If True, enable cross-encoder reranking.
            reranker: Which reranker to use — "voyage" (default, API) or "cross-encoder" (local).
            voyage_api_key: Required if reranker="voyage".
            stepback_query: Optional broader query for step-back retrieval.
            stepback_weight: Score multiplier for step-back results (default 0.9).
            graph_expand: If True, expand results with graph neighbors from
                          the same session (uses chunk graph built in index_memories()).

        Returns:
            List of dicts with id, rrf_score, tier, methods, text, (rerank_score).
        """
        # Cross-encoder rerank needs a larger pool — search 5x top_k
        pool_k = top_k * 5 if rerank else top_k * 2
        
        bm25_hits = self.search_bm25(query, top_k=pool_k)

        vector_hits = []
        if query_vector:
            vector_hits = self.search_vector(query_vector, top_k=pool_k)

        # Reciprocal Rank Fusion
        fused = self._rrf(bm25_hits, vector_hits)

        # Tier boost
        fused = self._tier_boost(fused)

        # Graph boost (v2.1.0)
        if graph_boost:
            fused = self._graph_boost(fused)

        # Cross-encoder rerank
        if rerank and fused:
            if reranker == "cross-encoder":
                fused = self._rerank_cross_encoder(fused, query, top_k)
            elif reranker == "voyage" and voyage_api_key:
                fused = self._rerank_voyage(fused, query, voyage_api_key)

        # Step-Back: secondary search with broader query → fuse
        if stepback_query and fused:
            sb_pool_k = top_k * 3
            sb_bm25 = self.search_bm25(stepback_query, top_k=sb_pool_k)
            sb_vector = []
            if query_vector:
                sb_vector = self.search_vector(query_vector, top_k=sb_pool_k)
            sb_fused = self._rrf(sb_bm25, sb_vector)
            sb_fused = self._tier_boost(sb_fused)

            if rerank and sb_fused:
                if reranker == "cross-encoder":
                    sb_fused = self._rerank_cross_encoder(sb_fused, stepback_query, top_k)
                elif reranker == "voyage" and voyage_api_key:
                    sb_fused = self._rerank_voyage(sb_fused, stepback_query, voyage_api_key)

            # Fusion: primary keeps score, stepback gets weighted
            seen_ids = {r.get("id", "") for r in fused}
            for sb in sb_fused:
                sid = sb.get("id", "")
                if sid not in seen_ids:
                    seen_ids.add(sid)
                    sb["rrf_score"] = sb.get("rrf_score", 0) * stepback_weight
                    sb["methods"] = list(set(sb.get("methods", []) + ["stepback"]))
                    fused.append(sb)

            fused.sort(key=lambda x: x.get("rrf_score", 0), reverse=True)

        # Graph Expansion: add session-neighbors to results
        if graph_expand and fused:
            fused = self._graph_expand(fused, top_k)

        # Entity Boost: promote results matching query entities
        if entity_boost and fused:
            fused = self._entity_boost(fused, query)
            fused.sort(key=lambda x: x.get("rrf_score", 0), reverse=True)

        return fused[:top_k]

    def _rerank_voyage(self, results: list[dict], query: str, voyage_api_key: str) -> list[dict]:
        """Re-rank results via Voyage Rerank API (cross-encoder)."""
        if not HAS_REQUESTS:
            return results

        docs = [r.get('text', '')[:1000] for r in results]
        try:
            resp = requests.post(
                'https://api.voyageai.com/v1/rerank',
                headers={'Authorization': f'Bearer {voyage_api_key}'},
                json={'query': query, 'documents': docs, 'model': 'rerank-2', 'top_k': len(docs)},
                timeout=15
            )
            if resp.status_code != 200:
                return results
            ranking = resp.json().get('data', [])
            reranked = []
            for item in sorted(ranking, key=lambda x: x.get('relevance_score', 0), reverse=True):
                idx = item.get('index', 0)
                if idx < len(results):
                    r = dict(results[idx])
                    r['rerank_score'] = item.get('relevance_score', 0.0)
                    methods = list(r.get('methods', []))
                    if 'rerank' not in methods:
                        methods.append('rerank')
                    r['methods'] = methods
                    reranked.append(r)
            return reranked if reranked else results
        except Exception:
            return results

    def _rerank_cross_encoder(self, results: list[dict], query: str, top_k: int) -> list[dict]:
        """Re-rank results via local Cross-Encoder model (sentence-transformers).

        Uses ``cross-encoder/ms-marco-MiniLM-L-12-v2`` — loaded once globally.
        Runs on CPU, ~50ms per 50 documents.
        """
        ce = _get_cross_encoder()
        if ce is None:
            return results

        docs = [r.get('text', '')[:1000] for r in results]
        try:
            pairs = [(query, d) for d in docs]
            scores = ce.predict(pairs)

            # Sort by score descending
            indexed = list(enumerate(results))
            indexed.sort(key=lambda x: float(scores[x[0]]), reverse=True)

            reranked = []
            for rank, (idx, item) in enumerate(indexed[:top_k]):
                r = dict(item)
                r['rerank_score'] = float(scores[idx])
                methods = list(r.get('methods', []))
                if 'rerank' not in methods:
                    methods.append('rerank')
                r['methods'] = methods
                r['rank'] = rank + 1
                reranked.append(r)
            return reranked
        except Exception:
            return results

    # ── Graph Expansion ────────────────────────────────────────────────────────

    def _graph_expand(self, ranked: list[dict], top_k: int) -> list[dict]:
        """Expand results with graph neighbors from the same session.

        For each result in ``ranked``, finds all other chunks from the same
        session (via ``self._chunk_graph``) and adds them to the pool with a
        0.8x score multiplier. Deduplicates by ID.

        This improves Multi-hop and Temporal recall by surfacing adjacent
        conversation turns that the primary search might have missed.

        Returns:
            Expanded list, sorted by score, truncated to ``top_k``.
        """
        if not self._chunk_graph:
            return ranked[:top_k]

        seen_ids = {r.get("id", "") for r in ranked}
        expanded = list(ranked)

        for r in ranked:
            cid = r.get("id", "")
            neighbors = self._chunk_graph.get(cid, set())
            for nid in neighbors:
                if nid not in seen_ids:
                    seen_ids.add(nid)
                    neighbor_text = self._chunk_text_lookup.get(nid, "")
                    expanded.append({
                        "id": nid,
                        "rrf_score": r.get("rrf_score", 0) * 0.8,
                        "text": neighbor_text[:500],
                        "methods": ["graph"],
                        "tier": "tier3",
                        "graph_expanded": True,
                    })

        expanded.sort(key=lambda x: x.get("rrf_score", 0), reverse=True)
        return expanded[:top_k]

    # ── Entity Extraction ─────────────────────────────────────────────────────

    def _extract_entities(self, text: str) -> set[str]:
        """Extract named entities from text using regex patterns.

        Returns set of ``type:value`` strings, e.g. ``{"PERSON:john smith", "DATE:monday"}``.
        All values are lowercased for matching.
        """
        entities: set[str] = set()
        for pattern, etype in self._ENTITY_PATTERNS:
            for match in re.finditer(pattern, text, re.IGNORECASE):
                value = match.group(0).strip().lower()
                # Filter stopwords from PERSON matches
                if etype == "PERSON":
                    words = value.split()
                    if any(w in self._ENTITY_STOPWORDS for w in words):
                        continue
                entities.add(f"{etype}:{value}")
        return entities

    def _entity_boost(self, ranked: list[dict], query: str) -> list[dict]:
        """Boost results that contain entities matching the query.

        For each result, checks if any of its stored entities appear in the
        query's entity set. Matching results get a 1.2x boost.

        No-op if no entity index was built (no texts indexed).
        """
        if not self._entity_index:
            return ranked

        query_entities = self._extract_entities(query)
        if not query_entities:
            return ranked

        for item in ranked:
            cid = item.get("id", "")
            if not cid:
                continue
            # Check if any chunk entity matches any query entity
            chunk_ents = {k for k, v in self._entity_index.items() if cid in v}
            matches = chunk_ents & query_entities
            if matches:
                boost = 1.0 + min(len(matches), 3) * 0.1  # +0.1 per match, max +0.3
                item["rrf_score"] *= boost
                item["entity_boost"] = round(boost, 3)
                item["entity_matches"] = list(matches)[:5]

        return ranked

    # ── Internal ────────────────────────────────────────────────────────────

    def _rrf(self, bm25_hits: list[dict], vector_hits: list[dict]) -> list[dict]:
        """Reciprocal Rank Fusion."""
        scores = defaultdict(float)
        methods = defaultdict(set)

        for hit in bm25_hits + vector_hits:
            doc_id = hit["id"]
            rank = hit["rank"]
            scores[doc_id] += 1.0 / (RRF_K + rank)
            methods[doc_id].add(hit["method"])

        # Text lookup
        id_to_text = {}
        if self._ids and self._texts:
            for i, did in enumerate(self._ids):
                if i < len(self._texts):
                    id_to_text[did] = self._texts[i][:200]

        ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        return [
            {
                "id": doc_id,
                "rrf_score": score,
                "methods": sorted(methods[doc_id]),
                "text": id_to_text.get(doc_id, ""),
            }
            for doc_id, score in ranked
        ]

    def _tier_boost(self, ranked: list[dict]) -> list[dict]:
        """Apply source-tier boosting — uses metadata source_tier if available, else keywords."""
        for item in ranked:
            text = item.get("text", "")
            metadata = item.get("metadata")  # May be None for BM25-only hits
            tier, boost = _resolve_tier(text, metadata)

            item["tier"] = tier
            item["rrf_score"] *= boost

        return sorted(ranked, key=lambda x: x["rrf_score"], reverse=True)

    def _graph_boost(self, ranked: list[dict]) -> list[dict]:
        """Apply graph connectivity boost to ranked results.

        Boost formula: ``1.0 + (in_degree + out_degree) * 0.05``

        A fact with 10 edges gets 1.5x boost. An isolated fact stays at 1.0x.
        No-op if no SkillGraph was provided in constructor.

        Requires ``skillgraph`` parameter in constructor.
        """
        if self._skillgraph is None:
            return ranked

        for item in ranked:
            fact_id = item.get("id", "")
            if not fact_id:
                continue

            if not self._skillgraph.has_node(fact_id):
                continue

            neighbors = self._skillgraph.neighbors(fact_id)
            degree = len(neighbors)
            boost = 1.0 + degree * 0.05
            item["rrf_score"] *= boost
            item["graph_boost"] = round(boost, 3)

        return sorted(ranked, key=lambda x: x["rrf_score"], reverse=True)