#!/usr/bin/env python3
"""Roadmap 3.3: p95 retrieval-latency benchmark against the live collection.

Measures the full plugin recall path (embed -> qdrant -> lifecycle filter ->
optional rerank -> graph boost) over 30 queries. Output: p50/p95/p99 + mean.
"""
import importlib.util
import math
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

spec = importlib.util.spec_from_file_location(
    "nhp", str(_repo / "plugins" / "memory" / "nexus" / "__init__.py"))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

_qdrant_host = os.environ.get("NEXUS_QDRANT_HOST", "localhost")
_qdrant_port = int(os.environ.get("NEXUS_QDRANT_PORT", "6333"))
prov = m.NexusMemoryProvider()  # Nr 276: real __init__ instead of a layout-coupled __new__
prov._collection = os.environ.get("NEXUS_COLLECTION", "nexus")  # Nr 275: same env resolution as the plugin
prov._rerank_cfg = {"enabled": True, "reranker": "auto", "pool_k": 20}
prov._embedder = m._Embedder()
from qdrant_client import QdrantClient
prov._qdrant = QdrantClient(host=_qdrant_host, port=_qdrant_port)

# Benchmark must not contaminate SICA trust counters / flywheel state: the
# recall path bumps agent stats and spawns the flywheel thread. Neutralize
# both on this instance so a benchmark run leaves no trace in live state.
prov._bump_agent_stats = lambda *a, **k: None
prov._flywheel_bump = lambda *a, **k: None

queries = [
    "wallbox ocpp", "tailscale routing fix", "gateway restart", "voyage embedding",
    "paperless backup", "odessa kasse", "serbien bankkonto", "design refero",
    "cron job audit", "expense tracking", "hund spaziergange", "mac mini ram",
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
p95 = lat[min(len(lat) - 1, max(0, math.ceil(0.95 * len(lat)) - 1))]
p99 = lat[-1]
print(f"\nn={len(lat)}  p50={p50:.1f}ms  p95={p95:.1f}ms  p99={p99:.1f}ms")
print("target p95 < 100ms:", "MET" if p95 < 100 else "NOT MET")