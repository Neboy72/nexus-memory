#!/usr/bin/env python3
"""Real-Bench: Query-Rewriting gegen den Live-Store (nexus-Collection).

12 echte Suchanfragen mit Ground-Truth (bekannte Memory-IDs), je mit und
ohne Rewrite. Metrik: Recall@5 (ist die richtige Memory in den Top-5?).
Das Voyage-Embedding bleibt identisch; nur der Suchtext aendert sich.
"""
import os
import sys
import time

sys.path.insert(0, '/Users/miosha/nexus-memory-test/src')
sys.path.insert(0, '/Users/miosha/nexus-memory-test')

# .env laden (Voyage-Key) — gleicher Mechanismus wie bench_latency.py
from pathlib import Path
for env in [Path.home() / '.hermes' / '.env', Path('/Users/miosha/nexus-memory-test/.env')]:
    if env.exists():
        for line in env.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, _, v = line.partition('=')
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if k and k not in os.environ:
                    os.environ[k] = v

from qdrant_client import QdrantClient

# ── 12 Anfragen mit Ground-Truth (Substring im Text eines bekannnten Punkts) ──
# Formuliert wie NEBO formuliert (salopp, Pronomen), GT = Suchbegriff der im Memory stehen muss.
QUERIES = [
    ("wies das thema mit dem hund", "spaziergange"),
    ("das ding fur das auto ladens", "wallbox"),
    ("wann kommt der muell weg", "muell"),
    ("was war nochmal mit dem backup kram", "backup"),
    ("der steuer-kram vom auto", "fahrzeug"),
    ("meine ausgaben sache", "expenses"),
    ("was lief beim paperless", "paperless"),
    ("die voice sache von neulich", "voice"),
    ("telegram krams einrichten", "telegram"),
    ("wie war das mit dem tailscale", "tailscale"),
    ("der kalender-kram mit ina", "kalender"),
    ("was stand wegen dem qdrant ding", "qdrant"),
]


def embed(text):
    import importlib.util
    import threading
    spec = importlib.util.spec_from_file_location(
        'nhp', '/Users/miosha/nexus-memory-test/plugins/memory/nexus/__init__.py')
    global _PLUGIN_MOD
    try:
        _PLUGIN_MOD
    except NameError:
        _PLUGIN_MOD = None
    if _PLUGIN_MOD is None:
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        globals()['_PLUGIN_MOD'] = m
    return _PLUGIN_MOD._Embedder().embed(text)


def top5(vector, collection='nexus'):
    c = QdrantClient(host='localhost', port=6333)
    res = c.query_points(collection_name=collection, query=vector, limit=5)
    return [p.payload.get('content', '') for p in res.points]


def recall_at5(query, rewritten_text):
    vec = embed(rewritten_text)
    hits = top5(vec)
    joined = ' '.join(hits).lower()
    return joined


def evaluate():
    results = []
    for q, gt in QUERIES:
        row = {'q': q, 'gt': gt}
        # OFF: original
        joined = recall_at5(q, q)
        row['off'] = gt in joined
        # ON: rewritten (echter fuel-chain call)
        os.environ['NEXUS_REWRITE'] = '1'
        from nexus_memory.query_rewrite import rewrite_query
        from nexus_memory.fuel_chain import get_fuel
        from nexus_memory.consolidation import OLLAMA_BASE, OLLAMA_MODEL, _ollama_generate
        fn = get_fuel(OLLAMA_BASE, OLLAMA_MODEL, _ollama_generate)
        rw = rewrite_query(q, fn)
        row['rw'] = rw
        joined = recall_at5(q, rw)
        row['on'] = gt in joined
        results.append(row)
        print(f"{q!r:45s} -> rw={rw!r:45s} off={row['off']!s:5s} on={row['on']}")
    off_rate = sum(1 for r in results if r['off']) / len(results) * 100
    on_rate = sum(1 for r in results if r['on']) / len(results) * 100
    print()
    print(f"Recall@5 ohne Rewrite: {off_rate:.0f}%  |  mit Rewrite: {on_rate:.0f}%")
    return results


if __name__ == '__main__':
    os.environ.pop('NEXUS_REWRITE', None)
    evaluate()