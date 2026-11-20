"""Dreaming: nightly pattern-learning from agent session history (v0.22.0).

Brain-equivalent: hippocampal replay during sleep (Rasch & Born 2013).
Reads agent session stores (Hermes state.db sessions table, or a generic
JSONL session log), extracts recurring patterns/workarounds via the same
LLM fuel the consolidator uses, deduplicates against the memory store via
similarity, and writes learned playbooks.

Design rules:
- Additive only: never touches consolidation.py behavior.
- Never deletes anything (dreaming is learning, not forgetting).
- Env knobs follow the NEXUS_* convention; all optional, fail-open.
- Agent-neutral: no hardcoded ~/.hermes paths (env-configurable).

Env knobs:
- NEXUS_DREAMING (default "1")           master kill-switch
- NEXUS_DREAM_STORE (default "0")       write learned playbooks into the
  memory store. Off by default: measured 20.11.2026, the extractor still
  feeds text windows of long prompts as "patterns", which are not lessons.
  The JSON file under learned-playbooks/ is written either way.
- NEXUS_DREAMING_SOURCES (optional)      JSON list of source configs, e.g.
  [{"type": "hermes", "db": "/abs/path/state.db"},
   {"type": "jsonl", "dir": "/abs/dir"}]
- NEXUS_DREAMING_LOOKBACK_HOURS (24)
- NEXUS_DREAMING_MIN_TOOLCALLS (5)
- NEXUS_DREAMING_PLAYBOOK_DIR (~/.nexus-memory/learned-playbooks)
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

log = logging.getLogger("nexus.dreaming")

NEXUS_DREAMING_ENABLED = os.environ.get("NEXUS_DREAMING", "1") == "1"
LOOKBACK_HOURS = float(os.environ.get("NEXUS_DREAMING_LOOKBACK_HOURS", "24"))
MIN_TOOLCALLS = int(os.environ.get("NEXUS_DREAMING_MIN_TOOLCALLS", "5"))
PLAYBOOK_DIR = Path(os.environ.get(
    "NEXUS_DREAMING_PLAYBOOK_DIR",
    str(Path.home() / ".nexus-memory" / "learned-playbooks")))

_JA_NEIN = re.compile(r"(?i)\b(ja|nein|yes|no)\b")


def _verdict_ist_nein(verdict: str) -> bool:
    """Liest das erste klare Ja oder Nein aus dem Urteil.

    Bis 0.22.59 wurde nur ``verdict.lower()[:12]`` geprueft. Die Antwort des
    Modells beginnt aber meist mit "Wiederkehrendes Muster: nein", das Nein
    steht also hinter Zeichen 12 und rutschte durch — jedes Urteil galt als
    Muster. Jetzt zaehlt das erste Ja/Nein-Wort im ganzen Text.
    """
    text = str(verdict or "").lower()
    treffer = _JA_NEIN.search(text)
    if not treffer:
        return True  # ohne klares Ja wird nichts gelernt
    return treffer.group(1) in ("nein", "no")


def _store_collection(store) -> Optional[str]:
    """Name der Sammlung, egal wie das Speicherobjekt ihn traegt.

    ``MemoryStore`` hat kein Feld ``collection`` — es haelt den Namen in
    ``_binding.collection_name``. Das alte ``self._store.collection`` warf
    deshalb immer einen AttributeError, der in ``_similar_known`` still
    gefangen wurde: die Dublettenpruefung lief nie.
    """
    for feld in ("collection", "collection_name"):
        wert = getattr(store, feld, None)
        if isinstance(wert, str) and wert:
            return wert
    binding = getattr(store, "_binding", None)
    if binding is not None:
        wert = getattr(binding, "collection_name", None)
        if isinstance(wert, str) and wert:
            return wert
    return None


def _embed_sync(store, text: str) -> list:
    """Einbettung aus dem Daemon-Thread (dort laeuft keine Schleife)."""
    import asyncio
    return asyncio.run(store._embedder.embed(text, is_query=False))

_SESSION_ID_RE = re.compile(r"^\d{8}_\d{6}_[0-9a-f]{6,}$")

# Conservative: only explicit failure/fix language counts as a pattern hint.
_HINT_RE = re.compile(
    r"(?i)(fix|workaround|patched|fehler|error|fail|bug|scheitert|crash|"
    r"geht nicht|lektion|lesson|immer erst|nie wieder|never again|gotcha)")
_HINT_WINDOW = 140
_MAX_PATTERNS_PER_SESSION = 5
_MAX_SESSIONS_PER_RUN = 20


def _session_sources() -> List[Dict[str, Any]]:
    """Resolve source list from NEXUS_DREAMING_SOURCES or auto-detect."""
    raw = os.environ.get("NEXUS_DREAMING_SOURCES")
    if raw:
        try:
            srcs = json.loads(raw)
            if isinstance(srcs, list):
                return [s for s in srcs if isinstance(s, dict)]
        except Exception:
            log.warning("dreaming: bad NEXUS_DREAMING_SOURCES JSON, auto-detect")
    hermes_home = os.environ.get("NEXUS_HERMES_HOME",
                                 str(Path.home() / ".hermes"))
    db = Path(hermes_home) / "state.db"
    if db.exists():
        return [{"type": "hermes", "db": str(db)}]
    return []


def _iter_hermes_sessions(db_path: str, cutoff: float) -> List[Dict[str, Any]]:
    """Sessions with tool usage, ended, newer than cutoff (epoch seconds)."""
    out: List[Dict[str, Any]] = []
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        rows = conn.execute(
            "SELECT id, started_at, message_count, tool_call_count "
            "FROM sessions WHERE ended_at IS NOT NULL AND tool_call_count >= ? "
            "AND started_at > ? ORDER BY started_at DESC LIMIT ?",
            (MIN_TOOLCALLS, cutoff, _MAX_SESSIONS_PER_RUN)).fetchall()
        conn.close()
    except Exception as exc:
        log.warning("dreaming: hermes db unreadable (%s) — fail-open", exc)
        return out
    for sid, started, msgs, tools in rows:
        out.append({"id": sid, "started_at": started,
                    "message_count": msgs, "tool_calls": tools,
                    "type": "hermes", "db": db_path})
    return out


def _iter_jsonl_sessions(dir_path: str, cutoff: float) -> List[Dict[str, Any]]:
    """Generic JSONL session-log source: newest-modified files as sessions."""
    out: List[Dict[str, Any]] = []
    try:
        d = Path(dir_path)
        if not d.is_dir():
            return out
        for f in sorted(d.glob("*.jsonl"), key=lambda p: p.stat().st_mtime,
                        reverse=True)[:_MAX_SESSIONS_PER_RUN]:
            if f.stat().st_mtime >= cutoff:
                out.append({"id": f.stem, "path": str(f),
                            "started_at": f.stat().st_mtime,
                            "tool_calls": MIN_TOOLCALLS, "type": "jsonl"})
    except Exception as exc:
        log.warning("dreaming: jsonl source unreadable (%s) — fail-open", exc)
    return out


def _marker_path(base: Path) -> Path:
    return base / ".dreaming-last-run"


def _read_marker(base: Path) -> set:
    try:
        return set(_marker_path(base).read_text(encoding="utf-8").splitlines())
    except Exception:
        return set()


def _write_marker(ids: List[str], base: Path) -> None:
    try:
        marker = _marker_path(base)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("\n".join(ids), encoding="utf-8")
    except Exception as exc:
        log.warning("dreaming: marker write failed (%s)", exc)


def _new_session_ids(ids: List[str], base: Path) -> List[str]:
    """Pure check, NO side effects: ids not seen in a committed marker.

    v0.22.1 review fix: the marker used to be written HERE, before any
    learning happened — a failed pass (or a dry_run!) burned the sessions
    forever. Committing is now the caller's job, after a successful pass.
    """
    seen = _read_marker(base)
    return [i for i in ids if i not in seen]


def _extract_hints(session: Dict[str, Any]) -> List[str]:
    """Pull pattern-candidate snippets from a session's message bodies."""
    hints: List[str] = []
    try:
        if session["type"] == "hermes":
            conn = sqlite3.connect(f"file:{session['db']}?mode=ro", uri=True)
            rows = conn.execute(
                "SELECT content FROM messages WHERE session_id = ?",
                (session["id"],)).fetchall()
            conn.close()
            texts = [str(r[0]) for r in rows]
        else:
            texts = [Path(session["path"]).read_text(encoding="utf-8",
                                                      errors="replace")]
    except Exception as exc:
        log.info("dreaming: session %s unreadable (%s)", session["id"], exc)
        return hints
    for t in texts:
        for m in _HINT_RE.finditer(t):
            start = max(0, m.start() - 20)
            snippet = t[start:start + _HINT_WINDOW].strip()
            if len(snippet) > 30 and snippet not in hints:
                hints.append(snippet)
            if len(hints) >= _MAX_PATTERNS_PER_SESSION:
                return hints
    return hints


class Dreamer:
    """Nightly dreaming pass: learn patterns from agent session history."""

    def __init__(self, store=None, llm_fn: Optional[Callable[[str], str]] = None):
        self._store = store
        self._llm_fn = llm_fn

    def _similar_known(self, text: str) -> bool:
        """Dedup against the store: a vector hit above threshold = known."""
        if self._store is None:
            return False
        try:
            vec = self._store._embed([text])[0]
            qp_kwargs = {
                "collection_name": _store_collection(self._store),
                "query": vec,
                "limit": 1,
            }
            vector_name = getattr(self._store, "vector_name", None)
            if vector_name:
                qp_kwargs["using"] = vector_name
            resp = self._store.client.query_points(**qp_kwargs)
            hits = resp.points
            if hits and hits[0].score >= 0.92:
                return True
        except Exception as exc:
            log.info("dreaming: dedup-embed skipped (%s)", exc)
        return False

    def _store_playbook(self, playbook: Dict[str, Any]) -> bool:
        """Schreibt ein gelerntes Muster als Eintrag in den Speicher.

        Bis 0.22.59 blieben Traeume Dateien auf der Platte: der Speicher wurde
        nur gefragt, ob etwas bekannt ist, geschrieben wurde nie. Damit war das
        Gelernte fuer niemanden auffindbar. Kategorie ``procedure``, weil es
        Handlungswissen ist und vom Alternsaudit geschuetzt wird.
        """
        if self._store is None:
            return False
        import uuid
        from qdrant_client.models import PointStruct
        from nexus_memory.collection_vectors import upsert_named

        muster = str(playbook.get("pattern", "")).strip()
        urteil = str(playbook.get("verdict", "")).strip()
        if not muster:
            return False
        text = f"Gelernt: {muster}" + (f" — {urteil}" if urteil else "")
        text = " ".join(text.split())[:900]

        # ── Gemeinsames Schreibtor (20.11.2026) ───────────────────────
        # Das Traeumen schrieb als eigener Weg direkt in die Sammlung. Mit
        # NEXUS_DREAM_STORE=0 ist der Schreibpfad abgeschaltet; sobald er
        # wieder anschaltet wird, gilt dasselbe Tor wie fuer jeden Agenten.
        from nexus.write_gate import prepare as _gate, last_reason as _gate_reason
        _prepared = _gate(
            content=text, category="procedure", access_level="private",
            source="nexus-dreaming", source_type="dreaming",
            provenance={"source_type": "dreaming",
                        "created_by": "dreaming-v1"},
        )
        if _prepared is None:
            log.info("dreaming: Schreibtor weist ab (%s) — Muster nicht gespeichert",
                     _gate_reason() or "refused")
            return False
        text = _prepared["content"]

        vec = _embed_sync(self._store, text)
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        point = PointStruct(
            id=str(uuid.uuid4()), vector=vec,
            payload={"content": text, "text": text, "category": "procedure",
                     "access_level": "private", "scope": "default",
                     "source": "nexus-dreaming", "created_at": now,
                     "lifecycle_status": "canonical", "confidence": 0.6,
                     "provenance": {"source_type": "dreaming",
                                    "created_by": "dreaming-v1",
                                    "session_id": playbook.get("session_id"),
                                    "timestamp": now}},
        )
        sammlung = _store_collection(self._store)
        if not sammlung:
            log.warning("dreaming: Sammlungsname unbekannt — Eintrag nicht geschrieben")
            return False
        upsert_named(self._store.client, sammlung, [point],
                     getattr(self._store, "vector_name", None))
        return True

    def dream(self, dry_run: bool = False) -> Dict[str, Any]:
        """One dreaming pass. Returns a summary dict; never raises."""
        result: Dict[str, Any] = {"sessions_scanned": 0, "patterns": 0,
                                  "playbooks": 0, "dry_run": dry_run}
        if not NEXUS_DREAMING_ENABLED or os.environ.get("NEXUS_DREAMING", "1") != "1":
            result["skipped"] = "disabled"
            return result
        try:
            cutoff = time.time() - LOOKBACK_HOURS * 3600
            sessions: List[Dict[str, Any]] = []
            for src in _session_sources():
                if src.get("type") == "hermes":
                    sessions.extend(_iter_hermes_sessions(src["db"], cutoff))
                elif src.get("type") == "jsonl":
                    sessions.extend(_iter_jsonl_sessions(src["dir"], cutoff))
            result["sessions_scanned"] = len(sessions)
            if not sessions:
                return result
            base = PLAYBOOK_DIR
            fresh = _new_session_ids([s["id"] for s in sessions], base)
            result["new_sessions"] = len(fresh)
            if not fresh:
                return result
            fresh_set = set(fresh)
            learned: List[Dict[str, str]] = []
            pass_error: Optional[str] = None
            for s in sessions:
                if s["id"] not in fresh_set:
                    continue
                for hint in _extract_hints(s):
                    if self._similar_known(hint):
                        continue
                    if self._llm_fn is not None and not dry_run:
                        try:
                            verdict = self._llm_fn(
                                "Wiederkehrendes Muster ja/nein, dann 1 Satz "
                                "Muster + 1 Satz Aktion. Text: " + hint)
                        except Exception as llm_exc:
                            # LLM fuel down mid-pass → stop WITHOUT committing
                            # the marker: these sessions stay learnable (review
                            # fix v0.22.1; old behavior burned them forever).
                            log.warning("dreaming: llm_fn failed (%s) — "
                                        "aborting pass, marker NOT committed",
                                        llm_exc)
                            pass_error = f"llm_fn failed: {llm_exc}"[:200]
                            break
                        if _verdict_ist_nein(verdict):
                            continue
                        playbook = {"pattern": hint,
                                    "verdict": verdict[:300],
                                    "session_id": s["id"],
                                    "ts": time.strftime("%Y-%m-%dT%H:%M:%S")}
                    else:
                        playbook = {"pattern": hint, "session_id": s["id"]}
                    learned.append(playbook)
                    if len(learned) >= 20:
                        break
                if pass_error or len(learned) >= 20:
                    break
            if pass_error:
                result["error"] = pass_error
                return result
            if learned and not dry_run:
                geschrieben = 0
                if _store_enabled():
                    for pb in learned:
                        try:
                            if self._store_playbook(pb):
                                geschrieben += 1
                        except Exception as exc:
                            log.info("dreaming: playbook write skipped (%s)", exc)
                else:
                    result["store_skipped"] = (
                        "NEXUS_DREAM_STORE=0 — Playbooks nur als Datei")
                result["stored"] = geschrieben
                base.mkdir(parents=True, exist_ok=True)
                out = base / f"dream-{time.strftime('%Y%m%d-%H%M%S')}.json"
                out.write_text(json.dumps(learned, ensure_ascii=False,
                                          indent=1), encoding="utf-8")
            if not dry_run:
                # Commit AFTER a successful pass only (review fix v0.22.1).
                _write_marker([s["id"] for s in sessions], base)
            result["patterns"] = len(learned)
            result["playbooks"] = 1 if learned and not dry_run else 0
        except Exception as exc:
            log.warning("dreaming: pass failed (%s) — fail-open", exc)
            result["error"] = str(exc)[:200]
        return result


def _store_enabled() -> bool:
    """Whether learned playbooks may be written into the memory store.

    Default off (NEXUS_DREAM_STORE=0). The store is only useful when the
    extracted pattern is a real recurring lesson; until the extractor is
    fixed it produces text slices, so the JSON file is the safer sink.
    """
    return str(os.environ.get("NEXUS_DREAM_STORE", "0")).strip().lower() in (
        "1", "true", "yes", "on")


def dream_once(store=None, llm_fn=None, dry_run: bool = False) -> Dict[str, Any]:
    """Entry point for the daemon loop or manual invocation."""
    return Dreamer(store=store, llm_fn=llm_fn).dream(dry_run=dry_run)