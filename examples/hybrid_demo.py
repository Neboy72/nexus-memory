#!/usr/bin/env python3
"""Quick demo: Hybrid Retrieval in action.

Usage:
    python3 examples/hybrid_demo.py

Shows BM25 search with source-tier boosting.
"""

from nexus.retrieval import HybridRetriever


def main() -> None:
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
        print(f"  score={r.get('score', 0.0):.4f} | {str(r.get('text', ''))[:80]}...")

    # Full hybrid search (BM25 + optional vector, with RRF + tier boost)
    fused = retriever.search_hybrid(query, top_k=3)

    print(f"\n🔍 Hybrid Search: '{query}'\n")
    for r in fused:
        methods = "+".join(r.get("methods", ["bm25"]))
        tier = r.get("tier", "—")
        print(f"  {r.get('rrf_score', 0.0):.4f} | {methods:8s} | {tier} | {str(r.get('text', ''))[:60]}...")

    # W40-3: an empty result set means the demo did NOT work (e.g. no BM25
    # index was built) — the banner must not claim success over empty output.
    if results or fused:
        print("\n✅ Hybrid Retrieval working.\n")
    else:
        print("\n⚠️ No matches — BM25 index may not have been built.\n")


if __name__ == "__main__":
    main()
