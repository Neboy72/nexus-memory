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
- On Windows, where POSIX mode bits do not grant or deny access, the same
  restriction is enforced with ``icacls`` (ACL inheritance removed; only
  the current account and SYSTEM keep full control) for the directory and
  the file.
- Writes are atomic via a ``0600`` temp file + ``os.replace`` so readers
  never observe a partially written file.
"""

from __future__ import annotations

import getpass
import os
import re
import subprocess
import tempfile
import warnings
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


def _unquote_env_value(v: str) -> str:
    """Invert :func:`serialize_env_value` (the exact complement).

    Handles both single- and double-quoted values. For single quotes the
    escape sequences are inverted (``\\'`` -> ``'``, ``\\\\`` -> ``\\``) so a
    value round-trips losslessly; the old ``.strip("'").strip('"')`` code
    stripped quoting WITHOUT inverting escapes, corrupting any value that
    contained a quote or backslash.
    """
    v = v.strip()
    if len(v) >= 2 and v.startswith("'") and v.endswith("'"):
        return v[1:-1].replace("\\'", "'").replace("\\\\", "\\")
    if len(v) >= 2 and v.startswith('"') and v.endswith('"'):
        return v[1:-1]
    return v


def _parse_env_text(text: str) -> Dict[str, str]:
    """Parse simple KEY=value lines (used for read-modify-write)."""
    existing: Dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            existing[k.strip()] = _unquote_env_value(v)
    return existing


# Well-known SIDs of principals that must never keep access to a secrets
# file (Everyone, BUILTIN\Users, Authenticated Users). SIDs instead of
# trustee names because the names are localized on non-English Windows.
_WIDE_PRINCIPAL_SIDS = ("*S-1-1-0", "*S-1-5-32-545", "*S-1-5-11")


def _restrict_windows_acl(path: Path, *, is_dir: bool) -> None:
    """Restrict *path* to the current account + SYSTEM using ``icacls``.

    ``os.chmod`` cannot enforce this on Windows: access is granted by ACLs,
    not by POSIX bits, so a file created under ``%TEMP%``/``%USERPROFILE%``
    keeps the inherited access of BUILTIN\\Users and Authenticated Users and
    a ``.env`` holding API keys stays readable by every local account.
    ``icacls`` is part of Windows, so this adds no dependency.

    A failure is surfaced as a warning (never silent) — the restriction is a
    security guarantee, so an inability to enforce it must be observable.
    """
    try:
        owner = getpass.getuser()
    except Exception:  # getpass raises when no account name is resolvable
        owner = ""
    owner = owner or os.environ.get("USERNAME", "")
    if not owner:
        warnings.warn(
            f"could not determine the current Windows account — ACL on {path} "
            "left unchanged (API keys there may be readable by other accounts)",
            RuntimeWarning,
            stacklevel=2,
        )
        return

    # (OI)(CI) makes the grant inherit onto new children of a directory.
    ace = ":(OI)(CI)F" if is_dir else ":F"
    commands = (
        ["icacls", str(path), "/inheritance:r"],
        ["icacls", str(path), "/grant:r", f"{owner}{ace}", f"SYSTEM{ace}"],
        ["icacls", str(path), "/remove:g", *_WIDE_PRINCIPAL_SIDS],
    )
    for cmd in commands:
        try:
            # icacls is a Windows console program: it writes the OEM code page,
            # not the ANSI one. Decoding its output with the ANSI code page (or
            # with UTF-8, when the parent runs in UTF-8 mode) fails inside the
            # reader thread, which leaves ``stdout`` as None and turns the error
            # report below into an AttributeError.
            proc = subprocess.run(
                cmd, capture_output=True, text=True,
                encoding="oem", errors="replace", timeout=30,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            warnings.warn(
                f"could not restrict the ACL on {path} (icacls unavailable): {exc}",
                RuntimeWarning,
                stacklevel=2,
            )
            return
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()
            warnings.warn(
                f"icacls could not restrict {path} to {owner} + SYSTEM "
                f"(exit {proc.returncode}): {detail}",
                RuntimeWarning,
                stacklevel=2,
            )
            return


def fix_dir_permissions(config_dir: Path) -> None:
    """Tighten the config directory to 0700 (owner+SYSTEM only on Windows).

    A failure is surfaced as a warning (never silent) — the restrictive mode
    is a security guarantee, so an inability to enforce it must be observable.
    """
    if os.name == "nt":
        _restrict_windows_acl(config_dir, is_dir=True)
        return
    try:
        os.chmod(config_dir, DIR_MODE)
    except OSError as exc:
        warnings.warn(
            f"could not set {DIR_MODE:04o} on config dir {config_dir}: {exc}",
            RuntimeWarning,
            stacklevel=2,
        )


def write_env_key(env_path: Path, key_env: str, api_key: str) -> None:
    """Store ``KEY=value`` in the dotenv file at ``env_path`` securely.

    - creates ``env_path.parent`` with mode 0700 (tightens it if wider),
    - preserves and updates existing entries via read-modify-write,
    - writes atomically through a 0600 temp file + ``os.replace``,
    - leaves the final file at mode 0600 (also fixes pre-existing files
      that were created with wider permissions).

    Raises ``ValueError`` if the key value fails ``validate_api_key`` and
    ``RuntimeError`` if an existing ``.env`` cannot be read (see W32-13).
    """
    ok, reason = validate_api_key(key_env, api_key)
    if not ok:
        raise ValueError(reason)

    config_dir = env_path.parent
    config_dir.mkdir(parents=True, exist_ok=True)
    fix_dir_permissions(config_dir)

    existing: Dict[str, str] = {}
    if env_path.exists():
        # W32-13: an existing but UNREADABLE .env must abort. The old code
        # folded the OSError into ``existing = {}`` and then os.replace'd a
        # file holding only the new key — silently destroying every other
        # secret in it. A missing file is a fresh install (fine); an
        # unreadable one is a data-loss trap (not fine).
        try:
            text = env_path.read_text()
        except FileNotFoundError:
            text = ""
        except OSError as exc:
            raise RuntimeError(
                f"cannot read existing .env at {env_path} — refusing to overwrite "
                f"(would drop existing keys): {exc}"
            ) from exc
        # A parse problem (not an OS error) is not fatal: warn and keep
        # whatever was parseable rather than dropping the whole file.
        try:
            existing = _parse_env_text(text)
        except Exception as exc:  # pragma: no cover - defensive
            warnings.warn(
                f"could not fully parse existing .env at {env_path}: {exc} "
                "— continuing with the parseable entries",
                RuntimeWarning,
                stacklevel=2,
            )
    existing[key_env] = api_key

    body = "\n".join(f"{k}={serialize_env_value(v)}" for k, v in existing.items())

    # Atomic write: restrictive temp file in the same directory, fsync,
    # then os.replace so readers never see a partial file and the final
    # path is guaranteed to carry the restrictive mode.
    fd, tmp_name = tempfile.mkstemp(
        dir=str(config_dir), prefix=".env-tmp-", suffix=".tmp"
    )
    tmp_path = Path(tmp_name)
    # Own the raw descriptor until os.fdopen() takes it over — otherwise a
    # failure in fchmod/fdopen would leak the open fd (mkstemp does not
    # register it for cleanup).
    fd_owned = True
    try:
        # os.fchmod is unavailable on Windows (AttributeError) — only call it
        # where the platform provides it.
        fchmod = getattr(os, "fchmod", None)
        if fchmod is not None and os.name != "nt":
            fchmod(fd, FILE_MODE)
        with os.fdopen(fd, "w") as f:
            fd_owned = False  # the file object now closes the fd
            f.write(body + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, env_path)
    except Exception:
        if fd_owned:
            try:
                os.close(fd)
            except OSError:
                pass
        tmp_path.unlink(missing_ok=True)
        raise

    # Repair permissions on pre-existing files that may have been created
    # with a wide umask, and enforce 0600 on the freshly replaced file.
    # Windows has no POSIX bits to enforce — restrict the file ACL instead.
    if os.name == "nt":
        _restrict_windows_acl(env_path, is_dir=False)
    else:
        try:
            os.chmod(env_path, FILE_MODE)
        except OSError as exc:
            warnings.warn(
                f"could not enforce {FILE_MODE:04o} on {env_path}: {exc}",
                RuntimeWarning,
                stacklevel=2,
            )


def read_env_key(env_path: Path, key_env: str) -> str:
    """Read one KEY from the dotenv file (wizard ↔ provider state sync).

    Returns '' when the file or the key is missing. Values are unquoted
    (single-quote serialization is inverted; escaped chars round-trip).
    """
    try:
        text = env_path.read_text()
    except OSError:
        return ""
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        if k.strip() != key_env:
            continue
        # Invert serialize_env_value() for BOTH single- and double-quoted
        # values (previously only single quotes were handled).
        return _unquote_env_value(v)
    return ""