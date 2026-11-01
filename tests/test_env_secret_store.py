"""Security tests for the .env secret-writing paths of both setup wizards.

Covers the three HIGH findings from the external code review:

1. chat_wizard.py — API keys must be validated before being written into
   the dotenv file, so a value containing quotes/newlines can never
   inject additional .env entries.
2. chat_wizard.py — the secrets file must be created with restrictive
   permissions (dir 0700, file 0600), pre-existing wide permissions must
   be repaired, and writes must go through an atomic temp-file replace.
3. wizard.py — same permissions/atomicity guarantees for the interactive
   wizard's save path.

All tests use real temp directories and assert real filesystem restrictions:
POSIX modes via ``os.stat`` (``st_mode``), Windows ACLs via ``icacls`` (mode
bits do not express access there).
"""

from __future__ import annotations

import getpass
import os
import stat
import subprocess
import tempfile
from pathlib import Path

import pytest

from nexus_memory import env_secret_store as ess
from nexus_memory import wizard as wiz
from nexus_memory import chat_wizard as cw


def _ok(result) -> bool:
    """First element of a validate_api_key (ok, reason) tuple."""
    return result[0]


@pytest.fixture
def tmp_env(tmp_path):
    """A temp config dir + .env path, mirroring ~/.nexus-memory/.env."""
    return tmp_path, tmp_path / ".env"


def _mode(path: Path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


# Principals that must never see the secrets file on Windows. Checked by name
# (GitHub's windows-latest is en-US, so icacls prints exactly these) and by
# well-known SID, which is what the code grants/removes.
_WIDE_TRUSTEES = ("everyone", "builtin\\users", "authenticated users")
_WIDE_TRUSTEE_SIDS = ("s-1-1-0", "s-1-5-32-545", "s-1-5-11")


def _icacls(path: Path) -> str:
    """Raw ``icacls`` listing for *path* (Windows only)."""
    proc = subprocess.run(
        ["icacls", str(path)], capture_output=True, text=True, timeout=30
    )
    assert proc.returncode == 0, f"icacls {path} failed: {proc.stderr}"
    # The first line is "<path> <first ACE>" — drop the path so a directory
    # named e.g. "Users" cannot be mistaken for a trustee.
    return proc.stdout.replace(str(path), "").lower()


def _assert_restricted(path: Path, *, is_dir: bool = False) -> None:
    """Assert *path* is private to its owner.

    POSIX: 0700 for directories, 0600 for files, no group/world bits.
    Windows: ACL holds the current account and SYSTEM, and none of the
    wide default principals (Everyone / BUILTIN\\Users / Authenticated
    Users) — os.chmod alone cannot express this there.
    """
    if os.name != "nt":
        expected = 0o700 if is_dir else 0o600
        assert _mode(path) == expected
        assert _mode(path) & 0o077 == 0
        return

    acl = _icacls(path)
    for trustee in _WIDE_TRUSTEES:
        assert trustee not in acl, f"{trustee} still has access to {path}:\n{acl}"
    for sid in _WIDE_TRUSTEE_SIDS:
        assert sid not in acl, f"{sid} still has access to {path}:\n{acl}"
    assert getpass.getuser().lower() in acl, f"owner missing from ACL:\n{acl}"
    assert "system" in acl, f"SYSTEM missing from ACL:\n{acl}"


# ---------------------------------------------------------------------------
# Finding 1: API-key validation / injection prevention (chat_wizard path)
# ---------------------------------------------------------------------------


class TestApiKeyValidation:
    def test_rejects_newline_injection_payload(self):
        payload = 'sk-x"\nNEXUS_SECURITY_OFF="1'
        ok, reason = ess.validate_api_key("OPENAI_API_KEY", payload)
        assert not ok
        assert "invalid characters" in reason

    def test_rejects_carriage_return(self):
        ok, _ = ess.validate_api_key("K", "abc\rdef")
        assert not ok

    def test_rejects_tab_and_control_characters(self):
        assert not _ok(ess.validate_api_key("K", "ab\tx"))
        assert not _ok(ess.validate_api_key("K", "ab\x01x"))
        assert not _ok(ess.validate_api_key("K", "ab\x7fx"))

    def test_rejects_empty_and_whitespace(self):
        assert not _ok(ess.validate_api_key("K", ""))
        assert not _ok(ess.validate_api_key("K", "a b"))

    def test_rejects_overlong_key(self):
        assert not _ok(ess.validate_api_key("K", "a" * 513))
        assert _ok(ess.validate_api_key("K", "a" * 512))

    def test_accepts_realistic_provider_keys(self):
        for key in ("sk-proj-AbC123_-xyz", "sk-abc123", "AIzaSyD-123"):
            assert _ok(ess.validate_api_key("K", key)), key

    def test_serialize_escapes_quotes_and_backslash(self):
        assert ess.serialize_env_value("it's") == "'it\\'s'"
        assert ess.serialize_env_value("back\\slash") == "'back\\\\slash'"
        assert ess.serialize_env_value("plain") == "'plain'"

    def test_write_env_key_rejects_invalid_value(self, tmp_env):
        d, env = tmp_env
        with pytest.raises(ValueError):
            ess.write_env_key(env, "OPENAI_API_KEY", 'sk-x"\nEVIL="1')
        assert not env.exists()

    def test_injected_line_never_reaches_file(self, tmp_env):
        d, env = tmp_env
        payload = 'sk-x"\nNEXUS_SECURITY_OFF="1'
        with pytest.raises(ValueError):
            ess.write_env_key(env, "OPENAI_API_KEY", payload)
        # No injected entry anywhere in the directory tree.
        for p in d.iterdir():
            assert "NEXUS_SECURITY_OFF" not in p.read_text(encoding="utf-8")

    def test_chat_wizard_apply_choice_rejects_injection(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cw, "_get_config_dir", lambda: tmp_path)
        result = cw.apply_choice("openai", 'sk-x"\nNEXUS_SECURITY_OFF="1')
        assert "error" in result
        assert not (tmp_path / ".env").exists()

    def test_chat_wizard_apply_choice_accepts_valid_key(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cw, "_get_config_dir", lambda: tmp_path)
        # The wizard only writes the key after its pip dependency check
        # passes; the test pins the check itself so this test verifies the
        # secret-writing path on every interpreter (with or without the
        # optional 'openai' package installed).
        monkeypatch.setattr(cw, "_check_pip_package", lambda package: True)
        # apply_choice() writes the key DIRECTLY into os.environ (wizard
        # semantics, review fix MEDIUM :21) — pre-registering it here makes
        # monkeypatch the owner, so the value is removed again after this
        # test and cannot leak into later tests (live-key reads made this
        # leak visible: a later _Embedder() init picked the fake key up).
        monkeypatch.setenv("OPENAI_API_KEY", "sk-w19-placeholder")
        result = cw.apply_choice("openai", "sk-w19-placeholder")
        assert result.get("provider") == "openai"
        assert (tmp_path / ".env").exists()

    def test_roundtrip_via_python_dotenv(self, tmp_env):
        dotenv = pytest.importorskip("dotenv")
        _, env = tmp_env
        ess.write_env_key(env, "OPENAI_API_KEY", "sk-weird'key\\value")
        parsed = dotenv.dotenv_values(env)
        assert parsed["OPENAI_API_KEY"] == "sk-weird'key\\value"


# ---------------------------------------------------------------------------
# Finding 2: restrictive permissions + atomic write (chat_wizard path)
# ---------------------------------------------------------------------------


class TestPermissionsChatWizard:
    def test_new_dir_and_file_get_restrictive_modes(self, tmp_env):
        d, env = tmp_env
        ess.write_env_key(env, "OPENAI_API_KEY", "sk-abc123")
        _assert_restricted(env)
        _assert_restricted(d, is_dir=True)

    def test_wizard_save_api_key_sets_restrictive_modes(self, tmp_path, monkeypatch):
        monkeypatch.setattr(wiz, "_get_config_dir", lambda: tmp_path)
        wiz._save_api_key("OPENAI_API_KEY", "sk-validkey123")
        _assert_restricted(tmp_path / ".env")
        _assert_restricted(tmp_path, is_dir=True)

    def test_preexisting_wide_permissions_are_repaired(self, tmp_env):
        d, env = tmp_env
        env.write_text('OLD_KEY="old"\n', encoding="utf-8")
        os.chmod(env, 0o644)
        os.chmod(d, 0o755)
        ess.write_env_key(env, "NEW_KEY", "nk-123")
        _assert_restricted(env)
        _assert_restricted(d, is_dir=True)

    def test_preserves_existing_entries_on_rewrite(self, tmp_env):
        _, env = tmp_env
        ess.write_env_key(env, "A", "a")
        ess.write_env_key(env, "B", "b")
        ess.write_env_key(env, "A", "a2")
        text = env.read_text(encoding="utf-8")
        assert "A='a2'" in text and "B='b'" in text
        # Only one entry per key
        assert text.count("A=") == 1

    def test_write_is_atomic_no_temp_file_left_behind(self, tmp_env):
        d, env = tmp_env
        ess.write_env_key(env, "A", "a")
        before = set(os.listdir(d))
        ess.write_env_key(env, "A", "a2")
        after = set(os.listdir(d))
        assert after == before  # no leftover .env-tmp-* files
        assert "A='a2'" in env.read_text(encoding="utf-8")

    def test_chat_wizard_save_api_key_sets_restrictive_modes(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(cw, "_get_config_dir", lambda: tmp_path)
        cw._save_api_key("JINA_API_KEY", "jina_validkey")
        _assert_restricted(tmp_path / ".env")
        _assert_restricted(tmp_path, is_dir=True)


# ---------------------------------------------------------------------------
# Finding 3: restrictive permissions + atomic write (interactive wizard)
# ---------------------------------------------------------------------------


class TestPermissionsInteractiveWizard:
    def test_wizard_env_helper_writes_0600(self, tmp_env):
        _, env = tmp_env
        ess.write_env_key(env, "OPENAI_API_KEY", "sk-abc123")
        _assert_restricted(env)

    def test_wizard_rewrites_keep_0600(self, tmp_env):
        _, env = tmp_env
        ess.write_env_key(env, "A", "1")
        os.chmod(env, 0o666)  # simulate later chmod widening
        ess.write_env_key(env, "A", "2")
        _assert_restricted(env)

    def test_wizard_dir_0700_after_repeated_saves(self, tmp_env):
        d, env = tmp_env
        ess.write_env_key(env, "A", "1")
        os.chmod(d, 0o777)  # simulate directory permission drift
        ess.write_env_key(env, "B", "2")
        _assert_restricted(d, is_dir=True)

    def test_no_group_or_world_bits_ever(self, tmp_env):
        _, env = tmp_env
        for key, val in (("A", "a"), ("B", "b")):
            ess.write_env_key(env, key, val)
            _assert_restricted(env)