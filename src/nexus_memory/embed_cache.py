"""Query embedding cache (roadmap 3.1 L0-tier, part 1).

Recall queries repeat heavily in practice (bursts in the same chat,
prefetch + recall on the same turn, repeated session starts). The cache
removes the ~256 ms Voyage roundtrip for repeated queries - a pure L0
win: same vectors, zero API cost, no behavior change.
"""

from __future__ import annotations

import hashlib
from collections import OrderedDict
from threading import Lock
from typing import List


class EmbedCache:
    """Thread-safe LRU cache for query vectors (demotion: oldest evicted)."""

    def __init__(self, maxsize: int = 256):
        if not isinstance(maxsize, int) or maxsize < 1:
            raise ValueError("maxsize must be a positive integer")
        self._maxsize = maxsize
        self._data: OrderedDict[str, List[float]] = OrderedDict()
        self._lock = Lock()
        self.hits = 0
        self.misses = 0

    @classmethod
    def _key(cls, text: str, is_query: bool = True) -> str:
        # Query and document embeddings of the same text differ (instruction-
        # aware models like qwen3-embedding prefix only queries), so the cache
        # key must include the mode.
        digest = hashlib.sha256(text.encode("utf-8", "ignore")).hexdigest()
        return digest + ("\x00q" if is_query else "\x00d")

    def get(self, text: str, is_query: bool = True) -> List[float] | None:
        key = self._key(text, is_query)
        with self._lock:
            vec = self._data.get(key)
            if vec is not None:
                self._data.move_to_end(key)
                self.hits += 1
            else:
                self.misses += 1
            return list(vec) if vec is not None else None

    def put(self, text: str, vector: List[float], is_query: bool = True) -> None:
        key = self._key(text, is_query)
        with self._lock:
            self._data[key] = list(vector)
            while len(self._data) > self._maxsize:
                self._data.popitem(last=False)

    @property
    def hit_rate(self) -> float:
        # One locked snapshot: reading hits/misses separately could interleave
        # with a concurrent get() and even yield a rate above 1.0.
        with self._lock:
            hits, misses = self.hits, self.misses
        total = hits + misses
        return hits / total if total else 0.0

    def stats(self) -> dict:
        with self._lock:
            entries = len(self._data)
            hits, misses = self.hits, self.misses
        total = hits + misses
        return {
            "entries": entries,
            "hits": hits,
            "misses": misses,
            "hit_rate": round(hits / total if total else 0.0, 3),
            "maxsize": self._maxsize,
        }
