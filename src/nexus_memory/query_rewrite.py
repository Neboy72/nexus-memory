#!/usr/bin/env python3
"""query_rewrite.py — v0.19.0 Rekonstruktion aus dem 311er-pyc (dis + consts).

Quelle ging beim v0.15-Vorher-Rollback verloren (nur pyc ueberlebt).
Rekonstruktion: exakte Logik aus disassembly + Konstanten; Zeilenzahlen
aus der Original-Datei (41-125) abgebildet.
"""
import logging
import os
import re

log = logging.getLogger("nexus.query_rewrite")

# ── Limits (aus pyc-Konstanten) ──────────────────────────────────────
_MAX_ORIG = 600       # laengere Queries unveraendert durchlassen
_MAX_REWRITE = 400    # Ruecklauf-Deckel (rewritten laenger = unakzeptabel)
_MIN_SAVE_LEN = 3     # unter 3 Zeichen: Rewrite lohnt nicht
_TIMEOUT_S = 10       # Default wall-clock bound; ENFORCED by the provider wiring
                      # (future.result timeout) + per-station partial timeout via
                      # consolidation.get_default_fuel(timeout=_TIMEOUT_S).

_PROMPT = (
    "Rewrite the search query so a keyword+vector memory search finds it. "
    "Resolve pronouns and vague nouns into concrete search terms, keep the "
    "original language (German stays German), max 10 words, one line, no "
    "explanation, no quotes, no thinking.\n\n"
    "Examples:\n"
    "Q: was war das mit dem ding furs auto\n"
    "A: wallbox ladekabel rfid ruckgabe\n"
    "Q: wies das thema mit dem hund\n"
    "A: hund spaziergange routine futter\n"
    "Q: anwalt kreditkarte\n"
    "A: anwalt amazon kreditkarte rechtlich\n\n"
    "Q: {q}\nA:"
)


def enabled() -> bool:
    """Ships enabled (v0.19.0): rewriting ships active so every user benefits
    without configuration. NEXUS_REWRITE=0 is the emergency OFF brake;
    anything else (unset, 1, true, yes) keeps it enabled."""
    return os.getenv("NEXUS_REWRITE", "").strip().lower() not in ("0", "false", "no", "off")


def _clean_output(txt: str) -> str:
    """GLM sanitation + rewrite-specific cleanup.

    The think-strip delegates to the CANONICAL consolidation._extract_payload
    (simplify-review 13.09.: one shared implementation, no drift); the
    rewrite-specific steps follow: answer-prefix, quotes, first line,
    trailing-dash explanation, meta-sentence check. Fail-open to the raw
    text when consolidation is unavailable."""
    if not txt:
        return ""
    try:
        from nexus_memory.consolidation import _extract_payload
        t = _extract_payload(str(txt))
    except Exception:
        t = str(txt)
    t = t.strip()
    t = re.sub(r"^(A:|A\s*[\):]|Antwort:)\s*", "", t, flags=re.IGNORECASE)
    t = t.strip().strip('"').strip("'")
    if t:
        t = t.splitlines()[0].strip()
    # Strip a trailing " - explanation" tail.
    t = re.sub(r"\s*[-–]\s*[^\"']{12,}$", "", t)
    if re.search(r"(antwort|here (is|'s)|hier ist|suchanfrage|reformuliert|suchbegriff)", t, re.IGNORECASE):
        colon = t.find(":")
        tail = t[colon + 1:].strip() if colon >= 0 else ""
        if tail and len(tail) >= 2 and not re.search(r"(antwort|hier ist)", tail, re.IGNORECASE):
            t = tail
        else:
            t = ""
    return t.strip()


def _acceptable(rewritten: str, original: str) -> bool:
    """Reject garbage: empty, identical, echo-prefixed, runaway long, meta."""
    r = rewritten.strip()
    if not r or len(r) < 2 or len(r) > _MAX_REWRITE:
        return False
    if r.lower() == original.lower().strip():
        return False
    if r.lower().startswith(("a:", "answer", "query:", "here", "i ")) or r.lower().endswith("..."):
        return False
    if re.search(r"<think|```|\\n", r):
        return False
    if r.count(" ") > 14:
        return False
    return True


def rewrite_query(query: str, generate_fn) -> str:
    """Return the rewritten query, or the original on any failure (fail-open).

    ``generate_fn``: a ``prompt -> text`` callable (fuel chain dispatcher).
    Callers resolve it via fuel_chain.get_fuel() and pass None when no station
    is open — in every such case the original query is returned unchanged.
    """
    q = str(query or "").strip()
    if not enabled():
        return q
    # Too short to matter, too long to trust.
    if len(q) < _MIN_SAVE_LEN or len(q) > _MAX_ORIG:
        return q
    # Short numeric queries (ports, IDs): never rewrite ("port 9220").
    if re.search(r"\d", q) and len(q) < 60:
        return q
    if generate_fn is None:
        return q

    prompt = _PROMPT.format(q=q)
    try:
        raw = generate_fn(prompt)
    except Exception as exc:
        # Hot path: keep logs quiet (simplify-review 13.09.) — DEBUG only,
        # no query content in INFO logs.
        log.debug("query-rewrite: station unavailable (%s) — original query", exc)
        return q

    rewritten = _clean_output(raw or "")
    if not _acceptable(rewritten, q):
        log.debug("query-rewrite: unacceptable output — original query")
        return q

    log.debug("query-rewrite: %.60r -> %.60r", q, rewritten)
    return rewritten