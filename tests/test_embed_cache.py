"""Tests for EmbedCache (roadmap 3.1 L0-tier)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = str(Path(__file__).resolve().parent.parent / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from nexus_memory.embed_cache import EmbedCache


def test_hit_after_put():
    c = EmbedCache()
    c.put("q", [0.1, 0.2])
    v = c.get("q")
    assert v == [0.1, 0.2]
    assert c.hit_rate == 1.0


def test_miss_returns_none():
    c = EmbedCache()
    assert c.get("nix") is None
    assert c.stats()["misses"] == 1


def test_lru_eviction():
    c = EmbedCache(maxsize=2)
    c.put("a", [1.0])
    c.put("b", [2.0])
    c.put("c", [3.0])  # evict "a"
    assert c.get("a") is None
    assert c.get("b") == [2.0]
    assert c.stats()["entries"] == 2


def test_get_promotes_lru():
    c = EmbedCache(maxsize=2)
    c.put("a", [1.0])
    c.put("b", [2.0])
    c.get("a")  # touch a
    c.put("c", [3.0])  # evict b (LRU), not a
    assert c.get("a") is not None
    assert c.get("b") is None


def test_thread_safety():
    import threading

    c = EmbedCache(maxsize=64)
    errors = []

    def worker(i):
        try:
            for j in range(50):
                t = f"q{i}_{j}"
                c.put(t, [float(j)])
                c.get(t)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert c.stats()["entries"] <= 64