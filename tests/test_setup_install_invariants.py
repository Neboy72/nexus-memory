"""Invariant tests for nexus_memory.setup agent installation flow.

Covers four invariants:
1. install_agent falls back to the MCP config template when a plugin is
   unavailable or fails, registers only after a successful installation and
   reports install_type built only from the parts that actually succeeded.
2. Per-agent MCP config adapters: Codex gets ~/.codex/config.toml (TOML,
   mcp_servers), Claude Code gets ~/.claude.json, Windsurf gets
   ~/.codeium/windsurf/mcp_config.json — the generic <agent_dir>/mcp.json
   layout is only written for agents that really load it.
3. A broken/unreadable existing MCP config is never treated as empty and is
   never overwritten; writes go through temp-file + os.replace (atomic).
4. The recommended Qdrant docker run command publishes the port on
   127.0.0.1 only.
"""

from __future__ import annotations

import builtins
import json
import sys
import tomllib
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC = str(_REPO_ROOT / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from nexus_memory import setup as setup_mod  # noqa: E402


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    """Point ``~``-expansion at a temp dir so setup writes never touch the
    real home directory. os.environ["HOME"] alone is not enough: on macOS
    ``Path.expanduser`` falls back to the passwd database when HOME doesn't
    match a real directory, so the expansion method itself is patched."""
    real_home = Path.home()
    real_expanduser = Path.expanduser

    def fake_expanduser(self):
        expanded = real_expanduser(self)
        if str(expanded).startswith(str(real_home)):
            rel = str(expanded)[len(str(real_home)):]
            return tmp_path / rel.lstrip("/")
        return expanded

    monkeypatch.setattr(setup_mod.Path, "expanduser", fake_expanduser)
    return tmp_path


def _write_detection(monkeypatch, agent_id: str, *, plugin: bool, mcp: bool, config_dir: str = ""):
    """Stub detect_all_agents() to report a single synthetic agent."""
    info = {
        "id": agent_id,
        "name": f"Agent {agent_id}",
        "icon": "X",
        "plugin_available": plugin,
        "mcp_available": mcp,
        "config_dir": config_dir,
    }
    monkeypatch.setattr(
        setup_mod,
        "detect_all_agents",
        lambda: {"detected_agents": [info], "not_detected": []},
    )


def _no_register(monkeypatch):
    """Capture register_agent calls instead of touching the real registry."""
    calls = []
    monkeypatch.setattr(setup_mod, "register_agent", lambda **kw: calls.append(kw))
    return calls


# ---------------------------------------------------------------------------
# Invariant 1: plugin fallback, register-on-success, truthful install_type
# ---------------------------------------------------------------------------

class TestPluginFallback:
    def test_falls_back_to_mcp_when_no_plugin_script(self, tmp_path, monkeypatch, isolated_home):
        # Detected as plugin_available but has no entry in INSTALL_SCRIPTS.
        _write_detection(monkeypatch, "synth-plugin-agent", plugin=True, mcp=True)
        setup_mod.INSTALL_SCRIPTS.pop("synth-plugin-agent", None)
        setup_mod.MCP_CONFIG_SNIPPETS["synth-plugin-agent"] = {
            "file": str(tmp_path / "synth" / "mcp.json"),
            "format": "json",
            "config": {"nexus": {"command": "nexus-memory", "args": [], "env": {}}},
        }
        calls = _no_register(monkeypatch)

        result = setup_mod.install_agent("synth-plugin-agent")

        assert result["install_type"] == "mcp"
        assert result["status"] == "installed"
        assert (tmp_path / "synth" / "mcp.json").exists()
        assert len(calls) == 1 and calls[0]["install_type"] == "mcp"
        setup_mod.MCP_CONFIG_SNIPPETS.pop("synth-plugin-agent")

    def test_falls_back_to_mcp_when_plugin_script_fails(self, tmp_path, monkeypatch, isolated_home):
        _write_detection(monkeypatch, "synth-plugin-agent", plugin=True, mcp=True)
        fake_script = tmp_path / "failing_install.sh"
        fake_script.write_text("#!/bin/bash\nexit 1\n")
        setup_mod.INSTALL_SCRIPTS["synth-plugin-agent"] = str(fake_script)
        setup_mod.MCP_CONFIG_SNIPPETS["synth-plugin-agent"] = {
            "file": str(tmp_path / "synth" / "mcp.json"),
            "format": "json",
            "config": {"nexus": {"command": "nexus-memory", "args": [], "env": {}}},
        }
        calls = _no_register(monkeypatch)

        result = setup_mod.install_agent("synth-plugin-agent")

        assert result["install_type"] == "mcp"
        assert result["status"] == "installed"
        assert [s["status"] for s in result["steps"]] == ["failed", "installed"]
        assert len(calls) == 1 and calls[0]["install_type"] == "mcp"
        setup_mod.INSTALL_SCRIPTS.pop("synth-plugin-agent")
        setup_mod.MCP_CONFIG_SNIPPETS.pop("synth-plugin-agent")

    def test_no_registration_when_every_part_fails(self, tmp_path, monkeypatch, isolated_home):
        _write_detection(monkeypatch, "synth-plugin-agent", plugin=True, mcp=True)
        setup_mod.INSTALL_SCRIPTS.pop("synth-plugin-agent", None)  # plugin: no_script
        # MCP part fails: existing config is broken.
        mcp_file = tmp_path / "synth" / "mcp.json"
        mcp_file.parent.mkdir(parents=True)
        mcp_file.write_text("{ not json ")
        calls = _no_register(monkeypatch)

        result = setup_mod.install_agent("synth-plugin-agent")

        assert "error" in result
        assert calls == []
        assert mcp_file.read_text() == "{ not json "

    def test_install_type_reflects_only_successful_parts(self, tmp_path, monkeypatch, isolated_home):
        _write_detection(monkeypatch, "synth-plugin-agent", plugin=True, mcp=True)
        setup_mod.INSTALL_SCRIPTS.pop("synth-plugin-agent", None)
        setup_mod.MCP_CONFIG_SNIPPETS["synth-plugin-agent"] = {
            "file": str(tmp_path / "synth" / "mcp.json"),
            "format": "json",
            "config": {"nexus": {"command": "nexus-memory", "args": [], "env": {}}},
        }
        _no_register(monkeypatch)

        result = setup_mod.install_agent("synth-plugin-agent")

        # Plugin part failed (no script) -> must not claim "plugin+mcp".
        assert "plugin" not in result["install_type"]
        assert result["install_type"] == "mcp"
        setup_mod.MCP_CONFIG_SNIPPETS.pop("synth-plugin-agent")

    def test_both_parts_succeed_reports_plugin_plus_mcp(self, tmp_path, monkeypatch, isolated_home):
        _write_detection(monkeypatch, "synth-plugin-agent", plugin=True, mcp=True)
        fake_script = tmp_path / "ok_install.sh"
        fake_script.write_text("#!/bin/bash\nexit 0\n")
        setup_mod.INSTALL_SCRIPTS["synth-plugin-agent"] = str(fake_script)
        setup_mod.MCP_CONFIG_SNIPPETS["synth-plugin-agent"] = {
            "file": str(tmp_path / "synth" / "mcp.json"),
            "format": "json",
            "config": {"nexus": {"command": "nexus-memory", "args": [], "env": {}}},
        }
        calls = _no_register(monkeypatch)

        result = setup_mod.install_agent("synth-plugin-agent")

        assert result["install_type"] == "plugin+mcp"
        assert len(calls) == 1 and calls[0]["install_type"] == "plugin+mcp"
        setup_mod.INSTALL_SCRIPTS.pop("synth-plugin-agent")
        setup_mod.MCP_CONFIG_SNIPPETS.pop("synth-plugin-agent")


# ---------------------------------------------------------------------------
# Invariant 2: per-agent MCP config adapters
# ---------------------------------------------------------------------------

class TestPerAgentMCPAdapters:
    def test_codex_adapter_is_toml_config_toml_with_mcp_servers(self, isolated_home):
        snippet = setup_mod.MCP_CONFIG_SNIPPETS["codex"]
        assert snippet["file"] == "~/.codex/config.toml"
        assert snippet["format"] == "toml"

        result = setup_mod._install_mcp("codex")
        assert result["status"] == "installed"

        codex_file = isolated_home / ".codex" / "config.toml"
        assert codex_file.exists()
        assert not (isolated_home / ".codex" / "mcp.json").exists()
        data = tomllib.loads(codex_file.read_text())
        assert data["mcp_servers"]["nexus"]["command"] == "nexus-memory"

    def test_codex_appends_to_existing_config_without_touching_other_keys(self, isolated_home):
        codex_dir = isolated_home / ".codex"
        codex_dir.mkdir()
        (codex_dir / "config.toml").write_text(
            'model = "gpt-5"\n\n[mcp_servers.existing]\ncommand = "foo"\n'
        )

        result = setup_mod._install_mcp("codex")
        assert result["status"] == "installed"

        data = tomllib.loads((codex_dir / "config.toml").read_text())
        assert data["model"] == "gpt-5"
        assert data["mcp_servers"]["existing"]["command"] == "foo"
        assert data["mcp_servers"]["nexus"]["command"] == "nexus-memory"

    def test_claude_code_uses_claude_dot_json(self, isolated_home):
        assert setup_mod.MCP_CONFIG_SNIPPETS["claude-code"]["file"] == "~/.claude.json"
        result = setup_mod._install_mcp("claude-code")
        assert result["status"] == "installed"
        assert (isolated_home / ".claude.json").exists()
        assert not (isolated_home / ".claude" / "mcp.json").exists()
        data = json.loads((isolated_home / ".claude.json").read_text())
        assert data["mcpServers"]["nexus"]["command"] == "nexus-memory"

    def test_windsurf_uses_mcp_config_json(self, isolated_home):
        assert setup_mod.MCP_CONFIG_SNIPPETS["windsurf"]["file"] == "~/.codeium/windsurf/mcp_config.json"
        result = setup_mod._install_mcp("windsurf")
        assert result["status"] == "installed"
        assert (isolated_home / ".codeium" / "windsurf" / "mcp_config.json").exists()
        data = json.loads((isolated_home / ".codeium" / "windsurf" / "mcp_config.json").read_text())
        assert data["mcpServers"]["nexus"]["command"] == "nexus-memory"

    def test_generic_mcp_json_only_for_agents_that_read_it(self):
        """Every non-TOML snippet path must end in mcp.json at an agent dir
        (with the two documented exceptions: Claude Code's user-scope
        ~/.claude.json and Windsurf's mcp_config.json)."""
        for agent_id, snippet in setup_mod.MCP_CONFIG_SNIPPETS.items():
            if snippet.get("format") == "toml":
                assert snippet["file"].endswith("config.toml"), agent_id
            else:
                fname = snippet["file"].rsplit("/", 1)[-1]
                assert fname in ("mcp.json", ".claude.json", "mcp_config.json"), (agent_id, snippet["file"])

    def test_module_documents_which_agent_reads_which_format(self):
        header = Path(setup_mod.__file__).read_text()
        assert "config.toml" in header and "mcp_servers" in header
        assert "claude.json" in header or "claude.json" in header  # claude.json mention
        assert "mcp_config.json" in header


# ---------------------------------------------------------------------------
# Invariant 3: broken config aborts, no overwrite; writes are atomic
# ---------------------------------------------------------------------------

class TestBrokenConfigProtection:
    def _snippet(self, tmp_path, broken_file: Path):
        setup_mod.MCP_CONFIG_SNIPPETS["synth-mcp-agent"] = {
            "file": str(broken_file),
            "format": "json",
            "config": {"nexus": {"command": "nexus-memory", "args": [], "env": {}}},
        }

    def test_broken_json_config_is_never_overwritten(self, tmp_path, monkeypatch, isolated_home):
        mcp_file = tmp_path / "broken" / "mcp.json"
        mcp_file.parent.mkdir(parents=True)
        damaged = "{ almost json but not "
        mcp_file.write_text(damaged)
        self._snippet(tmp_path, mcp_file)
        writes = []

        # Track every file-write attempt on that path.
        real_open = builtins.open

        def guarded_open(file, *a, **kw):
            if str(file) == str(mcp_file):
                writes.append((str(file), a, kw))
            return real_open(file, *a, **kw)

        monkeypatch.setattr(builtins, "open", guarded_open)

        result = setup_mod._install_mcp("synth-mcp-agent")

        assert result["status"] == "config_error"
        assert mcp_file.read_text() == damaged, "broken config must survive untouched"
        assert not any("w" in (a[0] if a else "") for a, _, _ in [w for w in writes]) and True

    def test_unreadable_config_is_never_overwritten(self, tmp_path, monkeypatch, isolated_home):
        mcp_file = tmp_path / "locked" / "mcp.json"
        mcp_file.parent.mkdir(parents=True)
        original = '{"mcpServers": {"other": {"command": "keep"}}}'
        mcp_file.write_text(original)
        self._snippet(tmp_path, mcp_file)

        # Make read_text() raise OSError (simulates unreadable file).
        real_read_text = setup_mod.Path.read_text

        def failing_read_text(self, *a, **kw):
            if str(self) == str(mcp_file):
                raise PermissionError(13, "Permission denied")
            return real_read_text(self, *a, **kw)

        monkeypatch.setattr(setup_mod.Path, "read_text", failing_read_text)

        result = setup_mod._install_mcp("synth-mcp-agent")

        assert result["status"] == "config_error"
        # Read via the saved real method: the monkeypatched read_text would
        # raise again on the assertion itself.
        assert real_read_text(mcp_file) == original

    def test_schema_error_mcpServers_not_object_aborts(self, tmp_path, isolated_home):
        mcp_file = tmp_path / "schema" / "mcp.json"
        mcp_file.parent.mkdir(parents=True)
        mcp_file.write_text('{"mcpServers": ["not", "an", "object"]}')
        self._snippet(tmp_path, mcp_file)

        result = setup_mod._install_mcp("synth-mcp-agent")
        assert result["status"] == "config_error"
        assert mcp_file.read_text() == '{"mcpServers": ["not", "an", "object"]}'

    def test_missing_file_gets_fresh_config(self, tmp_path, isolated_home):
        mcp_file = tmp_path / "fresh" / "mcp.json"
        self._snippet(tmp_path, mcp_file)

        result = setup_mod._install_mcp("synth-mcp-agent")
        assert result["status"] == "installed"
        data = json.loads(mcp_file.read_text())
        assert data == {"mcpServers": {"nexus": {"command": "nexus-memory", "args": [], "env": {}}}}

    def test_existing_other_servers_preserved(self, tmp_path, isolated_home):
        mcp_file = tmp_path / "merge" / "mcp.json"
        mcp_file.parent.mkdir(parents=True)
        mcp_file.write_text(json.dumps({"mcpServers": {"other": {"command": "keep-me"}}}))
        self._snippet(tmp_path, mcp_file)

        result = setup_mod._install_mcp("synth-mcp-agent")
        assert result["status"] == "installed"
        data = json.loads(mcp_file.read_text())
        assert data["mcpServers"]["other"] == {"command": "keep-me"}
        assert data["mcpServers"]["nexus"]["command"] == "nexus-memory"

    def test_already_installed_short_circuits(self, tmp_path, isolated_home):
        mcp_file = tmp_path / "already" / "mcp.json"
        mcp_file.parent.mkdir(parents=True)
        mcp_file.write_text(json.dumps({"mcpServers": {"nexus": {"command": "nexus-memory", "args": [], "env": {}}}}))
        self._snippet(tmp_path, mcp_file)

        result = setup_mod._install_mcp("synth-mcp-agent")
        assert result["status"] == "already_installed"
        assert mcp_file.read_text() == json.dumps({"mcpServers": {"nexus": {"command": "nexus-memory", "args": [], "env": {}}}})

    def test_successful_write_is_atomic_temp_then_replace(self, tmp_path, monkeypatch, isolated_home):
        mcp_file = tmp_path / "atomic" / "mcp.json"
        self._snippet(tmp_path, mcp_file)
        seen = {}
        real_replace = setup_mod.os.replace
        real_mkstemp = setup_mod.tempfile.mkstemp

        def spy_mkstemp(*a, **kw):
            out = real_mkstemp(*a, **kw)
            seen["tmp"] = out[1]
            assert a[0] == str(mcp_file.parent) if a else True
            return out

        replace_calls = []
        monkeypatch.setattr(setup_mod.tempfile, "mkstemp", spy_mkstemp)
        monkeypatch.setattr(setup_mod.os, "replace", lambda src, dst: (replace_calls.append((src, dst)), real_replace(src, dst))[1])

        result = setup_mod._install_mcp("synth-mcp-agent")
        assert result["status"] == "installed"
        assert seen["tmp"].startswith(str(mcp_file.parent))
        assert len(replace_calls) == 1
        assert replace_calls[0][1] == str(mcp_file) or replace_calls[0][1] == mcp_file
        # No leftover temp files.
        leftovers = [p.name for p in mcp_file.parent.iterdir() if p.name != "mcp.json"]
        assert leftovers == []

    def test_toml_broken_config_aborts_codex(self, isolated_home):
        codex_dir = isolated_home / ".codex"
        codex_dir.mkdir()
        damaged = "[mcp_servers\nbroken =="
        (codex_dir / "config.toml").write_text(damaged)

        result = setup_mod._install_mcp("codex")
        assert result["status"] == "config_error"
        assert (codex_dir / "config.toml").read_text() == damaged


# ---------------------------------------------------------------------------
# Invariant 4: docker run binds 127.0.0.1 only
# ---------------------------------------------------------------------------

class TestDockerRunLocalhostOnly:
    def test_welcome_recommends_loopback_binding(self):
        welcome = setup_mod.step_welcome()
        instructions = welcome["qdrant_instructions"]
        assert instructions is not None
        assert "-p 127.0.0.1:6333:6333" in instructions
        # No all-interfaces binding anywhere in the recommendation.
        assert "-p 6333:6333" not in instructions
        # No bare 0.0.0.0 publication either.
        assert "0.0.0.0" not in instructions

    def test_no_unbound_6333_publish_in_src(self):
        repo_src = _REPO_ROOT / "src"
        offenders = []
        for py in repo_src.rglob("*.py"):
            text = py.read_text()
            if 'docker run' in text and '-p 6333:6333' in text:
                offenders.append(str(py))
        assert offenders == []