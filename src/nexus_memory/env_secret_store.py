"""Shared helpers for writing API-key secrets into a dotenv file securely.

Used by both setup wizards (``wizard.py`` and ``chat_wizard.py``) so the
security-relevant behaviour lives in exactly one place:

- API-key values are validated against a strict provider-key format
  (printable ASCII, no CR/LF/control characters, bounded length) so a
  crafted value can never inject additional ``.env`` entries.
- Values are serialized with a properly escaping dotenv writer
  (single-quoted values, internal backslash/quote escaped) instead of
  raw f-string interpolation into double quotes.
- The config directory is created with mode ``0700`` and the ``.env``
  file with ``0600``; pre-existing files/dirs with wider permissions
  are tightened in place.
- Writes are atomic via a ``0600`` temp file + ``os.replace`` so readers
  never observe a partially written file.
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from typing import Dict, Tuple

# Allowed API-key shape: printable ASCII without whitespace. Anchored so a
# multi-line injection payload can never match.
_API_KEY_RE = re.compile(r"\A[\x21-\x7E]{1,512}\Z")

DIR_MODE = 0o700
FILE_MODE = 0o600


def validate_api_key(key_env: str, api_key: str) -> Tuple[bool, str]:
    """Validate an API key before it is written to the dotenv file.

    Returns ``(ok, reason)``. Rejects empty values, non-ASCII, any CR/LF
    or other control characters, and values longer than 512 characters —
    all shapes that could smuggle extra dotenv entries into the file.
    """
    if not isinstance(api_key, str) or not api_key:
        return False, f"{key_env}: API key must be a non-empty string"
    if len(api_key) > 512:
        return False, f"{key_env}: API key too long (max 512 characters)"
    if not _API_KEY_RE.match(api_key):
        return False, (
            f"{key_env}: API key contains invalid characters "
            "(only printable ASCII without whitespace allowed)"
        )
    return True, ""


def serialize_env_value(value: str) -> str:
    """Serialize a dotenv value as a single-quoted, escaped literal.

    ``\\`` becomes ``\\\\`` and ``'`` becomes ``\\'`` so the value round-trips
    through standard dotenv parsers (e.g. python-dotenv) without any
    possibility of breaking out of the quoting context.
    """
    escaped = value.replace("\\", "\\\\").replace("'", "\\'")
    return f"'{escaped}'"


def _parse_env_text(text: str) -> Dict[str, str]:
    """Parse simple KEY=value lines (used for read-modify-write)."""
    existing: Dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            existing[k.strip()] = v.strip().strip('"').strip("'")
    return existing


def _fix_dir_permissions(config_dir: Path) -> None:
    """Best-effort tightening of the config directory to 0700."""
    try:
        os.chmod(config_dir, DIR_MODE)
    except OSError:
        pass


def write_env_key(env_path: Path, key_env: str, api_key: str) -> None:
    """Store ``KEY=value`` in the dotenv file at ``env_path`` securely.

    - creates ``env_path.parent`` with mode 0700 (tightens it if wider),
    - preserves and updates existing entries via read-modify-write,
    - writes atomically through a 0600 temp file + ``os.replace``,
    - leaves the final file at mode 0600 (also fixes pre-existing files
      that were created with wider permissions).

    Raises ``ValueError`` if the key value fails ``validate_api_key``.
    """
    ok, reason = validate_api_key(key_env, api_key)
    if not ok:
        raise ValueError(reason)

    config_dir = env_path.parent
    config_dir.mkdir(parents=True, exist_ok=True)
    _fix_dir_permissions(config_dir)

    existing: Dict[str, str] = {}
    if env_path.exists():
        try:
            existing = _parse_env_text(env_path.read_text())
        except OSError:
            existing = {}
    existing[key_env] = api_key

    body = "\n".join(f"{k}={serialize_env_value(v)}" for k, v in existing.items())

    # Atomic write: restrictive temp file in the same directory, fsync,
    # then os.replace so readers never see a partial file and the final
    # path is guaranteed to carry the restrictive mode.
    fd, tmp_name = tempfile.mkstemp(
        dir=str(config_dir), prefix=".env-tmp-", suffix=".tmp"
    )
    tmp_path = Path(tmp_name)
    try:
        os.fchmod(fd, FILE_MODE)
        with os.fdopen(fd, "w") as f:
            f.write(body + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, env_path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise

    # Repair permissions on pre-existing files that may have been created
    # with a wide umask, and enforce 0600 on the freshly replaced file.
    try:
        os.chmod(env_path, FILE_MODE)
    except OSError:
        pass