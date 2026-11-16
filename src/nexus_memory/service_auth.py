"""Per-installation authentication for the local Nexus Memory service.

The service binds loopback only, but loopback is shared by every local
process (and reachable from a browser via DNS rebinding).  Both the
``/embed`` endpoint and the ``/mcp`` Streamable-HTTP endpoint therefore
require a per-installation secret that is generated once, stored with
mode 0600 under the data directory, and read by the native plugin.

Per-agent keys (15.11.2026): that installation secret says *that* a caller may
talk to the service, not *who* it is.  An agent may own a key of its own — its
SHA-256 lives in ``agents.json`` (``key_hash``), the key itself is shown once
by ``python -m nexus_memory.service_auth --issue``.  See ``verify_key`` for the
three outcomes (own key, shared key, reject).

Self-enrolment (15.11.2026): the installation key no longer reads or writes
data at all.  A plugin presents it once to ``POST /enroll`` and receives a key
of its own (``enroll``); from then on only that key is sent.  ``agents.json``
keeps the ``harness`` name, but an entry that already has a key is never
adopted again (16.11.2026): a second installation of the same harness gets its
own entry and id, and the first installation keeps its key.

Kennung und Label (15.11.2026): the id is a short, opaque handle — six
characters from ``ID_ALPHABET``, generated when ``--issue`` runs without
``--id``.  It is deliberately NOT derived from the agent type or the machine:
two Hermes instances on one Mac mini are two agents and get two ids.  ``name``
is the free-form LABEL shown in the dashboard and may repeat; ``--label``
changes it without touching the id.  The movement log keeps both — ``actor``
is always the id, ``actor_label`` the label as it stood when the action
happened.
"""
from __future__ import annotations

import argparse
import contextvars
import hashlib
import hmac
import logging
import os
import re
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from nexus_memory.self_report import data_dir as _data_dir

logger = logging.getLogger(__name__)

# Header name used by both the server and the plugin.  Kept in one place so the
# two sides can never drift out of agreement.
AUTH_HEADER = "x-nexus-service-key"
_KEY_FILE_NAME = "service_auth.key"

# ── Per-agent keys (15.11.2026) ───────────────────────────────────────────
# The installation key answers "may this caller talk to the service at all".
# It cannot answer "who is calling": with one shared key the sender was only
# the ``x-nexus-agent-id`` header, i.e. a claim.  An agent may therefore hold
# its OWN key.  Only the SHA-256 of that key is stored, in the agents registry
# (``key_hash``); the key itself exists exactly once, in the output of
# ``--issue``.  A call authenticated with an own key is attributed to that
# agent and the header is ignored.
KEY_HASH_FIELD = "key_hash"
KEY_ISSUED_FIELD = "key_issued_at"
KEY_HASH_PREFIX = "sha256:"

# Selbst-Einschreibung (15.11.2026): the installation key only ENROLLS a
# plugin and gets it a key of its own; ``POST /enroll`` is the one door left
# for it.  The registry keeps the harness name so an enrolment can find its
# entry — but only an entry WITHOUT a key is adopted (16.11.2026): one that
# already has a key belongs to an installation, and a second enrolment must
# never take that key away.
HARNESS_FIELD = "harness"
ENROLLED_AT_FIELD = "enrolled_at"
ENROLL_COUNT_FIELD = "enroll_count"
AGENT_KEY_DIR = "agent_keys"
#: The dashboard enrols like a plugin (it embeds through the service) but is
#: not an agent: it never becomes the owner and never shows up in the list.
DASHBOARD_HARNESS = "dashboard"
_HARNESS_PATTERN = re.compile(r"[a-z0-9._-]{1,64}")

# What the movement log records about the credential of the CURRENT request.
# ``None`` outside the HTTP service (library, hooks, CLI): no key was
# presented there, so the log line stays silent about it.
KEY_KIND_OWN = "own"
KEY_KIND_SHARED = "shared"

# Same plausibility rule the service applies to a declared caller id: an
# issued id ends up in the registry, the log and the dashboard, so it must not
# carry spaces, newlines or other decoration.
_AGENT_ID_PATTERN = re.compile(r"[a-z0-9._-]{1,64}")

# Neue Kennungen (15.11.2026): sechs Zeichen, ohne die Verwechslungspaare
# l/o (Buchstabe) und 0/1 (Ziffer). 32 Zeichen = 5 Bit je Stelle, 30 Bit
# gesamt; die Kollisionspruefung gegen alle ``id`` und ``formerly`` schliesst
# die Luecke vollstaendig, weil eine erzeugte Kennung sofort registriert wird.
ID_ALPHABET = "abcdefghijkmnpqrstuvwxyz23456789"
AGENT_ID_LENGTH = 6
_LABEL_MAX_CHARS = 64

UNIDENTIFIED_AGENT_ID = "unidentified"

_REQUEST_KEY_KIND: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "nexus_request_key_kind", default=None)


class ServiceAuthError(ValueError):
    """The presented key belongs to no known caller — reject (fail closed)."""


def request_key_kind() -> Optional[str]:
    """``"own"``/``"shared"`` for the CURRENT request, ``None`` elsewhere."""
    return _REQUEST_KEY_KIND.get()


def set_request_key_kind(kind: Optional[str]) -> contextvars.Token:
    """Mark the current request as authenticated by an own or the shared key."""
    return _REQUEST_KEY_KIND.set(kind)


def reset_request_key_kind(token: contextvars.Token) -> None:
    """Undo ``set_request_key_kind`` (one request, one scope)."""
    _REQUEST_KEY_KIND.reset(token)


def hash_key(key: str) -> str:
    """The storable form of a key: ``sha256:<hex>``, never the key itself."""
    digest = hashlib.sha256(key.encode("utf-8", "surrogateescape")).hexdigest()
    return KEY_HASH_PREFIX + digest


def _key_path() -> Path:
    return _data_dir() / _KEY_FILE_NAME


def load_service_key() -> str:
    """Read the service authentication key.

    Environment variable ``NEXUS_SERVICE_AUTH_KEY`` wins for test/operator
    overrides.  Otherwise the key file is read.  If the file is missing an
    empty string is returned: callers that do not own key generation (the
    plugin) must not create it.
    """
    env = os.environ.get("NEXUS_SERVICE_AUTH_KEY", "").strip()
    if env:
        return env
    path = _key_path()
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8").strip()
    except Exception as exc:
        logger.warning("service-auth: failed to read %s: %s", path, exc)
        return ""


def ensure_service_key() -> str:
    """Return the service authentication key, generating it if necessary.

    The key is written to ``<data-dir>/service_auth.key`` with mode 0600.
    ``NEXUS_SERVICE_AUTH_KEY`` is honoured as an override and the file is
    left untouched in that case.

    Fail-closed: an existing key that is empty or unreadable is NEVER silently
    replaced.  Returning an empty key lets every request be rejected with 401,
    which is visible and safe; a silently rewritten key would strand already
    running consumers with a secret the service no longer accepts.
    """
    env = os.environ.get("NEXUS_SERVICE_AUTH_KEY", "").strip()
    if env:
        return env
    path = _key_path()
    if path.exists():
        try:
            content = path.read_text(encoding="utf-8").strip()
        except Exception as exc:
            logger.error(
                "service-auth: existing key at %s is unreadable; refusing to overwrite "
                "(service will reject requests with 401 until the file is fixed): %s",
                path, exc)
            return ""
        if content:
            return content
        logger.error(
            "service-auth: existing key at %s is empty; refusing to overwrite "
            "(service will reject requests with 401 until a key is set)",
            path)
        return ""

    return _write_new_key(path)


def _write_new_key(path: Path) -> str:
    """Create a fresh key and persist it with restrictive permissions.

    Race-safe: ``O_CREAT | O_EXCL`` fails if another process created the file
    first.  In that case the existing file is read instead of overwritten,
    so two concurrent starters can never end up with different keys.
    """
    key = secrets.token_urlsafe(32)
    _data_dir().mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        # Another starter won the race; adopt its key.
        try:
            return path.read_text(encoding="utf-8").strip() or ""
        except Exception as exc:
            logger.error(
                "service-auth: race-loser could not read %s after it appeared: %s",
                path, exc)
            return ""
    except OSError as exc:
        logger.error("service-auth: failed to create %s: %s", path, exc)
        return key  # in-memory fallback: the process can still run, but loud
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(key)
    except Exception:
        os.close(fd)
        raise
    logger.info("service-auth: installed per-installation key at %s", path)
    return key


# ── Per-agent keys: verify ────────────────────────────────────────────────


def _agent_entries() -> list[dict]:
    """Registry entries that could carry an own key.

    Fail closed: an unreadable or corrupt registry yields NO entries, so
    nobody authenticates with an own key until the file is fixed.  The shared
    key is checked before this, so existing connections keep working.
    """
    try:
        from nexus_memory import agent_detect

        registry = agent_detect.load_agents_registry()
    except Exception as exc:  # noqa: BLE001 — fail closed, never 500
        logger.error(
            "service-auth: agents registry unreadable; per-agent keys are "
            "refused until it is fixed: %s", exc)
        return []
    agents = registry.get("agents") if isinstance(registry, dict) else None
    if not isinstance(agents, list):
        return []
    return [entry for entry in agents if isinstance(entry, dict)]


def verify_key(key: str, declared: str = "") -> tuple[str, bool]:
    """Resolve a presented service key to ``(agent_id, is_legacy)``.

    * the installation key  → ``(declared or "unidentified", True)``: the
      caller still names itself in the header, exactly as before. A header
      that is a former name of a renamed agent resolves to that agent's
      CURRENT id (``agent_detect.resolve_former_name``) — the alias applies
      while reading the sender, never as an id of its own.
    * an agent's own key    → ``(that agent's id, False)``: the header is
      ignored, even when it claims something else. A key issued before a
      rename travels with the entry and therefore yields the new id too.
    * anything else         → :class:`ServiceAuthError` (the caller answers
      with 401, as today).

    Comparison is time-constant (``hmac.compare_digest``) on bytes: header
    values are arbitrary bytes and a string comparison would raise on
    non-ASCII instead of simply not matching.
    """
    presented = key or ""
    if not presented:
        raise ServiceAuthError("no service key presented")
    presented_bytes = presented.encode("utf-8", "surrogateescape")

    shared = load_service_key()
    if shared and hmac.compare_digest(
            presented_bytes, shared.encode("utf-8", "surrogateescape")):
        name = (declared or "").strip()
        if not name:
            return (UNIDENTIFIED_AGENT_ID, True)
        # Uebergangs-Alias (Punkt 4a): der alte Name eines umbenannten
        # Agenten wird zur neuen Kennung. Reihenfolge und Fail-open stehen
        # in ``agent_detect.resolve_former_name``.
        from nexus_memory.agent_detect import resolve_former_name

        return (resolve_former_name(name), True)

    wanted = hash_key(presented).encode("ascii")
    for agent in _agent_entries():
        stored = agent.get(KEY_HASH_FIELD)
        if not isinstance(stored, str) or not stored.strip():
            continue
        if hmac.compare_digest(wanted, stored.strip().encode("utf-8", "replace")):
            agent_id = agent.get("id")
            if isinstance(agent_id, str) and agent_id.strip():
                return (agent_id.strip(), False)
    raise ServiceAuthError("unknown service key")


# ── Per-agent keys: issue / list / revoke ─────────────────────────────────


def issue_key(agent_id: str, name: str = "") -> str:
    """Create a key for ``agent_id``, store its hash, return the key ONCE.

    The registry entry is created when it does not exist yet; an existing
    entry keeps its name, trust level and stats (this command hands out a
    credential, it does not re-register the agent).  Issuing again replaces
    the hash, which invalidates the previous key.

    A ``formerly`` name (Punkt 4c) is resolved to the entry's current id
    BEFORE the lookup: the key then travels with the renamed agent and no
    second entry is created under the old name.
    """
    if not _AGENT_ID_PATTERN.fullmatch(agent_id or ""):
        raise ValueError(
            f"invalid agent id {agent_id!r}: expected [a-z0-9._-], max 64 characters")
    key = secrets.token_urlsafe(32)
    digest = hash_key(key)
    issued = datetime.now(timezone.utc).isoformat()

    # Registry owner is ``agent_detect``: its lock and atomic save are used
    # here so a parallel stats update cannot be lost.
    from nexus_memory import agent_detect

    with agent_detect._registry_lock():
        registry = agent_detect._load_registry_unlocked()
        agents = registry.setdefault("agents", [])
        agent_id = agent_detect._resolve_former_name_in(registry, agent_id)
        entry = next((a for a in agents if a.get("id") == agent_id), None)
        if entry is None:
            entry = {
                "id": agent_id,
                "name": name or agent_id,
                "icon": getattr(agent_detect, "_DEFAULT_AGENT_ICON", "\U0001f50c"),
                "trust_level": "public",
                "install_type": "unknown",
                "config_dir": None,
                "connected_at": issued,
                "last_seen": issued,
                "reads": 0,
                "writes": 0,
            }
            agent_detect.annotate_host(entry, host_type="local")
            agents.append(entry)
        entry[KEY_HASH_FIELD] = digest
        entry[KEY_ISSUED_FIELD] = issued
        agent_detect._save_registry_unlocked(registry)
    return key


def _taken_agent_names() -> set[str]:
    """Alle Namen, die eine neue Kennung nicht tragen darf.

    ``id`` UND ``formerly``: ein alter Name bleibt als Alias gueltig, eine
    neue Kennung mit demselben Wort haette zwei Bedeutungen. Ein unlesbares
    Verzeichnis ist ein Abbruch, keine leere Menge — sonst wuerde eine
    Kennung vergeben, deren Einmaligkeit niemand geprueft hat.
    """
    from nexus_memory import agent_detect

    try:
        registry = agent_detect.load_agents_registry()
    except Exception as exc:  # noqa: BLE001 — nicht raten, abbrechen
        raise ValueError(
            f"agents registry unreadable, cannot guarantee a unique id: {exc}"
        ) from exc
    taken: set[str] = set()
    agents = registry.get("agents") if isinstance(registry, dict) else None
    for entry in agents or []:
        if not isinstance(entry, dict):
            continue
        entry_id = entry.get("id")
        if isinstance(entry_id, str) and entry_id.strip():
            taken.add(entry_id.strip())
        taken.update(agent_detect._former_names(entry))
    return taken


def generate_agent_id() -> str:
    """Eine freie Kennung: sechs Zeichen aus :data:`ID_ALPHABET`.

    Der Zufall ist der Erzeuger, nicht Typ oder Rechner — zwei Hermes auf
    demselben Mac mini sind zwei Agenten und bekommen zwei Kennungen.
    """
    return _free_id_from(_taken_agent_names())


def _free_id_from(taken: set[str]) -> str:
    """Eine Kennung, die in ``taken`` nicht vorkommt (Aufrufer haelt das Lock)."""
    for _ in range(100):
        candidate = "".join(
            secrets.choice(ID_ALPHABET) for _ in range(AGENT_ID_LENGTH))
        if candidate not in taken:
            return candidate
    raise ValueError("no free agent id found — registry is unexpectedly full")


def agent_key_path(harness: str) -> Path:
    """Wo ein Harness seinen EIGENEN Schluessel ablegt (nur der Pfad).

    ``<datenverzeichnis>/agent_keys/<harness>.key``. Der Name kommt aus dem
    Netz, deshalb dieselbe Plausibilitaetsregel wie fuer Kennungen: kein
    Pfadtrenner, kein ``..``, nichts, was aus dem Verzeichnis herausfuehrt.
    """
    name = (harness or "").strip()
    if not _HARNESS_PATTERN.fullmatch(name):
        raise ValueError(f"invalid harness name {harness!r}")
    return _data_dir() / AGENT_KEY_DIR / f"{name}.key"


def _normalized_agent_name(value) -> str:
    """Ein Agentenname, vergleichbar gemacht: klein, ohne Leerzeichen/Bindestriche.

    ``"Hermes Agent"`` und ``"hermes-agent"`` sind derselbe Name; die
    Normalisierung ist nur fuer den Vergleich da, nie fuer einen Schreibvorgang.
    """
    if not isinstance(value, str):
        return ""
    return re.sub(r"[\s-]+", "", value.strip().lower())


def _is_unadoptable(entry: dict) -> bool:
    """``unidentified`` und ``dashboard`` werden nie uebernommen.

    ``unidentified`` ist kein Agent, sondern der anonyme Aufrufer; das
    Dashboard ist das Fenster auf die Agenten. Wer sich unter diesem Namen
    einschreibt, bekommt einen eigenen Eintrag — sonst wuerde die Anmeldung
    eines Plugins den anonymen Platz oder das Dashboard umdeuten.

    Ein Eintrag mit EIGENEM Harness ist ein echter Agent, auch wenn sein
    Label zufaellig "dashboard" lautet — nur Eintraege aus der Zeit vor
    ``harness`` werden ueber ihren Namen erkannt.
    """
    if entry.get("id") == UNIDENTIFIED_AGENT_ID:
        return True
    harness = entry.get(HARNESS_FIELD)
    if harness == DASHBOARD_HARNESS:
        return True
    if isinstance(harness, str) and harness.strip():
        return False
    return _normalized_agent_name(entry.get("name")) in {
        UNIDENTIFIED_AGENT_ID, DASHBOARD_HARNESS}


def _newest_entry(entries: list[dict]) -> Optional[dict]:
    """Der Eintrag mit dem juengsten ``last_seen``; fehlend = aeltester.

    ISO-Strings sind lexikografisch vergleichbar, deshalb genuegt ``max``.
    Fehlt ``last_seen``, zaehlt der leere String — er liegt vor jedem Datum.
    """
    if not entries:
        return None
    return max(entries, key=lambda e: str(e.get("last_seen") or ""))


def _has_own_key(entry: dict) -> bool:
    """True, wenn der Eintrag schon einen eigenen Schluessel traegt.

    Ein solcher Eintrag gehoert einer angemeldeten Installation. Fuer eine
    Anmeldung ueber den Installationsschluessel ist er kein Kandidat mehr —
    sonst wuerde eine zweite Installation desselben Harness (Windows neben
    dem Mac) ihm den Schluessel ersetzen und die erste aussperren.
    """
    stored = entry.get(KEY_HASH_FIELD)
    return isinstance(stored, str) and bool(stored.strip())


def _find_enroll_target(agents: list, name: str) -> Optional[dict]:
    """Der bestehende Eintrag, den ``name`` uebernimmt — in drei Stufen.

    Uebernommen wird nur ein Eintrag OHNE eigenen Schluessel: ein gesetztes
    ``key_hash`` heisst, dass diese Installation schon eine Identitaet hat.
    Solche Eintraege werden in ALLEN drei Stufen uebersprungen; findet sich
    kein schluesselloser Kandidat, entsteht ein neuer Eintrag (gleicher Name
    erlaubt, eigene Kennung).

    1. ``harness`` == ``name`` (der normale Fall: das Plugin hat sich schon
       einmal eingeschrieben),
    2. ``name`` steht in ``formerly`` (ein umbenannter Agent behaelt seinen
       Platz, statt als Waise stehenzubleiben),
    3. der normalisierte ``name`` gleicht dem normalisierten ``name``-Feld
       (Eintraege aus der Zeit vor ``harness``: ``"Hermes Agent"``).

    In Stufe 2 und 3 gewinnt der juengste Eintrag: bei doppelten Namen (zwei
    "DSH") ist der zuletzt gesehene der lebende. Stufe 1 nimmt den ersten
    Treffer — zwei Eintraege mit demselben ``harness`` waeren schon vorher
    ein Fehler im Register.
    """
    from nexus_memory import agent_detect as _agent_detect

    candidates = [a for a in agents
                  if isinstance(a, dict) and not _is_unadoptable(a)
                  and not _has_own_key(a)]

    for entry in candidates:
        if entry.get(HARNESS_FIELD) == name:
            return entry

    by_former = [e for e in candidates
                 if name in _agent_detect._former_names(e)]
    chosen = _newest_entry(by_former)
    if chosen is not None:
        return chosen

    norm = _normalized_agent_name(name)
    if not norm:
        return None
    return _newest_entry(
        [e for e in candidates
         if _normalized_agent_name(e.get("name")) == norm])


def enroll(harness: str, label: str = "") -> dict:
    """Melde einen Harness an und stelle ihm einen eigenen Schluessel aus.

    Der EINE Weg, auf dem ein Plugin aus dem Installationsschluessel
    herauskommt. Gesucht wird nach ``harness``, dann in ``formerly``, dann im
    normalisierten ``name`` (siehe :func:`_find_enroll_target`): uebernommen
    wird NUR ein Eintrag, der noch keinen Schluessel hat (``created: false``);
    sein ``key_hash`` wird gesetzt. Ein Eintrag MIT Schluessel bleibt
    unberuehrt — fuer ihn entsteht ein neuer Eintrag mit eigener Kennung und
    Standardrolle. Wer seinen Schluessel verliert, bekommt deshalb einen neuen
    Eintrag statt den alten zurueck: eine zweite Anmeldung darf eine bereits
    angemeldete Installation nicht verdraengen. Das ist gewollt.

    Der ERSTE Eintrag im Register ist der Eigentuemer (``admin`` + ``private``),
    jeder weitere startet als ``know`` + ``trusted``. Der Loopback-Zwang lebt
    in der Middleware, nicht hier: diese Funktion ist auch ohne HTTP aufrufbar.

    Der Klartextschluessel steht nur im Rueckgabewert; gespeichert wird sein
    SHA-256.
    """
    from nexus_memory import agent_detect as _ad

    name = (harness or "").strip()
    if not _HARNESS_PATTERN.fullmatch(name):
        raise ValueError(
            f"invalid harness name {harness!r}: expected [a-z0-9._-], "
            f"max 64 characters")
    label_text = " ".join((label or "").split())[:_LABEL_MAX_CHARS]

    key = secrets.token_urlsafe(32)
    digest = hash_key(key)
    now = datetime.now(timezone.utc).isoformat()

    with _ad._registry_lock():
        registry = _ad._load_registry_unlocked()
        agents = registry.setdefault("agents", [])
        entry = _find_enroll_target(agents, name)
        created = entry is None
        if created:
            taken: set[str] = set()
            for other in agents:
                if not isinstance(other, dict):
                    continue
                other_id = other.get("id")
                if isinstance(other_id, str) and other_id.strip():
                    taken.add(other_id.strip())
                taken.update(_ad._former_names(other))
            agent_id = _free_id_from(taken)
            # Eigentuemer nur, solange niemand sonst im Register steht. Ein
            # ``unidentified``-Eintrag ist kein Agent und zaehlt nicht — und
            # das Dashboard ist kein Agent, sondern das Fenster auf sie: es
            # bekommt einen Schluessel, aber nie den Eigentuemer-Platz.
            owner = name != DASHBOARD_HARNESS and not [
                a for a in agents
                if isinstance(a, dict)
                and a.get("id") != UNIDENTIFIED_AGENT_ID
                and a.get(HARNESS_FIELD) != DASHBOARD_HARNESS]
            entry = {
                "id": agent_id,
                "name": label_text or name,
                "icon": getattr(_ad, "_DEFAULT_AGENT_ICON", "\U0001f50c"),
                "trust_level": "private" if owner else "trusted",
                "install_type": "plugin",
                "config_dir": None,
                "connected_at": now,
                "last_seen": now,
                "reads": 0,
                "writes": 0,
                HARNESS_FIELD: name,
                _ad.ROLE_FIELD: _ad.ROLE_ADMIN if owner else _ad.ROLE_KNOW,
            }
            _ad.annotate_host(entry, host_type="local")
            agents.append(entry)
        else:
            # Uebernommener Eintrag: der Harness-Name kommt dazu, damit die
            # naechste Einschreibung ihn in Stufe 1 findet. ``role`` und
            # ``trust_level`` bleiben unangetastet — sie gehoeren dem Nutzer,
            # der sie im Dashboard gesetzt hat.
            entry[HARNESS_FIELD] = name
            owner = entry.get(_ad.ROLE_FIELD) == _ad.ROLE_ADMIN
        entry[KEY_HASH_FIELD] = digest
        entry[KEY_ISSUED_FIELD] = now
        entry[ENROLLED_AT_FIELD] = now
        entry[ENROLL_COUNT_FIELD] = int(entry.get(ENROLL_COUNT_FIELD) or 0) + 1
        agent_id = entry.get("id")
        _ad._save_registry_unlocked(registry)

    return {"agent_id": agent_id, "key": key, "is_owner": owner,
            "created": created}


def revoke_key(agent_id: str) -> bool:
    """Drop the agent's own key; it falls back to the shared key.

    Returns True when a key was removed, False when the agent had none.
    """
    from nexus_memory import agent_detect

    with agent_detect._registry_lock():
        registry = agent_detect._load_registry_unlocked()
        for entry in registry.get("agents", []):
            if not isinstance(entry, dict) or entry.get("id") != agent_id:
                continue
            removed = entry.pop(KEY_HASH_FIELD, None) is not None
            entry.pop(KEY_ISSUED_FIELD, None)
            if removed:
                agent_detect._save_registry_unlocked(registry)
            return removed
    return False


def rename_agent(old_id: str, new_id: str) -> dict:
    """Rename a registered agent, keeping the old name as ``formerly``.

    Touches the REGISTRY ONLY: ``id`` changes, the old name is appended to
    ``formerly``, and everything else — own key, role, trust level, stats,
    ``connected_at`` — stays with the entry. Points in the collection are NOT
    touched; that is a separate migration.

    Refusals (nothing is written, the file stays as it was):
      * ``new_id`` is not a usable id,
      * ``old_id`` is unknown,
      * ``new_id`` already exists — two entries must never claim one id,
      * ``old_id`` is already the ``formerly`` of another entry — the alias
        would become ambiguous, which the loader rejects.
    """
    from nexus_memory import agent_detect as _ad

    old = (old_id or "").strip()
    new = (new_id or "").strip()
    if not _AGENT_ID_PATTERN.fullmatch(new):
        return {"error": f"invalid agent id {new_id!r}: expected [a-z0-9._-], "
                         f"max 64 characters"}
    if old == new:
        return {"error": f"{old!r} is already its own name"}

    with _ad._registry_lock():
        registry = _ad._load_registry_unlocked()
        agents = registry.setdefault("agents", [])
        entry = next((a for a in agents
                      if isinstance(a, dict) and a.get("id") == old), None)
        if entry is None:
            return {"error": f"unknown agent id {old!r}"}
        if any(isinstance(a, dict) and a.get("id") == new for a in agents):
            return {"error": f"agent id {new!r} already exists"}
        for other in agents:
            if isinstance(other, dict) and other is not entry \
                    and old in _ad._former_names(other):
                return {"error": (
                    f"{old!r} is already a former name of "
                    f"{other.get('id')!r} — renaming would make it ambiguous")}

        entry["id"] = new
        names = _ad._former_names(entry)
        if old not in names:
            names.append(old)
        entry[_ad.FORMERLY_FIELD] = names
        # Vor dem Schreiben pruefen: was der Loader ablehnt, darf nie entstehen.
        _ad._validate_formerly(registry)
        _ad._save_registry_unlocked(registry)
    return {"status": "renamed", "from": old, "to": new,
            "formerly": names}


def set_agent_label(agent_id: str, label: str) -> dict:
    """Setze das Label (``name``) eines Agenten — die Kennung bleibt.

    Das Label ist frei waehlbar und darf doppelt vorkommen: zwei „Hermes
    Agent" sind erlaubt, unterschieden werden sie durch ``id``. Die Kennung
    ist der Schluessel des Eintrags; sie wird hier nicht angefasst.

    Ein unbekannter Agent wird abgewiesen, statt still angelegt zu werden:
    ein Label ohne Agenten waere ein Eintrag, den niemand angefordert hat.
    """
    from nexus_memory import agent_detect as _ad

    wanted = (agent_id or "").strip()
    text = " ".join((label or "").split())
    if not text:
        return {"error": "label must not be empty"}
    if len(text) > _LABEL_MAX_CHARS:
        return {"error": f"label too long: {len(text)} characters "
                         f"(max {_LABEL_MAX_CHARS})"}

    with _ad._registry_lock():
        registry = _ad._load_registry_unlocked()
        entry = next((a for a in registry.get("agents", [])
                      if isinstance(a, dict) and a.get("id") == wanted), None)
        if entry is None:
            return {"error": f"unknown agent id {wanted!r}"}
        previous = entry.get("name")
        entry["name"] = text
        _ad._save_registry_unlocked(registry)
    return {"status": "labeled", "id": wanted, "label": text,
            "previous": previous if isinstance(previous, str) else ""}


def list_agent_keys() -> list[dict]:
    """One row per registered agent: id, name, role, formerly, key yes/no, issued when."""
    from nexus_memory.agent_detect import (
        AGENT_ROLES, DEFAULT_AGENT_ROLE, FORMERLY_FIELD, ROLE_FIELD, _former_names)

    rows = []
    for entry in _agent_entries():
        stored = entry.get(KEY_HASH_FIELD)
        role = entry.get(ROLE_FIELD)
        rows.append({
            "id": str(entry.get("id") or ""),
            "name": str(entry.get("name") or ""),
            # Fail-closed, wie im Dienst: was nicht dasteht oder unbekannt ist,
            # ist ``know``. Die Liste soll zeigen, was WIRKLICH gilt.
            "role": role if role in AGENT_ROLES else DEFAULT_AGENT_ROLE,
            "formerly": _former_names(entry),
            "has_key": isinstance(stored, str) and bool(stored.strip()),
            "issued_at": str(entry.get(KEY_ISSUED_FIELD) or ""),
        })
    return rows


def main(argv: Optional[list[str]] = None) -> int:
    """CLI: ``--issue``/``--revoke``/``--role``/``--label``/``--rename``/``--list``."""
    from nexus_memory.agent_detect import AGENT_ROLES, set_agent_role

    parser = argparse.ArgumentParser(
        prog="python -m nexus_memory.service_auth",
        description="Eigene Schluessel und Rollen je Agent verwalten "
                    "(vom Schluessel wird nur der SHA-256 gespeichert).")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--issue", action="store_true",
                        help="Schluessel fuer einen Agenten ausstellen; ohne --id "
                             "wird eine neue sechsstellige Kennung erzeugt")
    action.add_argument("--list", action="store_true",
                        help="Agenten, ihre Rolle und ihren Schluesselstatus zeigen")
    action.add_argument("--revoke", action="store_true",
                        help="eigenen Schluessel eines Agenten entfernen")
    action.add_argument("--role", action="store_true",
                        help="Rolle eines Agenten setzen (mit --set)")
    action.add_argument("--rename", action="store_true",
                        help="Agenten umbenennen (mit --from und --to); der alte "
                             "Name bleibt als Uebergang gueltig")
    action.add_argument("--label", action="store_true",
                        help="Label eines Agenten setzen (mit --id und --set); "
                             "die Kennung bleibt unveraendert")
    parser.add_argument("--id", default="", help="Kennung des Agenten")
    parser.add_argument("--from", dest="old_id", default="",
                        help="bisherige Kennung (zu --rename)")
    parser.add_argument("--to", dest="new_id", default="",
                        help="neue Kennung (zu --rename)")
    parser.add_argument("--name", default="",
                        help="Label des Agenten (nur beim Anlegen eines neuen Agenten)")
    # ``--set`` traegt je nach Aktion etwas anderes: eine Rolle (--role) oder
    # ein freies Label (--label). Die Pruefung passiert deshalb pro Zweig,
    # nicht in argparse — ein Label ist kein Rollenname.
    parser.add_argument("--set", dest="set_value", default="", metavar="WERT",
                        help="admin | work | know (zu --role) oder das Label "
                             "(zu --label)")
    args = parser.parse_args(argv)

    if args.list:
        try:
            rows = list_agent_keys()
        except ValueError as exc:
            print(f"service_auth: {exc}", file=sys.stderr)
            return 2
        if not rows:
            print("Keine Agenten im Verzeichnis.")
            return 0
        print(f"{'KENNUNG':<24}{'LABEL':<24}{'ROLLE':<8}"
              f"{'FRUEHER':<20}{'EIGENER SCHLUESSEL':<20}AUSGESTELLT")
        for row in rows:
            own = "ja" if row["has_key"] else "nein"
            former = ",".join(row["formerly"]) or "-"
            print(f"{row['id']:<24}{row['name'][:22]:<24}{row['role']:<8}"
                  f"{former[:18]:<20}{own:<20}{row['issued_at'] or '-'}")
        return 0

    if args.role:
        if not args.id:
            parser.error("--role braucht --id")
        if args.set_value not in AGENT_ROLES:
            parser.error("--role braucht --set admin|work|know")
        result = set_agent_role(args.id, args.set_value)
        if "error" in result:
            print(f"service_auth: {result['error']}", file=sys.stderr)
            return 2
        # Bewegungsprotokoll: eine Rollenaenderung ist eine Aenderung an den
        # Rechten — sie gehoert in dieselbe Spur wie Schreib- und Loeschvorgaenge.
        from nexus_memory import service_log

        service_log.record(
            "role_change", "ok", target_id=args.id,
            detail=f"role {result.get('role', args.set_value)}",
            via="cli", actor=os.environ.get("NEXUS_AGENT_ID") or None,
        )
        print(f"Rolle von {args.id!r}: {result.get('role', args.set_value)} "
              f"({result.get('status', 'updated')}).")
        return 0

    if args.label:
        if not args.id:
            parser.error("--label braucht --id")
        if not args.set_value.strip():
            parser.error("--label braucht --set <Label>")
        result = set_agent_label(args.id, args.set_value)
        if "error" in result:
            print(f"service_auth: {result['error']}", file=sys.stderr)
            return 2
        # Wie die Umbenennung aendert auch das Label, WIE ein Agent in der
        # Historie heisst — dieselbe Spur.
        from nexus_memory import service_log

        service_log.record(
            "role_change", "ok", target_id=result["id"],
            reason=f"label {result['previous'] or '?'} to {result['label']}".strip(),
            via="cli", actor=os.environ.get("NEXUS_AGENT_ID") or None,
        )
        print(f"Label von {result['id']!r}: {result['label']!r} "
              f"(die Kennung bleibt).")
        return 0

    if args.rename:
        if not args.old_id or not args.new_id:
            parser.error("--rename braucht --from und --to")
        try:
            result = rename_agent(args.old_id, args.new_id)
        except ValueError as exc:
            print(f"service_auth: {exc}", file=sys.stderr)
            return 2
        if "error" in result:
            print(f"service_auth: {result['error']}", file=sys.stderr)
            return 2
        # Bewegungsprotokoll: die Umbenennung aendert, wem Protokolle gehoeren —
        # sie gehoert in dieselbe Spur wie Rollen- und Schluesselwechsel.
        from nexus_memory import service_log

        service_log.record(
            "role_change", "ok", target_id=result["to"],
            reason=f"rename {result['from']} to {result['to']}",
            via="cli", actor=os.environ.get("NEXUS_AGENT_ID") or None,
        )
        print(f"{result['from']!r} heisst jetzt {result['to']!r}. "
              f"Der alte Name bleibt als Uebergang gueltig "
              f"({', '.join(result['formerly'])}); die Sammlung wurde NICHT angefasst.")
        return 0

    if args.issue:
        # Ohne ``--id`` erzeugt der Dienst die Kennung selbst: sechs Zeichen,
        # frei von Typ und Rechner, geprueft gegen alle ``id`` und ``formerly``.
        agent_id = args.id.strip()
        if not agent_id:
            try:
                agent_id = generate_agent_id()
            except ValueError as exc:
                print(f"service_auth: {exc}", file=sys.stderr)
                return 2
        try:
            key = issue_key(agent_id, args.name)
        except ValueError as exc:
            print(f"service_auth: {exc}", file=sys.stderr)
            return 2
        # Das tatsaechliche Label: bei einem bestehenden Eintrag bleibt sein
        # Name stehen, auch wenn hier keiner angegeben wurde.
        row = next((r for r in list_agent_keys() if r["id"] == agent_id), {})
        label = row.get("name") or agent_id
        print(f"Kennung: {agent_id}\n"
              f"Label:   {label}\n"
              f"Schluessel ausgestellt. Er wird JETZT EINMAL angezeigt und ist "
              f"nicht wiederholbar;\ngespeichert ist nur sein SHA-256 in "
              f"agents.json.\n")
        print(key)
        return 0

    if not args.id:
        parser.error("--revoke braucht --id")

    removed = revoke_key(args.id)
    if removed:
        print(f"Schluessel von {args.id!r} entfernt — der Agent faellt auf den "
              f"globalen Schluessel zurueck.")
        return 0
    print(f"{args.id!r} hatte keinen eigenen Schluessel.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
