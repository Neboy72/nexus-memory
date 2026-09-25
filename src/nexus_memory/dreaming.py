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


def _new_session_ids(ids: List[str], base: Path) -> List[str]:
    """Marker-based idempotency: only ids not seen in the last run."""
    marker = _marker_path(base)
    try:
        seen = set(marker.read_text(encoding="utf-8").splitlines())
    except Exception:
        seen = set()
    fresh = [i for i in ids if i not in seen]
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("\n".join(ids), encoding="utf-8")
    except Exception as exc:
        log.warning("dreaming: marker write failed (%s)", exc)
    return fresh


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
            hits = self._store.client.search(
                collection_name=self._store.collection,
                query_vector=vec, limit=1,
                query_filter=None)
            if hits and hits[0].score >= 0.92:
                return True
        except Exception as exc:
            log.info("dreaming: dedup-embed skipped (%s)", exc)
        return False

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
            for s in sessions:
                if s["id"] not in fresh_set:
                    continue
                for hint in _extract_hints(s):
                    if self._similar_known(hint):
                        continue
                    if self._llm_fn is not None and not dry_run:
                        verdict = self._llm_fn(
                            "Wiederkehrendes Muster ja/nein, dann 1 Satz "
                            "Muster + 1 Satz Aktion. Text: " + hint)
                        if not verdict or "nein" in verdict.lower()[:12]:
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
                if len(learned) >= 20:
                    break
            if learned and not dry_run:
                base.mkdir(parents=True, exist_ok=True)
                out = base / f"dream-{time.strftime('%Y%m%d-%H%M%S')}.json"
                out.write_text(json.dumps(learned, ensure_ascii=False,
                                          indent=1), encoding="utf-8")
            result["patterns"] = len(learned)
            result["playbooks"] = 1 if learned and not dry_run else 0
        except Exception as exc:
            log.warning("dreaming: pass failed (%s) — fail-open", exc)
            result["error"] = str(exc)[:200]
        return result


def dream_once(store=None, llm_fn=None, dry_run: bool = False) -> Dict[str, Any]:
    """Entry point for the daemon loop or manual invocation."""
    return Dreamer(store=store, llm_fn=llm_fn).dream(dry_run=dry_run)