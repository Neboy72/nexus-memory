#!/usr/bin/env python3
"""Nexus Memory Search — BM25 + Vector Hybrid. Blazing fast, cached."""

import sys, os
os.environ['TQDM_DISABLE'] = '1'
from nexus.retrieval import HybridRetriever

query = ' '.join(sys.argv[1:]) if len(sys.argv) > 1 else ''
if not query:
    query = input('Search: ')
    if not query:
        print("No query provided.")
        sys.exit(0)

r = HybridRetriever(qdrant_host='127.0.0.1', qdrant_port=6333, collection_name=None)

# Load or build BM25 index. The retriever's internal attributes are guarded
# with getattr so a representation change cannot break this example silently.
if getattr(r, '_bm25', None) is None:
    print("⌛ Indexing BM25...", end=' ', flush=True)
    stats = r.index_memories()
    print(f"{stats['indexed']} points indexed")
else:
    print(f"✅ Loaded {len(getattr(r, '_ids', None) or [])} points from cache")

# Use sentence-transformers for a local vector search example.
try:
    from sentence_transformers import SentenceTransformer
    model = SentenceTransformer("all-MiniLM-L6-v2")
    vec = model.encode(query).tolist()
except Exception as exc:
    print("Local embedding unavailable:", exc)
    vec = None

results = r.search_hybrid(query, query_vector=vec, top_k=5, rerank=True)

if not results:
    print("\nNo results found.")
    sys.exit(0)

# ── Clean display ──────────────────────────────────────────────────────
print()
for i, hit in enumerate(results, 1):
    # Determine method badges (computed once)
    methods = hit.get('methods', ['bm25'] if 'rrf_score' not in hit else ['?'])
    method_badge = '+'.join(m.upper()[:4] for m in methods)

    # Score: dict.get only defaults when the key is ABSENT — a present-but-None
    # rerank_score/rrf_score/score would make the format below raise. Pick the
    # first numeric value and coerce.
    score = next((hit[k] for k in ('rerank_score', 'rrf_score', 'score')
                  if isinstance(hit.get(k), (int, float))), 0.0)
    score = float(score)

    # Tier is free-form in the payload — normalize so tier[-1:] cannot raise.
    tier = str(hit.get('tier') or '?')

    # The retriever already returns clean text. The old regex chain assumed the
    # value was a str(dict) repr and silently corrupted legitimate content that
    # contained quotes or "'key': 'value'" sequences — use it as-is.
    text = hit.get('text')
    text = text if isinstance(text, str) else ('' if text is None else str(text))
    if len(text) > 300:
        text = text[:300] + '...'

    print(f"  {i}. [{method_badge}] [T{tier[-1:]}] ({score:.2f})")
    for line in text.strip().split('\n')[:4]:
        print(f"     {line.strip()}")
    print()

print(f"── {len(results)} results ──")
