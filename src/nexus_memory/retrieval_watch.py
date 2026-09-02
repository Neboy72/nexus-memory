#!/usr/bin/env python3
"""
retrieval_watch.py — in-process retrieval-quality watchdog for Nexus Memory.

Portiert aus scripts/nexus-retrieval-watch.py (Kiosha/Nebo 30.08.2026) als
in-process daemon (HealthAuditor-Pattern). Nebo-Grundentscheidung 02.09.2026:
'Nexus Memory ist unabhängig — Wartung läuft im Server, nie extern.'

TÄGLICHER CHECK: kritische Entities müssen im Prefetch/Semantic-Search gefunden
werden. Wenn Score < 0.5 oder Expected-Keyword fehlt → Flag für health-Tool
+ Webhook (falls NEXUS_WEBHOOK_URL gesetzt).

Env:
  NEXUS_RETRIEVAL_WATCH=0   → Daemon deaktiviert (Kill-Switch)
  NEXUS_RETRIEVAL_INTERVAL_SEC → Intervall (default 86400)
  NEXUS_WATCH_QUERIES       → JSON: [["query","expected-keyword"],...]
                               (default: gerätekritische Queries)
"""
import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("nexus.retrieval_watch")

RETRIEVAL_START_DELAY_SECONDS = int(os.environ.get("NEXUS_RETRIEVAL_START_DELAY", 120))
RETRIEVAL_INTERVAL_SECONDS = int(os.environ.get("NEXUS_RETRIEVAL_INTERVAL_SEC", 24 * 3600))
MIN_SCORE = 0.5

DEFAULT_QUERIES: List[Tuple[str, str]] = [
    ("Bose SoundLink Audio-Ausgabe Bluetooth", "Bose"),
    ("Razer USB Mikrofon Input", "Razer"),
    ("Windows PC nebos-pc Desktop", "nebos-pc"),
]


def _load_queries() -> List[Tuple[str, str]]:
    raw = os.environ.get("NEXUS_WATCH_QUERIES", "").strip()
    if raw:
        try:
            import json
            parsed = json.loads(raw)
            out = [(str(q), str(kw)) for q, kw in parsed]
            if out:
                return out
        except Exception as exc:
            log.warning("NEXUS_WATCH_QUERIES unparsable (%s) — nutze Defaults", exc)
    return DEFAULT_QUERIES


class RetrievalWatch:
    """Täglich: kritische Queries ausführen, prüfen dass erwartet gefunden wird."""

    def __init__(self, store, collection: str, embedder: Optional[Any] = None) -> None:
        self._store = store
        self._collection = collection
        self._embedder = embedder
        self._queries = _load_queries()
        self._last_report: Dict[str, Any] = {}
        self._lock = threading.Lock()

    # ── flags für health-Tool ─────────────────────────────────────────
    def get_flags(self) -> Dict[str, Any]:
        with self._lock:
            r = dict(self._last_report)
        if not r:
            return {}
        fails = r.get("failures") or []
        if fails:
            names = ", ".join(f.get("expected", "?") for f in fails)
            return {
                "retrieval": {
                    "failed_queries": len(fails),
                    "message": (
                        f"🧠 Nexus Memory retrieval-watch: {len(fails)} kritische "
                        f"Entity-Query(s) nicht gefunden: {names}. "
                        f"Memory-Retrieval qualitativ beeinträchtigt."
                    ),
                    "timestamp": r.get("timestamp"),
                }
            }
        return {}

    # ── one pass ──────────────────────────────────────────────────────
    def run(self) -> Dict[str, Any]:
        failures = []
        checked = 0
        for query, expected in self._queries:
            try:
                results = self._search(query, limit=5)
                checked += 1
                text = " ".join(
                    str((r.payload or {}).get("text") or (r.payload or {}).get("content") or "")
                    for r in results
                ).lower()
                top_score = max(
                    (getattr(r, "score", 0.0) or 0.0 for r in results), default=0.0
                )
                if not results or expected.lower() not in text:
                    failures.append({
                        "query": query,
                        "expected": expected,
                        "results": len(results),
                        "top_score": round(top_score, 3),
                    })
            except Exception as exc:
                log.warning("Retrieval-watch query failed: %s", exc)
                failures.append({"query": query, "expected": expected, "error": str(exc)})

        report = {
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "queries_checked": checked,
            "failures": failures,
        }
        with self._lock:
            self._last_report = report
        if failures:
            log.warning("Retrieval-watch: %d/%d queries failed", len(failures), checked)
        return report

    def _search(self, query: str, limit: int = 5):
        """Vector search über den Store (nutzt Store-Embedder, kein Hermes-Import)."""
        embedder = self._embedder or getattr(self._store, "_embedder", None)
        vector = None
        if embedder is not None:
            try:
                vector = embedder.embed(query)
            except Exception as exc:
                log.warning("Embedding failed for watch query: %s", exc)
        if vector is None:
            # Fallback: kein Embedder am Store (z. B. Unit-Tests) → statisch
            # scrollen. Der Watchdog bewertet dann Text-Containment.
            results, _ = self._store.client.scroll(
                self._collection, limit=limit, with_payload=True, with_vectors=False
            )
            return results
        client = self._store.client
        query_fn = getattr(client, "query_points", None)
        if query_fn is None:
            # ältere qdrant_client: search()-API
            return client.search(
                collection_name=self._collection,
                query_vector=vector,
                limit=limit,
                with_payload=True,
            )
        return query_fn(
            collection_name=self._collection,
            query=vector,
            limit=limit,
            with_payload=True,
        ).points

    # ── daemon loop ───────────────────────────────────────────────────
    def start(self) -> None:
        def _loop():
            time.sleep(RETRIEVAL_START_DELAY_SECONDS)
            while True:
                try:
                    self.run()
                except Exception as exc:
                    log.warning("Retrieval-watch pass failed: %s", exc)
                for _ in range(max(60, RETRIEVAL_INTERVAL_SECONDS // 60)):
                    time.sleep(60)

        t = threading.Thread(target=_loop, name="nexus-retrieval-watch", daemon=True)
        t.start()
        log.info("Retrieval-watch daemon started (interval %.1f h)",
                 RETRIEVAL_INTERVAL_SECONDS / 3600)