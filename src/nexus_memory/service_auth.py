"""Per-installation authentication for the local Nexus Memory service.

The service binds loopback only, but loopback is shared by every local
process (and reachable from a browser via DNS rebinding).  Both the
``/embed`` endpoint and the ``/mcp`` Streamable-HTTP endpoint therefore
require a per-installation secret that is generated once, stored with
mode 0600 under the data directory, and read by the native plugin.
"""
from __future__ import annotations

import logging
import os
import secrets
from pathlib import Path

from nexus_memory.self_report import data_dir as _data_dir

logger = logging.getLogger(__name__)

# Header name used by both the server and the plugin.  Kept in one place so the
# two sides can never drift out of agreement.
AUTH_HEADER = "x-nexus-service-key"
_KEY_FILE_NAME = "service_auth.key"


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
