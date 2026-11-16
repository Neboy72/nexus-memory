#!/usr/bin/env python3
"""Nexus Memory Auto-Capture Hook for Claude Code.

Fires on Stop (when Claude finishes responding). Reads the transcript,
extracts notable facts/decisions, and stores them in Qdrant.

Uses a lightweight extraction approach: looks for key patterns in the
conversation and stores them as memories.
"""

import sys
import json
import logging
import os
import urllib.request
import re
from datetime import datetime, timezone
from pathlib import Path

# The shared embedding path: automatic = the local Nexus service (HuggingFace),
# explicit = the provider you name. See _embedding.py for the rule.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _embedding  # noqa: E402

# Config
QDRANT_URL = os.getenv("NEXUS_QDRANT_URL", "http://localhost:6333")
COLLECTION = os.getenv("NEXUS_COLLECTION", "nexus")
EMBEDDING_MODEL = os.getenv("NEXUS_EMBEDDING_MODEL", "Qwen/Qwen3-Embedding-0.6B")
EMBEDDING_PROVIDER = os.getenv("NEXUS_EMBEDDING_PROVIDER", "auto")
AGENTS_FILE = Path.home() / ".nexus-memory" / "agents.json"


def _central_env() -> dict:
    """Read the shared ``~/.hermes/.env`` so the hook follows the host.

    Hooks are started by the app, not by a login shell: they see neither the
    shell's exports nor a ``.env`` the user sourced. Without this, the hook
    falls back to its own defaults and silently writes to a different
    collection / provider than the MCP server of the same install.

    Only three keys are honoured, and the real environment always wins, so an
    explicit override (settings.json, shell export) is never overridden.
    """
    out = {}
    path = os.environ.get("NEXUS_ENV_FILE",
                          str(Path.home() / ".hermes" / ".env"))
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                if key.startswith("export "):
                    key = key[7:].strip()
                if key in ("NEXUS_COLLECTION", "NEXUS_EMBEDDING_PROVIDER",
                           "NEXUS_EMBEDDING_MODEL"):
                    out.setdefault(key, val.strip().strip('"').strip("'"))
    except OSError:
        pass
    return out


_CENTRAL = _central_env()
COLLECTION = os.getenv("NEXUS_COLLECTION") or _CENTRAL.get("NEXUS_COLLECTION") or COLLECTION
EMBEDDING_PROVIDER = (os.getenv("NEXUS_EMBEDDING_PROVIDER")
                      or _CENTRAL.get("NEXUS_EMBEDDING_PROVIDER")
                      or EMBEDDING_PROVIDER)
EMBEDDING_MODEL = (os.getenv("NEXUS_EMBEDDING_MODEL")
                   or _CENTRAL.get("NEXUS_EMBEDDING_MODEL")
                   or EMBEDDING_MODEL)

_SCOPE_RE = __import__("re").compile(r"^[a-z0-9][a-z0-9-]{0,39}$")


def _resolve_capture_scope(category: str) -> str:
    """Schreibziel des Captures: genau zwei Bereiche (entschieden 15.11.2026).

    Ein Sitzungsprotokoll (``category == "session"``) gehoert in den EIGENEN
    Verbund — ``scope_auto.own_bundle()``: ein gueltiges ``NEXUS_SCOPE``,
    sonst die Agenten-Kennung (Vorgabe ``claude-code``). Alles andere gehoert
    in den GEMEINSAMEN Bereich ``default``.

    ``infer_scope`` wird hier bewusst NICHT mehr befragt: ein aus Zentroiden
    geratener Themen-Bereich ist kein Verbund, und ein Punkt in einem
    geratenen Bereich waere fuer ALLE unsichtbar — auch fuer den Schreiber.
    Fail-open: jeder Fehler bei der Ermittlung → ``default``.
    """
    if category != "session":
        return "default"
    try:
        import scope_auto as _scope_auto  # same dir
        return _scope_auto.own_bundle()
    except Exception as exc:
        print(f"[nexus auto_capture] own bundle unresolved ({exc}) — default", file=sys.stderr)
        return "default"


def manual_scope_ok(s: str) -> bool:
    """Public scope validator — the single source of truth (H252).

    Applies the shared normalization (``_normalize_scope``: strip + lowercase)
    and then the scope contract ``^[a-z0-9][a-z0-9-]{0,39}$``. Empty or
    otherwise invalid input → False, so the caller falls through to
    'default' (never to a guessed area).

    H252: a hand-built per-char check previously accepted a leading dash
    ("-foo", "-" alone) and rejected uppercase — diverging from ``_SCOPE_RE``
    and from the MCP server, so the same scope could be accepted on one path
    and silently dropped on another.

    NB: validates the normalized value directly rather than via
    ``_normalize_scope`` — that helper maps anything invalid to ``"default"``,
    which is itself a valid scope and would make every input pass.
    """
    if not isinstance(s, str):
        return False
    normalized = s.strip().lower()
    if not normalized:
        return False
    return bool(_SCOPE_RE.match(normalized))


# Backwards-compatible alias (older callers used the underscore name).
_manual_scope_ok = manual_scope_ok


def _normalize_scope(scope) -> str:
    """Normalize a scope label (project/agent areas). Fail-open to 'default'.

    Same contract as the MCP server's _normalize_scope: valid = non-empty
    [a-z0-9-] string, max 40 chars; anything else (None, empty, uppercase,
    too long, non-str) degrades to 'default' so callers never break.
    """
    if isinstance(scope, str):
        s = scope.strip().lower()
        if s and _SCOPE_RE.match(s):
            return s
    return "default"


def _resolve_trust_level() -> str:
    """Gatekeeper: resolve this agent's trust level from agents.json.

    Uses NEXUS_AGENT_ID env var to identify the caller. This hook inherits the
    shell's value (a deliberate override), unlike the MCP server which
    plugin.json pins to "claude-code". Set NEXUS_AGENT_ID shell-wide for
    consistent attribution across both paths.
    Falls back to 'public' for unknown agents (safest default).
    """
    agent_id = os.getenv("NEXUS_AGENT_ID", "claude-code")
    if not agent_id:
        return "public"

    try:
        with open(AGENTS_FILE) as f:
            registry = json.load(f)
        for agent in registry.get("agents", []):
            # A renamed agent keeps its old name under "formerly": the caller may
            # still identify itself with the old one (env var, older config).
            known = [agent.get("id")] + list(agent.get("formerly") or [])
            if agent_id in known:
                trust = agent.get("trust_level", "public")
                if trust in ("public", "trusted", "private"):
                    return trust
                return "public"
        return "public"  # Agent not found in registry
    except Exception as exc:
        # Falling back to 'public' is the SAFE direction (no private memories are
        # disclosed), so the default stays — but it must not happen in silence:
        # a broken registry silently downgrades every write.
        print(
            f"[nexus auto-capture] could not read {AGENTS_FILE!r}, "
            f"defaulting to the safest level 'public' ({exc})",
            file=sys.stderr,
        )
        return "public"

def _agent_id() -> str:
    """Who this hook is: ``NEXUS_AGENT_ID``, else ``claude-code``.

    Used for the local movement-log line only. The service learns the sender
    from the agent key (15.11.2026) — no header claims it any more.
    """
    return (os.getenv("NEXUS_AGENT_ID") or "").strip() or "claude-code"


def get_embedding(text: str) -> list:
    """Get embedding from configured provider.

    Returns None (→ nothing stored) on misconfiguration or failure. H253:
    misconfiguration is now reported on stderr instead of silently returning
    None, which made auto-capture quietly store nothing.
    """
    if _embedding.is_serve_provider(EMBEDDING_PROVIDER):
        # Automatic path: the local Nexus service embeds the document side.
        # 15.11.2026: no caller header any more — the agent key names us.
        return _embedding.embed_via_serve(text, is_query=False)
    try:
        if EMBEDDING_PROVIDER == "ollama":
            # W32-9: endpoint ↔ field mismatch. The legacy /api/embeddings
            # endpoint reads the text from "prompt" and returns a single
            # "embedding"; the current /api/embed reads "input" and returns
            # "embeddings". We called the legacy URL with the new "input"
            # field, so Ollama received no prompt and returned no vector.
            # Migrated to the new API (same one confidence._embed uses).
            req_data = json.dumps({
                "model": os.getenv("NEXUS_OLLAMA_EMBED_MODEL", "qwen3-embedding:0.6b"),
                "input": text
            }).encode()
            req = urllib.request.Request(
                "http://localhost:11434/api/embed",
                data=req_data,
                headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
            embeddings = data.get("embeddings") or []
            return embeddings[0] if embeddings else None
        else:
            print(
                f"[nexus auto-capture] unknown NEXUS_EMBEDDING_PROVIDER="
                f"{EMBEDDING_PROVIDER!r} — memory not stored",
                file=sys.stderr,
            )
            return None
    except Exception as exc:
        print(f"[nexus auto-capture] embedding failed: {exc}", file=sys.stderr)
        return None

def _point_vector(embedding):
    """The Qdrant point ``vector`` value: list for anonymous, {name: [...]} for named.

    The write-side twin of ``_vector_body``: on a named collection a bare list is
    rejected with 400 "Not existing vector name" and the surrounding except
    swallowed it, so captures vanished without a word.

    The import is made robust on purpose. Hooks are started from many different
    working directories, so the sibling module is not always on ``sys.path``. The
    bare ``except`` then fell back to the flat list — the very shape a named
    collection rejects — and every capture failed silently. Measured 09.10.2026:
    without the script directory on the path this returned a list, with it a dict.
    """
    try:
        import scope_auto as _scope_auto
    except ImportError:
        # Load the sibling by its own file path instead of giving up.
        try:
            import importlib.util
            from pathlib import Path

            _pfad = Path(__file__).resolve().parent / "scope_auto.py"
            _spec = importlib.util.spec_from_file_location("scope_auto", _pfad)
            _scope_auto = importlib.util.module_from_spec(_spec)
            sys.modules.setdefault("scope_auto", _scope_auto)
            _spec.loader.exec_module(_scope_auto)
        except Exception as exc:
            # NEVER fall back silently here. The flat list this used to return is
            # the exact shape a collection with a named vector rejects with
            # ``400 Not existing vector name`` — so every capture failed and the
            # hook still reported success (measured 09.10.2026). Report instead:
            # a wrong vector is worse than a visible failure.
            print(
                f"[nexus auto-capture] cannot load scope_auto, refusing to guess "
                f"the vector layout ({exc})",
                file=sys.stderr,
            )
            return embedding
    try:
        return _scope_auto.point_vector_body(embedding, QDRANT_URL, COLLECTION)
    except Exception as exc:
        # Same reasoning: an unresolvable layout is a defect, not a reason to send
        # a vector shape that may be wrong. Keep the flat list as the last resort
        # (anonymous collections need it) but say so, loudly.
        print(
            f"[nexus auto-capture] vector layout unresolved, falling back to the "
            f"flat protocol — captures may be rejected ({exc})",
            file=sys.stderr,
        )
        return embedding


_GATE_LOAD_ERROR = ""


def _prepare_write(**fields):
    """Ruft das gemeinsame Schreibtor (``nexus.write_gate``).

    Der Hook laeuft in der Umgebung der Adapter-Installation. Laesst sich das
    Tor dort nicht laden, wird der Schreibvorgang abgewiesen und der Grund auf
    stderr gemeldet (stdout ist der Hook-Kanal) — ein fehlendes Tor darf nicht
    still den ungepruefte Zustand wiederherstellen (10.11.2026).
    """
    global _GATE_LOAD_ERROR
    try:
        from nexus.write_gate import prepare
    except Exception as exc:
        _GATE_LOAD_ERROR = f"write gate not loadable: {type(exc).__name__}: {exc}"
        print(f"[nexus auto-capture] {_GATE_LOAD_ERROR} — abgewiesen",
              file=sys.stderr)
        return None
    _GATE_LOAD_ERROR = ""
    return prepare(**fields)


def _write_refusal() -> str:
    """Grund der Abweisung, wenn das Tor einen kennt."""
    try:
        from nexus.write_gate import last_reason
        return last_reason() or _GATE_LOAD_ERROR or "abgewiesen"
    except Exception:
        return _GATE_LOAD_ERROR or "abgewiesen"


def _log_movement(action, result, *, text="", access_level="", category=None,
                  scope=None, target_id=None, reason=None, after_text=None):
    """Eine Zeile ins Bewegungsprotokoll — fail-open, ``via="hook"``.

    Der Hook schrieb bisher direkt nach Qdrant und damit an jeder
    Protokollzeile vorbei (15.11.2026). Kein Inhalt: das Thema bestimmt
    ``service_log.record`` (bei ``private`` leer, Zahlen ersetzt), sonst
    stehen nur Kennung, Kategorie, Bereich und Pruefsumme in der Zeile.
    Meldungen gehen nach stderr, nie nach stdout — stdout ist der Hook-Kanal
    und muss sauber bleiben.

    ``actor`` ist die eigene Kennung: der Hook schreibt direkt nach Qdrant,
    es gibt keinen Dienst, der sie setzen koennte — ohne sie stuende
    ``unidentified`` in einer Zeile, die dieser Hook selbst verfasst.
    """
    try:
        from nexus_memory import service_log
        service_log.record(
            action, result, text=text, access_level=access_level,
            target_id=target_id, category=category, scope=scope,
            reason=reason,
            actor=_agent_id(),
            after_hash=(service_log.content_hash(after_text)
                        if after_text is not None else None),
            via="hook",
        )
    except Exception as exc:  # noqa: BLE001 — Protokoll ist eine Zugabe
        print(f"[nexus auto-capture] Bewegungsprotokoll nicht geschrieben: {exc}",
              file=sys.stderr)


def store_memory(text: str, category: str = "session", point_id: str = None):
    """Store a memory in Qdrant."""
    import uuid
    point_id = point_id or str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    # Gemeinsames Schreibtor (10.11.2026): leerer Inhalt und Etiketten werden
    # abgewiesen, die Herkunft bleibt nie leer, und die Zugriffsstufe ist die
    # strengste aus Quelle und Angabe. ``chat`` ist die Herkunftsart dieses
    # Weges: der Hook liest ein Gespraech, die Quelle steht daneben.
    requested_level = _resolve_trust_level()
    prepared = _prepare_write(
        content=text,
        category=category,
        access_level=requested_level,
        source="claude-code",  # guard-ok: Name des Adapters, kein Modell
        provenance={
            "source_type": "claude-code",  # guard-ok: Name des Adapters, kein Modell
            "created_by": "claude-code-auto-capture",  # guard-ok: Name des Adapters, kein Modell
            "timestamp": now,
        },
        source_type="chat",
    )
    if prepared is None:
        reason = _write_refusal()
        # Fehlversuche gehoeren ins Protokoll: ohne sie saehe ein Hook, der
        # jeden Turn gegen die Wand laeuft, wie ein Hook ohne Arbeit aus.
        _log_movement("denied", "refused", text=text,
                      access_level=requested_level, category=category,
                      reason=reason)
        print(
            f"[nexus auto-capture] memory rejected: {reason}",
            file=sys.stderr,
        )
        return False

    text = prepared["content"]
    category = prepared["category"]
    access_level = prepared["access_level"]
    source = prepared["source"]

    embedding = get_embedding(text)
    if not embedding:
        return False

    scope = _resolve_capture_scope(category)
    point_data = json.dumps({
        "points": [{
            "id": point_id,
            "vector": _point_vector(embedding),
            "payload": {
                "text": text,
                "content": text,
                "category": category,
                "source": source,
                "access_level": access_level,
                "created_at": now,
                "agent": "claude-code",
                # Herkunft (10.11.2026): dieser Weg schrieb bisher gar keine.
                "provenance": prepared["provenance"],
                # H257: fetch_centroids filters on lifecycle_status ==
                # "canonical". Without this field these memories never feed the
                # scope centroids, which breaks the self-organizing feedback
                # loop (parity with the mcp_server.py writer).
                "lifecycle_status": "canonical",
                # Scope (Schreibziel, entschieden 15.11.2026): ein
                # Sitzungsprotokoll in den eigenen Verbund, alles andere in den
                # gemeinsamen Bereich. Kein geratener Themen-Bereich.
                "scope": scope,
                # Kennzeichen aus dem Schreibtor: die Einspielung laesst
                # Zustandswerte am Laengenfilter vorbei (12 statt 40 Zeichen).
                **({"state_value": True} if prepared.get("state_value") else {}),
            }
        }]
    }).encode()

    # Qdrant wants PUT for point creation, not POST. A bare
    # ``urllib.request.Request`` defaults to POST, which this endpoint
    # rejects with 400 "Format error in JSON body: missing field `ids`" —
    # the error the surrounding ``except`` swallowed, so every capture
    # vanished without a word. Verified against Qdrant 1.17: POST -> 400,
    # PUT -> 200. The read paths (``/points/search``, ``/points/scroll``,
    # ``/points`` with an ``ids`` body) do want POST and are left alone.
    #
    # ``wait=true`` matters just as much: without it Qdrant answers 200
    # "acknowledged" and applies the write in the background, so a rejected
    # write (wrong vector shape, missing vector name) is reported as success
    # and the memory silently stays missing. Measured 09.10.2026: a write
    # acknowledged with 200 left the collection at zero points.
    req = urllib.request.Request(
        f"{QDRANT_URL}/collections/{COLLECTION}/points?wait=true",
        data=point_data,
        headers={"Content-Type": "application/json"},
        method="PUT",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            # Bewegungsprotokoll: der Hook schrieb bisher an jeder Zeile
            # vorbei. Genau eine Zeile je angelegtem Punkt, mit Pruefsumme
            # des geschriebenen Inhalts — kein Text.
            _log_movement("write", "ok", text=text, access_level=access_level,
                          target_id=point_id, category=category, scope=scope,
                          after_text=text)
            return True
    except Exception as exc:
        # H254: never drop a memory silently — report to stderr (stdout is the
        # hook/MCP protocol channel and must stay clean).
        print(f"[nexus auto-capture] memory not stored: {exc}", file=sys.stderr)
        return False

def _iter_content_blocks(msg: dict):
    """Yield content blocks from one transcript line.

    Real Claude-Code transcripts nest content under message.content
    (message.role = "user"/"assistant"); legacy/synthetic lines keep it at
    the top level. Both are supported so extraction works either way.
    """
    if not isinstance(msg, dict):
        return
    message = msg.get("message") if isinstance(msg.get("message"), dict) else {}
    content = message.get("content")
    if content is None:
        content = msg.get("content")
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict):
                yield block
    elif isinstance(content, str) and content:
        yield {"type": "text", "text": content}


def extract_facts_from_transcript(transcript_path: str, session_id: str) -> list:
    """Extract notable facts from the conversation transcript.

    Reads the last few messages and looks for:
    - Decisions made
    - Files created/modified
    - Bugs fixed
    - Key learnings
    """
    facts = []
    try:
        with open(transcript_path, 'r') as f:
            lines = f.readlines()

        # Look at last 20 lines for recent activity
        recent = lines[-20:] if len(lines) > 20 else lines

        for line in recent:
            try:
                msg = json.loads(line)
                if not isinstance(msg, dict):
                    continue
                msg_type = msg.get("type", "")
                message = msg.get("message") if isinstance(msg.get("message"), dict) else {}
                mtype = message.get("role") or msg_type

                for block in _iter_content_blocks(msg):
                    btype = block.get("type", "")

                    # Tool results (user-side): look for file operations.
                    if btype == "tool_result" or msg_type == "tool_result":
                        raw = block.get("content", "")
                        if not raw:
                            raw = msg.get("content", "")  # legacy top-level
                        # tool_result content may be a plain string or a
                        # list of dicts with a "text" field — str() of either
                        # is enough for the path scan.
                        content_str = str(raw)
                        if "created" in content_str.lower() or "modified" in content_str.lower():
                            path_match = re.search(r'["\']?(/[^"\']+\.\w+)["\']?', content_str)
                            if path_match:
                                facts.append({
                                    "text": f"File modified in Claude Code session {session_id}: {path_match.group(1)}",
                                    "category": "session"
                                })
                        continue

                    # Assistant text blocks: look for decisions.
                    if btype == "text" and mtype == "assistant":
                        text = block.get("text", "")
                        if not isinstance(text, str):
                            continue
                        if any(kw in text.lower() for kw in ["decided", "chose", "will use", "implemented", "fixed"]):
                            clean = text.strip()[:200]
                            if len(clean) > 20:
                                facts.append({
                                    "text": f"Claude Code session {session_id}: {clean}",
                                    "category": "session"
                                })
            except (json.JSONDecodeError, KeyError):
                # A malformed transcript line is expected (streaming writes cut
                # mid-line). Skipping it is correct and needs no alarm.
                continue
    except Exception as exc:
        # Unexpected trouble while reading the transcript. This USED to be a bare
        # ``pass``, which meant captures silently stopped happening and nothing
        # anywhere said why (measured 09.10.2026). A read failure is a defect.
        print(
            f"[nexus auto-capture] could not read the transcript {transcript_path!r}: "
            f"{exc} — no memories captured from this turn",
            file=sys.stderr,
        )

    return facts[:3]  # Max 3 facts per turn to avoid noise

def main():
    # Read hook input from stdin
    try:
        hook_input = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    session_id = hook_input.get("session_id", "unknown")
    transcript_path = hook_input.get("transcript_path", "")

    if not transcript_path:
        sys.exit(0)

    # Extract facts from transcript
    facts = extract_facts_from_transcript(transcript_path, session_id)

    if not facts:
        sys.exit(0)

    # Store each fact in Qdrant
    stored = 0
    for fact in facts:
        if store_memory(fact["text"], fact["category"]):
            stored += 1

    # Output summary (not injected into context, just logged)
    if stored > 0:
        output = {
            "hookSpecificOutput": {
                "hookEventName": "Stop",
                "additionalContext": f"Nexus: stored {stored} new memor{'y' if stored == 1 else 'ies'} from this turn."
            }
        }
        print(json.dumps(output))

if __name__ == "__main__":
    main()