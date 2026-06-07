#!/usr/bin/env python3
"""Quick demo: Hybrid Retrieval in action.

Usage:
    python3 examples/hybrid_demo.py

Shows BM25 search with source-tier boosting.
"""

from nexus.retrieval import HybridRetriever

# Sample memories
memories = [
    {"id": "1", "text": "DeepSeek V4 Pro is disabled as fallback due to frequent 500/503 errors on Ollama Cloud."},
    {"id": "2", "text": "Fallback chain: Kimi K2.6 → Gemini Flash → GPT-5.5 (last resort)."},
    {"id": "3", "text": "Ollama Cloud Pro plan shows percentage, not hours. Session reset ~5h."},
    {"id": "4", "text": "Medium subscription expires November 2026. RSS feeds work without auth."},
    {"id": "5", "text": "Mac Mini M4 16GB — Kiosha and Miosha exclusive. Headless via NoMachine."},
]

# Build retriever (no Qdrant needed for this demo)
retriever = HybridRetriever()
retriever.index_from_texts(
    texts=[m["text"] for m in memories],
    ids=[m["id"] for m in memories],
)

# BM25 keyword search
query = "fallback provider"
results = retriever.search_bm25(query, top_k=3)

print(f"\n🔍 BM25 Search: '{query}'\n")
for r in results:
    print(f"  score={r['score']:.4f} | {r['text'][:80]}...")

# Full hybrid search (BM25 + optional vector, with RRF + tier boost)
fused = retriever.search_hybrid(query, top_k=3)

print(f"\n🔍 Hybrid Search: '{query}'\n")
for r in fused:
    methods = "+".join(r.get("methods", ["bm25"]))
    print(f"  {r['rrf_score']:.4f} | {methods:8s} | {r.get('tier', '—')} | {r['text'][:60]}...")

print(f"\n✅ Hybrid Retrieval working.\n")