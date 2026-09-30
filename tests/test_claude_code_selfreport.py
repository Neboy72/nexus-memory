"""Tests for the Claude Code plugin self-report path.

The hook script is plain stdlib and loaded from its file path (the scripts
directory is not a package). Coverage mirrors the Hermes plugin self-report
tests: the file contract, the health probe, atomic write, fail-open behaviour,
and the agent-visible warning.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT_PATH = _REPO_ROOT / "plugins" / "claude-code" / "scripts" / "self_check.py"


def _load(name: str = "claude_self_check"):
    spec = importlib.util.spec_from_file_location(name, str(_SCRIPT_PATH))
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def sc():
    return _load()


class _HealthyUrlopen:
    """urllib.request.urlopen replacement that pretends Qdrant is up."""

    def __call__(self, *args, **kwargs):
        return self

    def read(self):
        return b'{"collections": []}'

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


def _patch_urlopen_healthy(monkeypatch, mod):
    monkeypatch.setattr(mod.urllib.request, "urlopen", _HealthyUrlopen())


def _patch_urlopen_broken(monkeypatch, mod, exc_cls=Exception):
    def _boom(*args, **kwargs):
        raise exc_cls("network down")

    monkeypatch.setattr(mod.urllib.request, "urlopen", _boom)


def _run(sc, monkeypatch, tmp_path, agent_id="claude-code", healthy=True):
    """Run the script's main() with controlled env and urlopen."""
    monkeypatch.setenv("NEXUS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("NEXUS_AGENT_ID", agent_id)
    if healthy:
        _patch_urlopen_healthy(monkeypatch, sc)
        monkeypatch.setenv("VOYAGE_API_KEY", "x")
    else:
        _patch_urlopen_broken(monkeypatch, sc)

    sc.main()


class TestProbeAndFile:
    def test_healthy_probe_ok_empty_stdout(self, sc, monkeypatch, tmp_path, capsys):
        _run(sc, monkeypatch, tmp_path, healthy=True)

        path = tmp_path / "agent-selfcheck-claude-code.json"
        assert path.exists()
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["ok"] is True
        assert data["reason"] == ""
        assert capsys.readouterr().out == ""

    def test_qdrant_unreachable_writes_false_and_warns_stdout(
        self, sc, monkeypatch, tmp_path, capsys
    ):
        monkeypatch.setenv("NEXUS_DATA_DIR", str(tmp_path))
        monkeypatch.setenv("NEXUS_AGENT_ID", "claude-code")
        monkeypatch.setenv("VOYAGE_API_KEY", "x")
        _patch_urlopen_broken(monkeypatch, sc)

        sc.main()

        path = tmp_path / "agent-selfcheck-claude-code.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["ok"] is False
        assert "Qdrant" in data["reason"]
        assert data["fix"]

        captured = capsys.readouterr()
        stdout = captured.out
        assert stdout
        parsed = json.loads(stdout)
        assert parsed["hookSpecificOutput"]["hookEventName"] == "SessionStart"
        ctx = parsed["hookSpecificOutput"]["additionalContext"]
        assert "NOT WORKING" in ctx
        assert data["reason"] in ctx
        # Universal hook field: the same failure is shown directly to the user.
        assert parsed["systemMessage"]
        assert captured.err == ""

    def test_system_message_is_short_single_line_user_facing(
        self, sc, monkeypatch, tmp_path, capsys
    ):
        """systemMessage is the user-facing one-liner; additionalContext is the
        full model-facing warning. Both must ride in the same hook output."""
        _run(sc, monkeypatch, tmp_path, healthy=False)
        captured = capsys.readouterr()
        parsed = json.loads(captured.out)

        msg = parsed["systemMessage"]
        assert isinstance(msg, str) and msg
        assert "\n" not in msg
        assert msg.startswith("Nexus Memory is not working")
        assert "Fix:" in msg
        assert "Your memories are safe" in msg

        ctx = parsed["hookSpecificOutput"]["additionalContext"]
        assert "NOT WORKING" in ctx
        assert "\n" in ctx

    def test_system_message_truncated_reason_stays_bounded(self, sc):
        long_reason = "x" * 5000
        msg = sc._system_message(long_reason, "y" * 500)
        assert len(msg) <= 500
        assert "\n" not in msg

    def test_missing_api_key_writes_false_and_names_var(
        self, sc, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("NEXUS_DATA_DIR", str(tmp_path))
        monkeypatch.setenv("NEXUS_AGENT_ID", "claude-code")
        monkeypatch.setenv("NEXUS_EMBEDDING_PROVIDER", "openai")
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        _patch_urlopen_healthy(monkeypatch, sc)

        sc.main()

        path = tmp_path / "agent-selfcheck-claude-code.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["ok"] is False
        assert "OPENAI_API_KEY" in data["reason"]
        assert "OPENAI_API_KEY" in data["fix"]


class TestFileContract:
    def test_atomic_write_leaves_no_tmp_file(self, sc, monkeypatch, tmp_path):
        monkeypatch.setenv("NEXUS_DATA_DIR", str(tmp_path))
        monkeypatch.setenv("NEXUS_AGENT_ID", "claude-code")
        _patch_urlopen_healthy(monkeypatch, sc)
        monkeypatch.setenv("VOYAGE_API_KEY", "x")

        sc.main()
        assert list(tmp_path.glob("agent-selfcheck*.tmp-*")) == []

        # Force the replace step to fail and assert the temp file is removed.
        def _boom(*args, **kwargs):
            raise OSError("replace failed")

        monkeypatch.setattr(sc.os, "replace", _boom)
        sc.main()
        assert list(tmp_path.glob("agent-selfcheck*.tmp-*")) == []

    def test_unwritable_data_dir_does_not_raise(self, sc, monkeypatch, tmp_path):
        monkeypatch.setenv("NEXUS_DATA_DIR", str(tmp_path))
        monkeypatch.setenv("NEXUS_AGENT_ID", "claude-code")
        _patch_urlopen_healthy(monkeypatch, sc)
        monkeypatch.setenv("VOYAGE_API_KEY", "x")

        tmp_path.chmod(0o555)
        try:
            sc.main()  # must not raise
        finally:
            tmp_path.chmod(0o755)

    def test_agent_id_sanitization_stays_in_data_dir(
        self, sc, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("NEXUS_DATA_DIR", str(tmp_path))
        monkeypatch.setenv("NEXUS_AGENT_ID", "../../evil")
        _patch_urlopen_healthy(monkeypatch, sc)
        monkeypatch.setenv("VOYAGE_API_KEY", "x")

        sc.main()

        paths = list(tmp_path.glob("agent-selfcheck-*.json"))
        assert len(paths) == 1
        assert paths[0].parent == tmp_path

    def test_data_dir_redirect_honored(self, sc, monkeypatch, tmp_path):
        monkeypatch.setenv("NEXUS_DATA_DIR", str(tmp_path))
        monkeypatch.setenv("NEXUS_AGENT_ID", "test-id")
        _patch_urlopen_healthy(monkeypatch, sc)
        monkeypatch.setenv("VOYAGE_API_KEY", "x")

        sc.main()

        path = tmp_path / "agent-selfcheck-test-id.json"
        assert path.exists()

    def test_timestamp_format(self, sc, monkeypatch, tmp_path):
        monkeypatch.setenv("NEXUS_DATA_DIR", str(tmp_path))
        monkeypatch.setenv("NEXUS_AGENT_ID", "claude-code")
        _patch_urlopen_healthy(monkeypatch, sc)
        monkeypatch.setenv("VOYAGE_API_KEY", "x")

        sc.main()

        data = json.loads(
            (tmp_path / "agent-selfcheck-claude-code.json").read_text(encoding="utf-8")
        )
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", data["ts"])

    def test_payload_keys_exact(self, sc, monkeypatch, tmp_path):
        monkeypatch.setenv("NEXUS_DATA_DIR", str(tmp_path))
        monkeypatch.setenv("NEXUS_AGENT_ID", "claude-code")
        _patch_urlopen_healthy(monkeypatch, sc)
        monkeypatch.setenv("VOYAGE_API_KEY", "x")

        sc.main()

        data = json.loads(
            (tmp_path / "agent-selfcheck-claude-code.json").read_text(encoding="utf-8")
        )
        assert set(data.keys()) == {
            "agent_id",
            "ok",
            "reason",
            "fix",
            "interpreter",
            "plugin_version",
            "ts",
        }

    def test_plugin_version_from_manifest(self, sc):
        manifest = json.loads(sc.PLUGIN_MANIFEST.read_text(encoding="utf-8"))
        assert sc._plugin_version() == manifest["version"]

    def test_version_fallback_matches_manifest(self, sc, monkeypatch, tmp_path):
        """The literal fallback is a second version source: it must not go stale.

        A bump that forgets the fallback reports the previous version exactly
        when the manifest is unreadable (a broken install) - the worst moment
        for a wrong version. Fail loudly instead.
        """
        manifest = json.loads(sc.PLUGIN_MANIFEST.read_text(encoding="utf-8"))
        monkeypatch.setattr(sc, "PLUGIN_MANIFEST", tmp_path / "missing.json")
        assert sc._plugin_version() == manifest["version"]

    def test_warning_is_capped(self, sc, monkeypatch):
        long_reason = "x" * 5000
        fix = "y" * 500
        text = sc._warning_text(long_reason, fix)
        assert len(text) <= 1000
        assert "NOT WORKING" in text


class TestInstallerContract:
    """The README advertises a one-command installer for this plugin.

    It was missing while the README pointed at it, and an installer that drops
    the self-check hook would reintroduce the silent no-memory failure this
    feature exists to catch. Keep both promises pinned.
    """

    def test_installer_exists_and_ships_self_check(self):
        installer = _REPO_ROOT / "scripts" / "install_claude_plugin.sh"
        assert installer.exists(), "README advertises scripts/install_claude_plugin.sh"
        text = installer.read_text(encoding="utf-8")
        assert "self_check.py" in text, "installer must verify the self-check hook"
        assert "Backup" in text, "installer must back up an existing install first"
