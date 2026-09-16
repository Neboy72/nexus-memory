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

# Get Voyage embedding for vector search
vec = None
voyage_key = None
try:
    with open(os.path.expanduser('~/.hermes/config.yaml')) as f:
        for line in f:
            if 'voyage_api_key:' in line:
                voyage_key = line.split(':', 1)[1].strip().strip("'\"")
                break
except (OSError, UnicodeDecodeError) as e:
    # Narrowed from a bare except: a typo or permission problem must not
    # silently degrade the run to BM25-only without a hint.
    print(f"⚠️ config read failed ({e}) — continuing without vector search")

if voyage_key:
    import requests
    print("🧠 Embedding...", end=' ', flush=True)
    try:
        resp = requests.post(
            'https://api.voyageai.com/v1/embeddings',
            headers={'Authorization': f'Bearer {voyage_key}'},
            json={'input': query, 'model': 'voyage-4'},
            timeout=15
        )
        if 200 <= resp.status_code < 300:
            vec = resp.json()['data'][0]['embedding']
            print("1024d")
        else:
            print(f"API {resp.status_code}")
    except Exception as e:
        print(f"fail: {e}")

# Hybrid search with reranker
if vec:
    results = r.search_hybrid(query, query_vector=vec, top_k=5, rerank=True, voyage_api_key=voyage_key)
else:
    results = r.search_bm25(query, top_k=5)

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
