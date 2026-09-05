#!/usr/bin/env python3
"""consolidation.py — in-process ingestion consolidation for Nexus Memory.

Moves the heavy work from RETRIEVAL time to INGESTION time (Kiosha benchmark
findings 2026-09-05, LongMemEval phase-1/2): raw turn dumps ("User: ...\n
Assistant: ...", category=session) get distilled into atomic, self-contained
facts with resolved references (pronouns -> names, relative times -> absolute
dates), and contradictions are resolved at write time via the established
supersede pattern (lifecycle_status='deprecated', NEVER delete).

Runs as an in-process daemon thread inside the MCP server
(HealthAuditor/TrustService pattern, harness-independent: no external cron,
works after plain `pip install` on any host).

Design rules (agreed with Nebo 2026-09-05):
  - Kill-switch: NEXUS_CONSOLIDATION=0 disables the daemon (default ON).
  - Every per-memory exception is logged and skipped; the daemon never dies.
  - No deletes, EVER — superseded facts stay in Qdrant with lifecycle fields
    (audit trail preserved, recall skips them via existing filters).
  - LLM access is optional: if Ollama is unreachable the per-point work is
    skipped and retried on the next tick (fail-safe, no crash loop).
"""
import json
import logging
import os
import threading
import time
import uuid
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple

log = logging.getLogger("nexus.consolidation")

CONSOLIDATION_INTERVAL_SECONDS = int(os.environ.get("NEXUS_CONSOLIDATION_INTERVAL", 3600))
CONSOLIDATION_START_DELAY_SECONDS = int(os.environ.get("NEXUS_CONSOLIDATION_START_DELAY", 120))
CONSOLIDATION_BATCH = int(os.environ.get("NEXUS_CONSOLIDATION_BATCH", 10))
CONSOLIDATION_ENABLED = os.environ.get("NEXUS_CONSOLIDATION", "1") == "1"
OLLAMA_BASE = os.environ.get("NEXUS_OLLAMA_BASE", "http://127.0.0.1:11434")
OLLAMA_MODEL = os.environ.get("NEXUS_CONSOLIDATION_MODEL", "glm-5.3-flash:cloud")
_CONFLICT_SIM_THRESHOLD = float(os.environ.get("NEXUS_CONSOLIDATION_SIM", "0.75"))
_CONSOLIDATED_BY = "consolidation-v1"
_MAX_CONV_CHARS = 8000

DISTILL_PROMPT = """Extract atomic, self-contained facts from this AI agent conversation turn. Return JSON only.

Rules:
- 0-10 facts. Each fact must be understandable WITHOUT the conversation.
- Resolve pronouns to names/context and relative times ("yesterday", "next week") to the absolute date given below.
- Skip greetings, task progress, tool output, chit-chat.
- Each fact under 250 chars. Language: same as the conversation.
- Only facts worth remembering weeks from now.

Today's date (resolve relative times against this): {date}

Conversation:
{conv}

Return ONLY: {{"facts": ["fact 1", "fact 2", ...]}}
If nothing durable: return {{"facts": []}}"""

CLASSIFY_PROMPT = """Two memory records about the same topic. Classify their relationship. Return JSON only.

Existing memory: "{old}"
New fact:        "{new}"

Return ONLY one of:
{{"verdict": "duplicate"}}      — same information, no conflict
{{"verdict": "supersede"}}     — new fact contradicts/updates the old one
{{"verdict": "unrelated"}}     — similar wording but about different things
"""


def _ollama_generate(prompt: str, timeout: int = 120) -> str:
    """POST /api/generate, stream=False. Raises on failure (caller catches)."""
    body = json.dumps({"model": OLLAMA_MODEL, "prompt": prompt, "stream": False,
                       "options": {"temperature": 0.1}}).encode()
    req = urllib.request.Request(OLLAMA_BASE + "/api/generate", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read()).get("response", "")


def _extract_payload(txt: str) -> str:
    """Strip GLM reasoning wrappers: prefer the LAST </think> block, else the
    FIRST {..} JSON object found anywhere in the text."""
    if "</think>" in txt:
        parts = txt.split("</think>")
        candidate = parts[-1].strip()
        if not candidate and len(parts) >= 2:
            candidate = parts[1].strip()
        if candidate:
            return candidate
    i = txt.find("{")
    if i >= 0 and "facts" in txt[i:i+200]:
        return txt[i:]
    return txt


def _parse_facts(raw: str) -> List[str]:
    """Tolerant JSON parsing of {"facts": [...]}; regex fallback for broken JSON."""
    txt = _extract_payload((raw or "").strip())
    try:
        d = json.loads(txt)
        if isinstance(d, dict) and isinstance(d.get("facts"), list):
            return [str(x).strip()[:300] for x in d["facts"]
                    if len(str(x).strip()) >= 4]
    except Exception:
        pass
    import re
    m = re.search(r'"facts"\s*:\s*\[(.*)', txt, re.DOTALL)
    if m:
        items = re.findall(r'"((?:[^"\\]|\\.)+)"', m.group(1))
        return [it.replace('\\"', '"').strip()[:300] for it in items
                if len(it.strip()) >= 4]
    return []


def _parse_verdict(raw: str) -> str:
    """Parse the classifier verdict; keyword fallback for GLM prose leaks."""
    txt = _extract_payload((raw or "").strip())
    try:
        d = json.loads(txt)
        v = str(d.get("verdict", "")).lower()
        if v in ("duplicate", "supersede", "unrelated"):
            return v
    except Exception:
        pass
    import re
    m = re.search(r'"verdict"\s*:\s*"(\w+)"', txt)
    if m and m.group(1).lower() in ("duplicate", "supersede", "unrelated"):
        return m.group(1).lower()
    low = txt.lower()
    if "supersede" in low:
        return "supersede"
    if "duplicate" in low:
        return "duplicate"
    return "unrelated"


def _embed_via_store(store, text: str) -> list:
    """Sync embedding via the store's EmbeddingProvider (daemon thread has no loop)."""
    import asyncio
    coro = store._embedder.embed(text)
    return asyncio.run(coro)


class Consolidator:
    """Distills raw session dumps into atomic facts + resolves conflicts.

    Injectable seams for tests: llm_fn (prompt -> raw LLM string) and
    embed_fn (text -> vector). Defaults hit Ollama / the store embedder.
    """

    def __init__(self, store, collection: str, llm_fn: Optional[Callable[[str], str]] = None,
                 embed_fn: Optional[Callable[[str], list]] = None) -> None:
        self._store = store
        self._collection = collection
        self._llm_fn = llm_fn
        self._embed_fn = embed_fn
        self._lock = threading.Lock()
        self._last_report: Dict[str, Any] = {}

    # ── flags for the health tool ────────────────────────────────────
    def get_flags(self) -> Dict[str, Any]:
        with self._lock:
            r = dict(self._last_report)
        if not r:
            return {}
        return {"consolidation": {
            "last_run": r.get("timestamp"),
            "raw_scanned": r.get("scanned", 0),
            "facts_created": r.get("facts_created", 0),
            "superseded": r.get("superseded", 0),
            "duplicates_dropped": r.get("duplicates", 0),
        }}

    def _llm(self, prompt: str) -> str:
        return self._llm_fn(prompt) if self._llm_fn else _ollama_generate(prompt)

    def _embed(self, text: str) -> list:
        if self._embed_fn:
            return self._embed_fn(text)
        return _embed_via_store(self._store, text)

    # ── one pass (used by loop AND tests) ────────────────────────────
    def run(self, batch_size: int = 0, dry_run: bool = False) -> Dict[str, Any]:
        batch_size = batch_size or CONSOLIDATION_BATCH
        scanned = facts_created = superseded = duplicates = failed = 0
        raw_points = self._next_raw_batch(batch_size)
        date = time.strftime("%Y-%m-%d")
        for p in raw_points:
            scanned += 1
            try:
                text = (p.payload or {}).get("content", "")
                conv = text[:_MAX_CONV_CHARS]
                facts = _parse_facts(self._llm(DISTILL_PROMPT.format(date=date, conv=conv)))
                created_here = 0
                for fact in facts:
                    decision, supersede_ids = self._resolve_conflicts(fact)
                    if decision == "duplicate":
                        duplicates += 1
                        continue
                    if not dry_run:
                        new_id = self._store_fact(fact, p.id)
                        for old_id in supersede_ids:
                            self._supersede_old(old_id, new_id)
                        superseded += len(supersede_ids)
                    created_here += 1
                    facts_created += 1
                if not dry_run:
                    self._mark_consolidated(p.id, created_here)
            except Exception as exc:
                failed += 1
                log.warning("consolidation: point %s failed (skipped): %s", p.id, exc)
        report = {"timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                  "scanned": scanned, "facts_created": facts_created,
                  "superseded": superseded, "duplicates": duplicates,
                  "failed": failed}
        with self._lock:
            self._last_report = report
        return report

    # ── raw batch selection: not-yet-consolidated canonical sessions ──
    def _next_raw_batch(self, limit: int) -> list:
        from qdrant_client.models import (Filter, FieldCondition, MatchValue,
                                          IsEmptyCondition, PayloadField)
        points, offset = [], None
        while len(points) < limit:
            batch, offset = self._store.client.scroll(
                self._collection,
                scroll_filter=Filter(must=[
                    FieldCondition(key="category", match=MatchValue(value="session")),
                    FieldCondition(key="lifecycle_status", match=MatchValue(value="canonical")),
                    IsEmptyCondition(is_empty=PayloadField(key="consolidated_by")),
                ]),
                limit=min(limit - len(points), 64) or 1, offset=offset,
                with_payload=True, with_vectors=False,
            )
            if not batch:
                break
            points.extend(batch)
            if offset is None:
                break
        return points[:limit]

    # ── conflict resolution BEFORE the fact lands ────────────────────
    def _resolve_conflicts(self, fact: str):
        """Returns (decision, to_supersede_ids).

        decision: 'ok' (store it) or 'duplicate' (drop it).
        Contradictions: old canonical fact ids are returned; caller supersedes
        them AFTER the new fact got its id (superseded_by reference). Never deletes.
        """
        from qdrant_client.models import Filter, FieldCondition, MatchValue
        vec = self._embed(fact)
        try:
            res = self._store.client.query_points(
                self._collection, query=vec, limit=3,
                score_threshold=_CONFLICT_SIM_THRESHOLD,
                query_filter=Filter(must=[
                    FieldCondition(key="category", match=MatchValue(value="fact")),
                    FieldCondition(key="lifecycle_status", match=MatchValue(value="canonical")),
                ]),
            )
        except Exception as exc:
            log.warning("consolidation: similarity check failed (conservative store): %s", exc)
            return "ok", []
        to_supersede: list = []
        for point in res.points:
            score = float(getattr(point, "score", 0.0) or 0.0)
            old_payload = point.payload or {}
            if score < _CONFLICT_SIM_THRESHOLD:
                continue
            if str(old_payload.get("category", "fact")) != "fact":
                continue
            if old_payload.get("lifecycle_status") not in ("canonical", None, ""):
                continue
            try:
                verdict = _parse_verdict(self._llm(CLASSIFY_PROMPT.format(
                    old=str(old_payload.get("content", ""))[:500], new=fact[:500])))
            except Exception as exc:
                log.warning("consolidation: classify failed (conservative store): %s", exc)
                verdict = "unrelated"
            if verdict == "duplicate":
                return "duplicate", []
            if verdict == "supersede":
                to_supersede.append(str(point.id))
        return "ok", to_supersede

    def _supersede_old(self, old_id: str, new_id: str) -> None:
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self._store.client.set_payload(
            self._collection,
            payload={"lifecycle_status": "deprecated",
                     "superseded_by": new_id,
                     "superseded_at": now,
                     "valid_to": now,
                     "supersede_reason": "consolidation: contradicted by distilled fact"},
            points=[old_id],
        )

    # ── write the distilled fact ─────────────────────────────────────
    def _store_fact(self, fact: str, source_point_id: str) -> str:
        import uuid
        from qdrant_client.models import PointStruct
        vec = self._embed(fact)
        new_id = str(uuid.uuid4())
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self._store.client.upsert(
            self._collection,
            points=[PointStruct(
                id=new_id, vector=vec,
                payload={"content": fact, "category": "fact", "access_level": "public",
                         "source": "nexus-consolidation", "created_at": now,
                         "lifecycle_status": "canonical", "confidence": 0.8,
                         "consolidated_from": source_point_id,
                         "provenance": {"source_type": "consolidation",
                                        "created_by": "consolidation-v1",
                                        "timestamp": now}},
            )],
        )
        return new_id

    def _mark_consolidated(self, point_id: str, facts_created: int) -> None:
        self._store.client.set_payload(
            self._collection,
            payload={"consolidated_by": _CONSOLIDATED_BY,
                     "consolidated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                     "consolidated_facts": facts_created},
            points=[point_id],
        )

    # ── daemon wiring (TrustService pattern) ─────────────────────────
    def start(self) -> None:
        if not CONSOLIDATION_ENABLED:
            log.info("consolidation disabled via NEXUS_CONSOLIDATION=0")
            return

        def _loop():
            time.sleep(CONSOLIDATION_START_DELAY_SECONDS)
            while True:
                try:
                    self.run()
                except Exception as exc:
                    log.warning("Consolidation pass failed: %s", exc)
                slept = 0
                while slept < CONSOLIDATION_INTERVAL_SECONDS:
                    time.sleep(min(60, CONSOLIDATION_INTERVAL_SECONDS - slept))
                    slept += 60

        t = threading.Thread(target=_loop, name="nexus-consolidation", daemon=True)
        t.start()
        log.info("Consolidation daemon started (interval %ds, batch %d)",
                 CONSOLIDATION_INTERVAL_SECONDS, CONSOLIDATION_BATCH)


def start_daemon(store, collection: str) -> Optional[Consolidator]:
    """Wire the consolidation daemon into a running MemoryStore.

    Called from mcp_server _ensure_daemons; respects the kill-switch.
    Never raises — a broken consolidation must not block server boot.
    """
    try:
        c = Consolidator(store, collection)
        c.start()
        return c
    except Exception as exc:
        log.warning("Consolidation daemon unavailable: %s", exc)
        return None