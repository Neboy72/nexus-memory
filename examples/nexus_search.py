#!/usr/bin/env python3
"""Nexus Memory Search — BM25 + Vector Hybrid. Blazing fast, cached."""

import sys, json, os, re
os.environ['TQDM_DISABLE'] = '1'
from nexus.retrieval import HybridRetriever

query = ' '.join(sys.argv[1:]) if len(sys.argv) > 1 else ''
if not query:
    query = input('Search: ')
    if not query:
        print("No query provided.")
        sys.exit(0)

r = HybridRetriever(qdrant_host='127.0.0.1', qdrant_port=6333, collection_name=None)

# Load or build BM25 index
if r._bm25 is None:
    print("⌛ Indexing BM25...", end=' ', flush=True)
    stats = r.index_memories()
    print(f"{stats['indexed']} points indexed")
else:
    print(f"✅ Loaded {len(r._ids)} points from cache")

# Get Voyage embedding for vector search
vec = None
voyage_key = None
try:
    with open(os.path.expanduser('~/.hermes/config.yaml')) as f:
        for line in f:
            if 'voyage_api_key:' in line:
                voyage_key = line.split(':', 1)[1].strip().strip("'\"")
                break
except:
    pass

if voyage_key:
    import requests
    print("🧠 Embedding...", end=' ', flush=True)
    try:
        resp = requests.post(
            'https://api.voyageai.com/v1/embeddings',
            headers={'Authorization': f'Bearer {voyage_key}'},
            json={'input': query, 'model': 'voyage-3-large'},
            timeout=15
        )
        if resp.status_code == 200:
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
    # Determine method badges
    methods = hit.get('methods', ['bm25'] if 'rrf_score' not in hit else ['?'])
    method_badge = '+'.join(m.upper()[:4] for m in methods)

    # Score
    score = hit.get('rerank_score', hit.get('rrf_score', hit.get('score', 0)))

    # Tier
    tier = hit.get('tier', '?')

    # Method badges
    method_badge = '+'.join(m.upper()[:4] for m in methods)

    # Get clean text - strip JSON-like prefix/suffix
    text = hit.get('text', '')
    # Remove common Qdrant payload formatting like {'content': '...', 'category': '...', ...}
    text = re.sub(r"^\{'content':\s*'", '', text)
    text = re.sub(r"',\s*'[^']+':\s*'[^']*'(,\s*'[^']+':\s*'[^']*')*\}", '', text)
    text = re.sub(r"'\}$", '', text)
    text = re.sub(r"^'", '', text)
    text = re.sub(r"'$", '', text)
    # Unescape
    text = text.replace("\\n", "\n").replace("\\'", "'").replace('\\"', '"')
    # Remove leading/trailing whitespace per line
    lines_clean = [l.strip() for l in text.split('\n')]
    text = '\n'.join(lines_clean)
    # Truncate
    if len(text) > 300:
        text = text[:300] + '...'

    print(f"  {i}. [{method_badge}] [T{tier[-1:]}] ({score:.2f})")
    for line in text.strip().split('\n')[:4]:
        print(f"     {line.strip()}")
    print()

print(f"── {len(results)} results ──")
