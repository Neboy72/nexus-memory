"""The embedding path shared by every Claude Code hook.

One rule, so all four hooks cannot drift apart:

* **automatic (default)** — the hook asks the local Nexus service
  (``NEXUS_SERVE_URL``, default ``http://127.0.0.1:9122``) for the vector. That
  service runs the engine with its **local HuggingFace** model, so this plugin
  works on a machine with nothing installed: no Ollama, no API key, and every
  agent in the house shares one vector space.
* **explicit** — ``NEXUS_EMBEDDING_PROVIDER=ollama|voyage|openai|google|jina``
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

DEFAULT_SERVE_URL = "http://127.0.0.1:9122"

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
    """
    url = f"{serve_url()}/embed"
    try:
        req = urllib.request.Request(
            url,
            data=json.dumps({"text": text, "is_query": is_query}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload = json.loads(resp.read())
        vector = payload.get("embedding")
        if not isinstance(vector, list) or not vector:
            print(
                f"[nexus] {url} returned no usable vector "
                f"(model={payload.get('model')!r}) — skipping",
                file=sys.stderr,
            )
            return None
        return vector
    except urllib.error.HTTPError as exc:
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
            "        NEXUS_EMBEDDING_PROVIDER=ollama|voyage|openai|google|jina",
            file=sys.stderr,
        )
        return None


def serve_reachable(timeout: float = 2.0) -> bool:
    """Cheap /healthz probe — used by the self-check hook."""
    try:
        with urllib.request.urlopen(f"{serve_url()}/healthz", timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except Exception:
        return False
