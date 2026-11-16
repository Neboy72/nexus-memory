"""The embedding path shared by every Claude Code hook.

One rule, so all four hooks cannot drift apart:

* **automatic (default)** — the hook asks the local Nexus service
  (``NEXUS_SERVE_URL``, default ``http://127.0.0.1:9122``) for the vector. That
  service runs the engine with its **local HuggingFace** model, so this plugin
  works on a machine with nothing installed: no Ollama, no API key, and every
  agent in the house shares one vector space.
* **explicit** — ``NEXUS_EMBEDDING_PROVIDER=ollama|openai|google|jina``
  keeps working exactly as before, for anyone who wants that.

Why not embed here instead? These hooks are started with ``python3`` by the
editor, not by us: we cannot promise a Python version, a virtualenv or a local
model on that interpreter. The service is the one place that can.

Every failure path returns ``None`` and says why on stderr — a hook that cannot
embed skips its work loudly instead of storing or recalling nothing in silence.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_SERVE_URL = "http://127.0.0.1:9122"

# Header name MUST match ``nexus_memory.service_auth.AUTH_HEADER``.
# The scripts run under the system python3 and must not import nexus_memory.
_SERVICE_AUTH_HEADER = "x-nexus-service-key"
_SERVICE_AUTH_KEY_FILE = "service_auth.key"
# Self-enrolment (15.11.2026): the installation key only ENROLS this hook
# (POST /enroll) and gets it a key of its own; only that key is sent after.
# The key names the agent, so no ``x-nexus-agent-id`` header is sent any more.
HARNESS = "claude-code"
_AGENT_KEY_DIR = "agent_keys"
#: This plugin IS the Claude Code plugin — a missing NEXUS_AGENT_ID must name
#: it, not leave the line anonymous.
DEFAULT_AGENT_ID = "claude-code"


def agent_id() -> str:
    """Who is calling the service: ``NEXUS_AGENT_ID``, else ``claude-code``."""
    return (os.environ.get("NEXUS_AGENT_ID") or "").strip() or DEFAULT_AGENT_ID


def _data_dir() -> Path:
    env_dir = os.environ.get("NEXUS_DATA_DIR", "").strip()
    return Path(os.path.expanduser(env_dir)) if env_dir else Path.home() / ".nexus-memory"


def _install_key() -> str:
    """The per-installation key — used for ONE thing: ``POST /enroll``.

    Mirrors ``nexus_memory.service_auth.load_service_key()``: the environment
    variable ``NEXUS_SERVICE_AUTH_KEY`` wins, otherwise the key file in the
    data directory is read. Missing/unreadable key -> empty string.
    """
    env = os.environ.get("NEXUS_SERVICE_AUTH_KEY", "").strip()
    if env:
        return env
    try:
        return (_data_dir() / _SERVICE_AUTH_KEY_FILE).read_text(
            encoding="utf-8").strip()
    except Exception:
        return ""


def _agent_key_path(harness: str = HARNESS) -> Path:
    """``<data-dir>/agent_keys/<harness>.key`` — this hook's OWN key."""
    return _data_dir() / _AGENT_KEY_DIR / f"{harness}.key"


def _read_agent_key(harness: str = HARNESS) -> str:
    try:
        return _agent_key_path(harness).read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def _write_agent_key(key: str, harness: str = HARNESS) -> None:
    path = _agent_key_path(harness)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".key.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(key)
    os.replace(tmp, path)


def _enroll() -> str:
    """Ask the service for a key of our own; "" when that fails (never raises)."""
    install = _install_key()
    if not install:
        print(
            "[nexus] no installation key for /enroll "
            "(set NEXUS_SERVICE_AUTH_KEY or make "
            f"{_data_dir() / _SERVICE_AUTH_KEY_FILE} readable)",
            file=sys.stderr,
        )
        return ""
    try:
        request = urllib.request.Request(
            f"{serve_url()}/enroll",
            data=json.dumps({"harness": HARNESS,
                             "label": "Claude Code"}).encode(),
            headers={"Content-Type": "application/json",
                     _SERVICE_AUTH_HEADER: install},
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.loads(response.read())
        key = payload.get("key")
        if not isinstance(key, str) or not key.strip():
            return ""
        _write_agent_key(key.strip())
        return key.strip()
    except Exception as exc:
        print(f"[nexus] enrolment against {serve_url()} failed: {exc}",
              file=sys.stderr)
        return ""


def _service_auth_key() -> str:
    """This hook's own key — read once, enrolled once when missing."""
    return _read_agent_key() or _enroll()

# Providers that mean "ask the local Nexus service". "auto" is the default the
# hooks carry when the user configured nothing.
SERVE_PROVIDERS = ("", "auto", "serve", "nexus", "local-service")


def serve_url() -> str:
    """Base URL of the local Nexus service (env-overridable)."""
    return (os.getenv("NEXUS_SERVE_URL") or DEFAULT_SERVE_URL).rstrip("/")


def provider() -> str:
    """The configured provider, normalised. Defaults to ``auto``."""
    return (os.getenv("NEXUS_EMBEDDING_PROVIDER") or "auto").strip().lower()


def is_serve_provider(value: str | None = None) -> bool:
    return (value if value is not None else provider()) in SERVE_PROVIDERS


def embed_via_serve(text: str, is_query: bool = True) -> "list | None":
    """POST /embed on the local service. Returns None (and says why) on failure.

    ``is_query`` picks the query/document side of an asymmetric model — the
    recall path embeds queries, the capture path embeds documents, exactly as
    the engine does internally.

    Only this hook's own agent key is sent. A 401 means the key was revoked or
    replaced: the hook enrols once more and repeats the call once.
    """
    url = f"{serve_url()}/embed"
    for attempt in (0, 1):
        key = _service_auth_key()
        if not key:
            print(
                f"[nexus] no agent key for {url} — enrolment needs an "
                "installation key (NEXUS_SERVICE_AUTH_KEY or the service's "
                "service_auth.key)",
                file=sys.stderr,
            )
            return None
        try:
            req = urllib.request.Request(
                url,
                data=json.dumps({"text": text, "is_query": is_query}).encode(),
                headers={"Content-Type": "application/json",
                         _SERVICE_AUTH_HEADER: key},
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                payload = json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            if exc.code == 401 and attempt == 0:
                try:
                    _agent_key_path().unlink()
                except Exception:
                    pass
                continue
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:200]
            except Exception:
                pass
            print(f"[nexus] {url} answered {exc.code}: {detail}", file=sys.stderr)
            return None
        except (urllib.error.URLError, OSError, ValueError, KeyError) as exc:
            print(
                f"[nexus] no local Nexus service at {serve_url()} ({exc}).\n"
                "        Start it with `nexus-memory serve` (installed as a service by\n"
                "        the wizard), or pick a provider explicitly:\n"
                "        NEXUS_EMBEDDING_PROVIDER=ollama|openai|google|jina",
                file=sys.stderr,
            )
            return None
        vector = payload.get("embedding")
        if not isinstance(vector, list) or not vector:
            print(
                f"[nexus] {url} returned no usable vector "
                f"(model={payload.get('model')!r}) — skipping",
                file=sys.stderr,
            )
            return None
        return vector
    return None


def serve_reachable(timeout: float = 2.0) -> bool:
    """Cheap /healthz probe — used by the self-check hook."""
    try:
        with urllib.request.urlopen(f"{serve_url()}/healthz", timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except Exception:
        return False
