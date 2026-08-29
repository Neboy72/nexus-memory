#!/usr/bin/env python3
"""Roadmap 3.3: p95 retrieval-latency benchmark against the live collection.

Measures the full plugin recall path (embed -> qdrant -> lifecycle filter ->
optional rerank -> graph boost) over 30 queries. Output: p50/p95/p99 + mean.
"""
import importlib.util
import statistics
import sys
import threading as T
import time
import os
from pathlib import Path

# Load .env BEFORE importing the plugin (embeddings.py binds env at import time)
_repo = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_repo / "src"))
sys.path.insert(0, str(_repo))
for _env in [Path.home() / ".hermes" / ".env", _repo / ".env"]:
    if _env.exists():
        for line in _env.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if k and k not in os.environ:
                    os.environ[k] = v

spec = importlib.util.spec_from_file_location("nhp", "plugins/memory/nexus/__init__.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

prov = m.NexusMemoryProvider.__new__(m.NexusMemoryProvider)
prov._collection = "nexus"
prov._rerank_cfg = {"enabled": True, "reranker": "auto", "pool_k": 20}
prov._rerank_lock = T.Lock()
prov._skill_graph = None
prov._skill_graph_lock = T.Lock()
prov._prefetch_result = ""
prov._prefetch_lock = T.Lock()
prov._entity_extract_lock = T.Lock()
prov._hermes_home = ""
prov._write_stop = T.Event()
prov._embedder = m._Embedder()
from qdrant_client import QdrantClient

prov._qdrant = QdrantClient(host="localhost", port=6333)

queries = [
    "wallbox ocpp", "tailscale routing fix", "gateway restart", "voyage embedding",
    "paperless backup", "odessa kasse", "serbien bankkonto", "design refero",
    "cron job audit", "expense tracking", "bleki hund", "mac mini ram",
    "voice plan b desktop", "nexus memory roadmap", "kimi k3 designer",
]

# warmup (embedder + qdrant conn)
prov._recall("warmup query", limit=3)

lat = []
for q in queries:
    t0 = time.perf_counter()
    hits = prov._recall(q, limit=5)
    dt = (time.perf_counter() - t0) * 1000
    lat.append(dt)
    print(f"{dt:7.1f} ms | {len(hits)} hits | {q}")

lat.sort()
p50 = statistics.median(lat)
p95 = lat[int(len(lat) * 0.95) - 1]
p99 = lat[-1]
print(f"\nn={len(lat)}  p50={p50:.1f}ms  p95={p95:.1f}ms  p99={p99:.1f}ms")
print("target p95 < 100ms:", "MET" if p95 < 100 else "NOT MET")