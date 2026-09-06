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
import re
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
# Consolidation resolves RELATIVE time expressions ("yesterday", "next
# week") against the date the SOURCE SESSION was created — not the date
# of the consolidation run. A session dumped 2026-08-10 saying "I
# switched to the Mac Mini yesterday" means 2026-08-09; resolving
# against the run date (2026-09-06) would bake a wrong date into a
# permanent fact. Derived from the source point's created_at
# (fallback: today when missing/unparseable).
_CONSOLIDATED_BY = "consolidation-v1"
_MAX_CONV_CHARS = 8000


def _source_date(payload: Any) -> str:
    """Date of the source point (YYYY-MM-DD) for relative-time resolution.

    Reads created_at from the source payload (ISO-ish or unix seconds/
    milliseconds); falls back to the current date when missing, empty
    or unparseable — never raises, never returns an empty string.
    """
    if not isinstance(payload, dict):
        return time.strftime("%Y-%m-%d")
    raw = payload.get("created_at")
    if isinstance(raw, (int, float)) and raw > 0:
        try:
            return time.strftime("%Y-%m-%d", time.gmtime(float(raw)))
        except Exception:
            pass
    txt = str(raw or "").strip()
    if txt:
        m = re.match(r"(\d{4})-(\d{2})-(\d{2})", txt)
        if m:
            try:
                return time.strftime(
                    "%Y-%m-%d", time.struct_time(
                        (int(m.group(1)), int(m.group(2)), int(m.group(3)),
                         0, 0, 0, 0, 1, 0)))
            except Exception:
                pass
    return time.strftime("%Y-%m-%d")


class _SkipPointError(Exception):
    """Raised when a point must be excluded from consolidation entirely."""

# Valid access levels + which candidate levels a fact may conflict with.
# Conflict resolution must not cross visibility domains: a public fact
# must never suppress/supersede a private memory (and vice versa).
_ACCESS_LEVELS = ("public", "trusted", "private")
_ACCESS_CONFLICT_SCOPE = {
    "public": frozenset({"public"}),
    "trusted": frozenset({"trusted", "public"}),
    "private": frozenset({"private"}),
}

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


def _normalize_access_level(payload: Any) -> str:
    """Valid explicit access_level wins; anything missing/unknown degrades
    to 'private', NEVER to 'public' (private session content must never
    surface as a public consolidated fact)."""
    if not isinstance(payload, dict):
        return "private"
    v = str(payload.get("access_level") or "").strip().lower()
    return v if v in _ACCESS_LEVELS else "private"


def _conflict_allowed(new_level: str, old_level: str) -> bool:
    """Access-level compatibility for conflict resolution.

    public conflicts only with public; trusted with trusted+public;
    private only with private (no mixing upwards). Cross-domain
    conflicts would let one visibility domain suppress or supersede
    another domain's facts.
    """
    return old_level in _ACCESS_CONFLICT_SCOPE.get(new_level, frozenset({"private"}))


def _parse_facts(raw: str) -> Optional[List[str]]:
    """Parse a distiller response into a fact list.

    Returns None when the response is UNPARSEABLE (invalid JSON, no
    'facts' structure) so the caller can leave the source point unmarked
    and retry it on the next tick. A valid response without facts
    returns [].
    """
    txt = _extract_payload((raw or "").strip())
    if not txt:
        return None
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
    return None


_VERDICTS = ("duplicate", "supersede", "unrelated")


def _parse_verdict(raw: str) -> str:
    """Parse the classifier verdict.

    STRICT: only a JSON object whose "verdict" field is one of
    duplicate/supersede/unrelated counts. Everything else — prose,
    embedded instruction text, JSON with a foreign verdict value — is
    treated as 'unrelated' (no action). Keyword heuristics were removed
    on purpose: prose like "do not supersede" must never deactivate a
    memory, and instructions embedded in memory content must not be able
    to drive supersede decisions. A missed conflict is the safe failure
    mode; a false supersede is not.
    """
    txt = _extract_payload((raw or "").strip())
    try:
        d = json.loads(txt)
    except Exception:
        return "unrelated"
    if not isinstance(d, dict):
        return "unrelated"
    v = str(d.get("verdict", "")).strip().lower()
    if v in _VERDICTS:
        return v
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
        if self._llm_fn:
            return self._llm_fn(prompt)
        from nexus_memory.fuel_chain import get_fuel
        fn = get_fuel(OLLAMA_BASE, OLLAMA_MODEL, _ollama_generate)
        if fn is None:
            raise RuntimeError("no fuel station available (daemon sleeps)")
        return fn(prompt)

    def _embed(self, text: str) -> list:
        if self._embed_fn:
            return self._embed_fn(text)
        return _embed_via_store(self._store, text)

    # ── one pass (used by loop AND tests) ────────────────────────────
    def run(self, batch_size: int = 0, dry_run: bool = False) -> Dict[str, Any]:
        batch_size = batch_size or CONSOLIDATION_BATCH
        scanned = facts_created = superseded = duplicates = failed = skipped = 0
        pending_supersedes = self._load_pending_supersedes()
        raw_points = self._next_raw_batch(batch_size)
        for p in raw_points:
            scanned += 1
            payload = p.payload or {}
            # Guardrail override audit entries are NEVER consolidated:
            # their content holds commands + reasoning from protected-
            # resource bypasses. Marked so they don't reappear each batch.
            if payload.get("guardrail_override"):
                skipped += 1
                log.info("consolidation: guardrail override audit point %s excluded", p.id)
                if not dry_run:
                    try:
                        self._store.client.set_payload(
                            self._collection,
                            payload={"consolidated_by": _CONSOLIDATED_BY,
                                     "consolidated_at": time.strftime(
                                         "%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                     "consolidated_facts": 0,
                                     "consolidation_skipped": "guardrail_override_audit"},
                            points=[p.id],
                        )
                    except Exception as mark_exc:
                        # Non-blocking: unmarked points are simply re-skipped
                        # (and re-marked) on the next batch — no LLM cost.
                        log.warning("consolidation: marking skipped point %s failed: %s",
                                    p.id, mark_exc)
                continue
            try:
                text = payload.get("content", "")
                conv = text[:_MAX_CONV_CHARS]
                # Resolve relative times ("yesterday") against the date the
                # SOURCE SESSION was created, not the consolidation run date.
                facts = _parse_facts(self._llm(DISTILL_PROMPT.format(
                    date=_source_date(payload), conv=conv)))
                if facts is None:
                    # Unparseable LLM response — NOT a valid "no facts"
                    # result. Do not mark the source consolidated: its
                    # content would be lost forever. Leave it unmarked so
                    # the next tick retries (fail-safe loop).
                    failed += 1
                    log.warning(
                        "consolidation: point %s got unparseable LLM response "
                        "(left unmarked for retry)", p.id)
                    continue
                src_access = _normalize_access_level(payload)
                created_here = 0
                for fact in facts:
                    decision, supersede_ids = self._resolve_conflicts(
                        fact, access_level=src_access)
                    if decision == "duplicate":
                        duplicates += 1
                        continue
                    if not dry_run:
                        new_id = self._store_fact(fact, p.id, source_payload=payload)
                        for old_id in supersede_ids:
                            try:
                                self._supersede_old(old_id, new_id)
                                superseded += 1
                            except Exception as sup_exc:
                                # A failed supersede must NOT abort the rest
                                # of the batch (the new fact is already
                                # stored). Collect it as pending and retry
                                # on the next tick (see _load_pending_supersedes).
                                failed += 1
                                pending_supersedes.append(
                                    {"old_id": str(old_id), "new_id": str(new_id)})
                                log.warning(
                                    "consolidation: supersede of %s -> %s failed "
                                    "(pending retry): %s", old_id, new_id, sup_exc)
                    created_here += 1
                    facts_created += 1
                if not dry_run:
                    self._mark_consolidated(p.id, created_here)
            except Exception as exc:
                failed += 1
                log.warning("consolidation: point %s failed (skipped): %s", p.id, exc)
        retried, done_ids = self._retry_pending_supersedes(pending_supersedes)
        superseded += retried
        failed += max(0, len(pending_supersedes) - len(done_ids))
        report = {"timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                  "scanned": scanned, "facts_created": facts_created,
                  "superseded": superseded, "duplicates": duplicates,
                  "failed": failed, "skipped": skipped,
                  "pending_supersedes": max(0, len(pending_supersedes) - len(done_ids))}
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
    def _resolve_conflicts(self, fact: str, access_level: str = "private"):
        """Returns (decision, to_supersede_ids).

        decision: 'ok' (store it) or 'duplicate' (drop it).
        Contradictions: old canonical fact ids are returned; caller supersedes
        them AFTER the new fact got its id (superseded_by reference). Never deletes.

        Candidates are filtered by category (fact only), lifecycle (only
        canonical/unknown are considered) AND access level: a fact may only
        conflict with candidates whose access_level is compatible (see
        _conflict_allowed) — public facts must never suppress or supersede
        private memories.
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
            # Access boundary: candidates in a different visibility domain
            # are skipped entirely — never classified, never superseded.
            if not _conflict_allowed(access_level, _normalize_access_level(old_payload)):
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
    def _store_fact(self, fact: str, source_point_id: str,
                    source_payload: Optional[dict] = None) -> str:
        import uuid
        from qdrant_client.models import PointStruct

        src = source_payload if isinstance(source_payload, dict) else {}
        src_access = _normalize_access_level(src)
        access_level = src_access
        # Guardrail override audit entries must never be consolidated
        # into distilled facts (their content contains commands and
        # reasoning from protected-resource bypasses).
        if src.get("guardrail_override"):
            raise _SkipPointError(
                "consolidation: guardrail override audit point skipped (never consolidated)")

        vec = self._embed(fact)
        new_id = str(uuid.uuid4())
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self._store.client.upsert(
            self._collection,
            points=[PointStruct(
                id=new_id, vector=vec,
                payload={"content": fact, "category": "fact",
                         "access_level": access_level,
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

    # ── pending supersede retry store (review-fix: partial failures must
    #    complete on a later tick instead of being lost) ────────────────
    # The list lives at ~/.nexus-memory/consolidation_pending_supersedes.json
    # as [{"old_id": ..., "new_id": ...}, ...]. Small, fail-safe: a broken
    # file is treated as empty (the underlying facts are still canonical;
    # supersede retries resume on the next successful load).

    @staticmethod
    def _pending_supersedes_path():
        import pathlib
        base = os.environ.get("NEXUS_HOME", str(pathlib.Path.home() / ".nexus-memory"))
        return pathlib.Path(base) / "consolidation_pending_supersedes.json"

    def _load_pending_supersedes(self) -> list:
        try:
            with open(self._pending_supersedes_path(), "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, list):
                return [item for item in data
                        if isinstance(item, dict) and item.get("old_id") and item.get("new_id")]
        except FileNotFoundError:
            pass
        except Exception as exc:
            log.warning("consolidation: pending-supersede file unreadable (%s) — "
                        "starting with empty retry list", exc)
        return []

    def _save_pending_supersedes(self, items: list) -> None:
        path = self._pending_supersedes_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(items, fh)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        except Exception as exc:
            # Non-fatal: a failed persist means lost retry bookkeeping, the
            # canonical facts themselves are unaffected (never delete).
            log.warning("consolidation: persisting pending supersedes failed: %s", exc)

    def _retry_pending_supersedes(self, pending: list):
        """Retry collected supersede pairs; returns (succeeded_count, done_ids)."""
        retried, done_ids = 0, []
        for item in pending:
            try:
                self._supersede_old(item["old_id"], item["new_id"])
                retried += 1
                done_ids.append(item)
            except Exception as exc:
                log.warning("consolidation: pending supersede retry %s -> %s still "
                            "failing: %s", item["old_id"], item["new_id"], exc)
        self._save_pending_supersedes([i for i in pending if i not in done_ids])
        return retried, done_ids

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