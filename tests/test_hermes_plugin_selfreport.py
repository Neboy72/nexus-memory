"""Tests for the Hermes plugin self-report path (NexusMemoryProvider).

Regression cover for the incident where a Hermes update replaced its managed
venv, dropped ``qdrant_client``, and the plugin module failed to import — the
provider was silently dropped for days. These tests pin the four guarantees:
the module always imports, it explains why it is broken, it surfaces a visible
warning into the agent's prompt, and it names a one-line fix.

The plugin module is loaded point-blank from its file path (the same trick as
``test_hermes_plugin.py``) to avoid colliding with the top-level ``nexus``
package at the repo root.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC = str(_REPO_ROOT / "src")
_PLUGIN_PATH = _REPO_ROOT / "plugins" / "memory" / "nexus" / "__init__.py"

# The plugin's own internal imports need nexus_memory.* on the path.
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

_spec = importlib.util.spec_from_file_location("nexus_hermes_plugin_selfreport", str(_PLUGIN_PATH))
assert _spec is not None and _spec.loader is not None
nexus_plugin = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(nexus_plugin)


# ---------------------------------------------------------------------------
# Subprocess helpers: simulate the exact failure mode (qdrant_client missing)
# ---------------------------------------------------------------------------

# A meta_path finder that makes `import qdrant_client` raise ImportError, then
# loads the plugin file. The import must still SUCCEED because the guarded
# import catches the missing dependency.
_BLOCK_IMPORT_PROLOGUE = r'''
import importlib.util, sys

class _BlockQdrant:
    def find_spec(self, name, path=None, target=None):
        if name == "qdrant_client" or name.startswith("qdrant_client."):
            raise ImportError("qdrant_client blocked by test finder")
        return None

sys.meta_path.insert(0, _BlockQdrant())
plugin_path = sys.argv[1]
_spec = importlib.util.spec_from_file_location("nexus_hermes_plugin_blocked", plugin_path)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)  # must NOT raise — this is the incident
assert mod.QdrantClient is None, "guarded import should leave QdrantClient as None"
assert mod._QDRANT_IMPORT_ERROR, "guard must record the import failure"
'''

_SCRIPT_IMPORT_OK = _BLOCK_IMPORT_PROLOGUE + r'''
p = mod.NexusMemoryProvider()
assert p.is_available() is False
print("OK")
'''

_SCRIPT_REASON = _BLOCK_IMPORT_PROLOGUE + r'''
p = mod.NexusMemoryProvider()
reason = p.unavailable_reason()
assert reason, "unavailable_reason must not be empty when broken"
assert "qdrant_client" in reason, reason
assert "pip install" in reason, reason
print("OK")
'''


def _run_blocked_script(script: str) -> subprocess.CompletedProcess:
    """Run a script in a fresh interpreter with qdrant_client import blocked."""
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)  # a polluted PYTHONPATH breaks third-party imports
    return subprocess.run(
        [sys.executable, "-c", script, str(_PLUGIN_PATH)],
        cwd=str(_REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


# ---------------------------------------------------------------------------
# 1 + 2: guarded import / unavailable_reason
# ---------------------------------------------------------------------------

def test_module_imports_without_qdrant_client():
    """The module must import even when qdrant_client is missing (the incident)."""
    proc = _run_blocked_script(_SCRIPT_IMPORT_OK)
    assert proc.returncode == 0, f"stdout={proc.stdout!r}\nstderr={proc.stderr!r}"
    assert "OK" in proc.stdout


def test_unavailable_reason_mentions_missing_package_and_fix():
    """The reason must name the missing package and carry a fix command."""
    proc = _run_blocked_script(_SCRIPT_REASON)
    assert proc.returncode == 0, f"stdout={proc.stdout!r}\nstderr={proc.stderr!r}"
    assert "OK" in proc.stdout


# ---------------------------------------------------------------------------
# 3: repair command
# ---------------------------------------------------------------------------

def test_repair_command_uses_uv_for_checkout():
    """A source checkout/editable install is repaired against the active interpreter."""
    cmd = nexus_plugin._repair_command()
    assert "uv pip install -e" in cmd
    assert str(_REPO_ROOT) in cmd
    assert sys.executable in cmd


# ---------------------------------------------------------------------------
# 4 + 5: system-prompt self-report section
# ---------------------------------------------------------------------------

def test_status_section_empty_when_healthy(monkeypatch):
    """A healthy provider adds zero prompt clutter."""
    monkeypatch.setattr(nexus_plugin, "_health_probe", lambda: (True, ""))
    assert nexus_plugin._status_section_content() == ""


def test_status_section_warns_when_broken(monkeypatch):
    """A broken provider surfaces cause, fix and the tell-the-user instruction."""
    monkeypatch.setattr(
        nexus_plugin, "_health_probe",
        lambda: (False, "Qdrant at localhost:6333 is unreachable"),
    )
    out = nexus_plugin._status_section_content()
    assert "NOT WORKING" in out
    assert nexus_plugin._repair_command() in out
    assert "Tell your user" in out
    assert "safe and not lost" in out


# ---------------------------------------------------------------------------
# 6: register is fail-open around the (optional) section API
# ---------------------------------------------------------------------------

class _FakeCtx:
    """Minimal Hermes context: provider registration works, section API blows up."""

    def __init__(self) -> None:
        self.providers: list = []

    def register_memory_provider(self, provider) -> None:
        self.providers.append(provider)

    def register_system_prompt_section(self, **kwargs) -> None:
        raise RuntimeError("older Hermes without the section API")


def test_register_never_raises_without_section_api(tmp_path, monkeypatch):
    """A failing section registration must not prevent provider registration."""
    monkeypatch.setattr(nexus_plugin, "_selfcheck_path",
                        lambda: tmp_path / "agent-selfcheck.json")
    monkeypatch.setattr(nexus_plugin, "_health_probe", lambda: (True, ""))
    monkeypatch.setattr(nexus_plugin, "_status_section_registered", False)

    ctx = _FakeCtx()
    nexus_plugin.register(ctx)  # must not raise

    assert len(ctx.providers) == 1
    assert type(ctx.providers[0]).__name__ == "NexusMemoryProvider"


# ---------------------------------------------------------------------------
# 7: watchdog self-check file
# ---------------------------------------------------------------------------

def test_agent_selfcheck_file_written(tmp_path, monkeypatch):
    """write_agent_selfcheck writes the expected JSON and is fail-open."""
    target = tmp_path / "nested" / "agent-selfcheck.json"
    monkeypatch.setattr(nexus_plugin, "_selfcheck_path", lambda: target)
    monkeypatch.setenv("NEXUS_AGENT_ID", "test-agent")

    nexus_plugin.write_agent_selfcheck(False, "boom", "fix-cmd")

    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["agent_id"] == "test-agent"
    assert data["ok"] is False
    assert data["reason"] == "boom"
    assert data["fix"] == "fix-cmd"
    assert data["interpreter"] == sys.executable
    assert {"agent_id", "ok", "reason", "fix", "interpreter",
            "plugin_version", "ts"} <= set(data)

    # A failing write (unwritable location) must never raise.
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setattr(nexus_plugin, "_selfcheck_path",
                        lambda: blocker / "agent-selfcheck.json")
    nexus_plugin.write_agent_selfcheck(True, "", "")  # must not raise


# ---------------------------------------------------------------------------
# 8: module import survives a malformed NEXUS_QDRANT_PORT
# ---------------------------------------------------------------------------

def test_import_survives_malformed_port(monkeypatch):
    """A non-numeric port must not raise at import (the incident class)."""
    monkeypatch.setenv("NEXUS_QDRANT_PORT", "not-a-number")
    spec = importlib.util.spec_from_file_location(
        "nexus_hermes_plugin_badport", str(_PLUGIN_PATH))
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # must NOT raise
    assert mod._PORT == 6333


def test_env_int_bounded_clamps_and_defaults(monkeypatch):
    monkeypatch.setenv("NEXUS_TEST_PORT", "0")
    assert nexus_plugin._env_int_bounded("NEXUS_TEST_PORT", 6333, 1, 65535) == 1
    monkeypatch.setenv("NEXUS_TEST_PORT", "70000")
    assert nexus_plugin._env_int_bounded("NEXUS_TEST_PORT", 6333, 1, 65535) == 65535
    monkeypatch.setenv("NEXUS_TEST_PORT", "abc")
    assert nexus_plugin._env_int_bounded("NEXUS_TEST_PORT", 6333, 1, 65535) == 6333


# ---------------------------------------------------------------------------
# 9: the health probe is cached and bounded (per-turn prompt build)
# ---------------------------------------------------------------------------

def test_health_probe_caches_within_ttl(monkeypatch):
    calls: list = []

    class FakeClient:
        def __init__(self, *args, **kwargs):
            calls.append(kwargs)

        def get_collections(self):
            return None

        def close(self):
            pass

    monkeypatch.setattr(nexus_plugin, "QdrantClient", FakeClient)
    monkeypatch.setattr(nexus_plugin, "qmodels", object())
    monkeypatch.setattr(nexus_plugin, "_PROBE_CACHE", None)
    monkeypatch.setattr(nexus_plugin, "_PROBE_TTL_SEC", 30.0)

    assert nexus_plugin._health_probe()[0] is True
    assert nexus_plugin._health_probe()[0] is True
    assert len(calls) == 1  # second probe served from the TTL cache
    assert calls[0].get("timeout") == nexus_plugin._QDRANT_TIMEOUT_SEC

    # Force the cached timestamp far into the past -> a second real probe.
    monkeypatch.setattr(nexus_plugin, "_PROBE_CACHE", (True, "", -1e12))
    nexus_plugin._health_probe()
    assert len(calls) == 2


def test_health_probe_missing_dependency_never_calls_client(monkeypatch):
    monkeypatch.setattr(nexus_plugin, "QdrantClient", None)
    monkeypatch.setattr(nexus_plugin, "qmodels", object())
    monkeypatch.setattr(nexus_plugin, "_PROBE_CACHE", None)

    def boom():
        raise AssertionError("probe_once must not run without qdrant_client")

    monkeypatch.setattr(nexus_plugin, "_probe_once", boom)

    ok, cause = nexus_plugin._health_probe()
    assert ok is False
    assert "qdrant_client" in cause
    # Cached for the whole process — a second call is also served locally.
    assert nexus_plugin._health_probe()[0] is False


# ---------------------------------------------------------------------------
# 9b: plugin and daemon agree on the data directory
# ---------------------------------------------------------------------------

def test_selfcheck_path_honours_data_dir(tmp_path, monkeypatch):
    from nexus_memory import self_report as sr

    monkeypatch.setenv("NEXUS_DATA_DIR", str(tmp_path))
    path = nexus_plugin._selfcheck_path()
    assert path.parent == sr.data_dir() == tmp_path
    assert path.name.startswith("agent-selfcheck-")


def test_selfcheck_path_defaults_to_home(tmp_path, monkeypatch):
    from nexus_memory import self_report as sr

    monkeypatch.delenv("NEXUS_DATA_DIR", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    path = nexus_plugin._selfcheck_path()
    assert path.parent == tmp_path / ".nexus-memory"
    assert sr.data_dir() == tmp_path / ".nexus-memory"


# ---------------------------------------------------------------------------
# 9c: initialize() failure refreshes the self-check to ok: false
# ---------------------------------------------------------------------------

def test_initialize_failure_writes_false_selfcheck(tmp_path, monkeypatch):
    target = tmp_path / "agent-selfcheck-x.json"
    monkeypatch.setattr(nexus_plugin, "_selfcheck_path", lambda: target)

    def boom(*args, **kwargs):
        raise RuntimeError("qdrant down")

    monkeypatch.setattr(nexus_plugin, "QdrantClient", boom)
    monkeypatch.setattr(nexus_plugin, "qmodels", object())

    provider = nexus_plugin.NexusMemoryProvider()
    with pytest.raises(RuntimeError):
        provider.initialize("sess", hermes_home=str(tmp_path))

    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["ok"] is False
    assert data["reason"]
    assert "RuntimeError" in data["reason"]


def test_refresh_selfcheck_is_throttled(monkeypatch):
    written: list = []
    monkeypatch.setattr(nexus_plugin, "_health_probe", lambda: (True, ""))
    monkeypatch.setattr(
        nexus_plugin, "write_agent_selfcheck",
        lambda ok, reason, fix: written.append(ok),
    )
    monkeypatch.setattr(nexus_plugin, "_last_selfcheck_refresh", 0.0)
    clock = [1000.0]
    monkeypatch.setattr(nexus_plugin.time, "monotonic", lambda: clock[0])

    nexus_plugin._refresh_selfcheck_if_needed()
    nexus_plugin._refresh_selfcheck_if_needed()
    assert len(written) == 1  # throttled within 60 s

    clock[0] += 61
    nexus_plugin._refresh_selfcheck_if_needed()
    assert len(written) == 2


# ---------------------------------------------------------------------------
# 9d: atomic self-check write cleans up its temp file on failure
# ---------------------------------------------------------------------------

def test_selfcheck_temp_file_cleaned_on_replace_failure(tmp_path, monkeypatch):
    target = tmp_path / "agent-selfcheck-x.json"
    monkeypatch.setattr(nexus_plugin, "_selfcheck_path", lambda: target)

    def boom(*args, **kwargs):
        raise OSError("replace failed")

    monkeypatch.setattr(nexus_plugin.os, "replace", boom)
    nexus_plugin.write_agent_selfcheck(False, "r", "f")  # fail-open
    assert list(tmp_path.glob("*.tmp-*")) == []


# ---------------------------------------------------------------------------
# 9e: API-bound text masks absolute paths and is length-capped
# ---------------------------------------------------------------------------

def test_mask_paths_keeps_file_name():
    assert nexus_plugin._mask_paths("/a/b/c.py") == "<path>/c.py"


def test_status_section_masks_paths_and_caps(monkeypatch):
    cause = "/Users/someone/secret/place/qdrant.py: " + "x" * 3000
    monkeypatch.setattr(nexus_plugin, "_health_probe", lambda: (False, cause))
    out = nexus_plugin._status_section_content()
    assert "/Users/someone" not in out
    assert len(out) <= 1000
    assert "NOT WORKING" in out


def test_unavailable_reason_masks_cause_paths(monkeypatch):
    monkeypatch.setattr(
        nexus_plugin, "_health_probe",
        lambda: (False, "failed at /Users/someone/private/x.py"),
    )
    reason = nexus_plugin.NexusMemoryProvider().unavailable_reason()
    assert "/Users/someone" not in reason
    assert "<path>/x.py" in reason
