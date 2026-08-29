"""Query embedding cache (roadmap 3.1 L0-tier, part 1).

Recall queries repeat heavily in practice (bursts in the same chat,
prefetch + recall on the same turn, repeated session starts). The cache
removes the ~256 ms Voyage roundtrip for repeated queries - a pure L0
win: same vectors, zero API cost, no behavior change.
"""

from __future__ import annotations

import hashlib
import threading
from collections import OrderedDict
from threading import Lock
from typing import List


class EmbedCache:
    """Thread-safe LRU cache for query vectors (demotion: oldest evicted)."""

    def __init__(self, maxsize: int = 256):
        self._maxsize = maxsize
        self._data: OrderedDict[str, List[float]] = OrderedDict()
        self._lock = Lock()
        self.hits = 0
        self.misses = 0

    @staticmethod
    def _key(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8", "ignore")).hexdigest()

    def get(self, text: str) -> List[float] | None:
        key = self._key(text)
        with self._lock:
            vec = self._data.get(key)
            if vec is not None:
                self._data.move_to_end(key)
                self.hits += 1
            else:
                self.misses += 1
            return list(vec) if vec is not None else None

    def put(self, text: str, vector: List[float]) -> None:
        key = self._key(text)
        with self._lock:
            self._data[key] = list(vector)
            while len(self._data) > self._maxsize:
                self._data.popitem(last=False)

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return self.hits / total if total else 0.0

    def stats(self) -> dict:
        with self._lock:
            entries = len(self._data)
        return {
            "entries": entries,
            "hits": self.hits,
            "misses": self.misses,
            "hit_rate": round(self.hit_rate, 3),
            "maxsize": self._maxsize,
        }
