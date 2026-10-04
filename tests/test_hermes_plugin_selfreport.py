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

def test_repair_command_uses_own_venv_never_host_interpreter():
    """Der Reparaturbefehl zielt auf das EIGENE venv, nie auf den Host.

    Der Host-Interpreter gehoert Hermes; bei pipx-/System-Installationen ist
    er fremdverwaltet. Ein Befehl gegen ``sys.executable`` wuerde bei
    Fremd-Usern Schaden anrichten (Auslieferungs-Regel).
    """
    cmd = nexus_plugin._repair_command()
    assert "uv venv" in cmd
    assert str(_REPO_ROOT) in cmd
    # Das eigene venv unter dem Datenverzeichnis ist das Ziel ...
    assert "plugin-venv" in cmd
    # ... und NIE der Interpreter des Host-Prozesses.
    assert sys.executable not in cmd


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


def test_plugin_venv_site_packages_rejects_foreign_python(tmp_path):
    """Ein venv einer ANDEREN Minor-Version wird abgelehnt (ABI-Schutz).

    CPython haengt ``lib/python3.14/site-packages`` klaglos in den Suchpfad,
    aber C-Erweiterungen (torch/numpy) brechen dann mit einem ABI-Fehler. Ein
    fremdes venv darf darum nie akzeptiert werden.
    """
    venv = tmp_path / "plugin-venv"
    foreign = venv / "lib" / "python2.7" / "site-packages"
    foreign.mkdir(parents=True)
    assert nexus_plugin._plugin_venv_site_packages(venv) is None

    # Das passende venv der laufenden Version wird akzeptiert.
    want = f"python{sys.version_info.major}.{sys.version_info.minor}"
    own = venv / "lib" / want / "site-packages"
    own.mkdir(parents=True)
    assert nexus_plugin._plugin_venv_site_packages(venv) == own


def test_repair_command_pins_the_python_version():
    """Der Reparaturbefehl fordert die laufende Minor-Version an."""
    cmd = nexus_plugin._repair_command()
    py_ver = f"{sys.version_info.major}.{sys.version_info.minor}"
    assert f"--python {py_ver}" in cmd


def test_health_probe_missing_dependency_never_calls_client(monkeypatch):
    """Ohne qdrant_client: kein Qdrant-Zugriff, aber das Urteil laeuft ab.

    Der Fehlerfall darf NICHT prozessweit eingefroren werden: nach Ablauf von
    ``_DEP_MISSING_TTL_SEC`` wird der Import neu versucht, damit eine
    Reparatur den laufenden Dienst heilt (04.10.2026).
    """
    monkeypatch.setattr(nexus_plugin, "QdrantClient", None)
    monkeypatch.setattr(nexus_plugin, "qmodels", None)
    monkeypatch.setattr(nexus_plugin, "_PROBE_CACHE", None)
    # Ein wirklich fehlendes Paket: der Re-Import-Versuch findet nichts.
    monkeypatch.setattr(nexus_plugin, "_try_import_qdrant", lambda: False)

    def boom():
        raise AssertionError("probe_once must not run without qdrant_client")

    monkeypatch.setattr(nexus_plugin, "_probe_once", boom)

    ok, cause = nexus_plugin._health_probe()
    assert ok is False
    assert "qdrant_client" in cause
    # Zweiter Aufruf innerhalb des TTL wird lokal bedient (kein Re-Import).
    assert nexus_plugin._health_probe()[0] is False

    # Nach Ablauf des TTL wird der Import erneut versucht -> Reparatur kann
    # den laufenden Prozess heilen, statt nur den naechsten.
    attempts: list = []

    def still_missing():
        attempts.append(1)
        return False

    monkeypatch.setattr(nexus_plugin, "_try_import_qdrant", still_missing)
    # Das Fehlurteil kuenstlich altern lassen.
    monkeypatch.setattr(nexus_plugin, "_PROBE_CACHE", (False, "stale", -1e12))
    assert nexus_plugin._health_probe()[0] is False
    assert attempts == [1]


def test_health_probe_recovers_when_dependency_appears(monkeypatch):
    """Die Kern-Lektion: ein nachinstalliertes Paket heilt den LAUFENDEN Prozess.

    Vorher war das Urteil prozessweit eingefroren (``float('inf')``) — nur ein
    Neustart half. Jetzt genuegt der naechste Re-Probe nach Ablauf des TTL.
    """
    monkeypatch.setattr(nexus_plugin, "QdrantClient", None)
    monkeypatch.setattr(nexus_plugin, "qmodels", None)
    monkeypatch.setattr(nexus_plugin, "_PROBE_CACHE", None)
    monkeypatch.setattr(nexus_plugin, "_try_import_qdrant", lambda: False)
    assert nexus_plugin._health_probe()[0] is False

    # "Reparatur": der Re-Import findet das Paket jetzt und traegt es ein.
    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def get_collections(self):
            return None

        def close(self):
            pass

    def import_now():
        monkeypatch.setattr(nexus_plugin, "QdrantClient", FakeClient)
        monkeypatch.setattr(nexus_plugin, "qmodels", object())
        return True

    monkeypatch.setattr(nexus_plugin, "_try_import_qdrant", import_now)
    monkeypatch.setattr(nexus_plugin, "_PROBE_CACHE", (False, "stale", -1e12))

    # Kein Neustart, kein neuer Prozess: derselbe Prozess ist wieder gesund.
    assert nexus_plugin._health_probe()[0] is True


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


# ─────────────────────────────────────────────────────────────────────────────
# 10: chat-visible warning via the transform_llm_output hook
# ─────────────────────────────────────────────────────────────────────────────

class _HookCtx:
    """Minimal Hermes context that only records hook registrations."""

    def __init__(self) -> None:
        self.hooks: list = []

    def register_hook(self, hook_name, callback) -> None:
        self.hooks.append((hook_name, callback))


def _reset_warning_state(monkeypatch):
    """Clear the throttle cache and the once-per-process guard for a test."""
    monkeypatch.setattr(nexus_plugin, "_warned_sessions",
                        nexus_plugin.OrderedDict())
    monkeypatch.setattr(nexus_plugin, "_output_warning_registered", False)


def test_output_hook_noop_when_healthy(monkeypatch):
    """A healthy probe must leave the answer untouched (None)."""
    _reset_warning_state(monkeypatch)
    monkeypatch.setattr(nexus_plugin, "_health_probe", lambda: (True, ""))
    assert nexus_plugin._on_llm_output("all good", "sess-a") is None


def test_output_hook_appends_cause_and_fix_when_broken(monkeypatch):
    """The appended block names the probe cause and the repair command."""
    _reset_warning_state(monkeypatch)
    monkeypatch.setattr(
        nexus_plugin, "_health_probe",
        lambda: (False, "Qdrant at localhost:6333 is unreachable"),
    )
    out = nexus_plugin._on_llm_output("Here is your answer.", "sess-b")
    assert out is not None
    assert out.startswith("Here is your answer.")
    assert "Qdrant at localhost:6333 is unreachable" in out
    assert nexus_plugin._repair_command() in out
    assert "Your stored memories are safe" in out
    assert len(out.splitlines()) <= 7  # answer + short block


def test_output_hook_warns_once_per_session(monkeypatch):
    """A second turn for the same session_id must not warn again."""
    _reset_warning_state(monkeypatch)
    monkeypatch.setattr(
        nexus_plugin, "_health_probe",
        lambda: (False, "Qdrant down"),
    )
    ctx = _HookCtx()
    nexus_plugin._register_output_warning(ctx)
    nexus_plugin._on_llm_output("first answer", "sess-c")
    assert nexus_plugin._on_llm_output("second answer", "sess-c") is None
    assert nexus_plugin._on_llm_output(None, "sess-c") is None


def test_output_hook_warns_again_for_new_session(monkeypatch):
    """A fresh session_id warns again after the cap-bounded cache was used."""
    _reset_warning_state(monkeypatch)
    monkeypatch.setattr(nexus_plugin, "_health_probe",
                        lambda: (False, "Qdrant down"))
    nexus_plugin._on_llm_output("answer", "sess-d1")
    assert nexus_plugin._on_llm_output("answer", "sess-d2") is not None
    assert nexus_plugin._on_llm_output("answer", "sess-d3") is not None


def test_output_hook_fail_open_when_probe_raises(monkeypatch):
    """An exploding probe must return None, never propagate, never warn."""
    _reset_warning_state(monkeypatch)

    def boom():
        raise RuntimeError("probe exploded")

    monkeypatch.setattr(nexus_plugin, "_health_probe", boom)
    assert nexus_plugin._on_llm_output("answer", "sess-e") is None
    assert "sess-e" not in nexus_plugin._warned_sessions


def test_output_hook_fail_open_on_empty_or_missing_session(monkeypatch):
    """Empty/None text is a no-op; the throttle also tolerates an empty id."""
    _reset_warning_state(monkeypatch)
    monkeypatch.setattr(nexus_plugin, "_health_probe",
                        lambda: (False, "Qdrant down"))
    assert nexus_plugin._on_llm_output("", "sess-f") is None
    assert nexus_plugin._on_llm_output(None, "sess-f") is None
    assert nexus_plugin._on_llm_output(12345, "sess-f") is None
    # Missing session_id kwarg: hook still works, throttled under "".
    first = nexus_plugin._on_llm_output("answer")
    assert first is not None and "Qdrant down" in first
    assert nexus_plugin._on_llm_output("answer") is None


def test_output_hook_warn_cache_is_bounded(monkeypatch):
    """The throttle cache must never grow beyond the cap (64 entries)."""
    _reset_warning_state(monkeypatch)
    monkeypatch.setattr(nexus_plugin, "_health_probe",
                        lambda: (False, "Qdrant down"))
    for i in range(nexus_plugin._OUTPUT_WARN_SESSION_CAP + 20):
        nexus_plugin._on_llm_output("answer", f"sess-{i}")
    assert len(nexus_plugin._warned_sessions) <= nexus_plugin._OUTPUT_WARN_SESSION_CAP


def test_register_registers_transform_llm_output_hook(monkeypatch, tmp_path):
    """register() must wire the chat warning hook beside the prompt section."""
    _reset_warning_state(monkeypatch)
    monkeypatch.setattr(nexus_plugin, "_selfcheck_path",
                        lambda: tmp_path / "agent-selfcheck.json")
    monkeypatch.setattr(nexus_plugin, "_health_probe", lambda: (True, ""))
    monkeypatch.setattr(nexus_plugin, "_status_section_registered", False)

    class _FullCtx:
        def __init__(self) -> None:
            self.providers: list = []
            self.hooks: list = []

        def register_memory_provider(self, provider) -> None:
            self.providers.append(provider)

        def register_system_prompt_section(self, **kwargs) -> None:
            pass

        def register_hook(self, hook_name, callback) -> None:
            self.hooks.append((hook_name, callback))

    ctx = _FullCtx()
    nexus_plugin.register(ctx)
    registered = [name for name, cb in ctx.hooks]
    assert "transform_llm_output" in registered
    # Exactly once per process: a second register() call must not re-add it.
    registered_after = [name for name, cb in ctx.hooks]
    assert registered_after.count("transform_llm_output") == 1
    # The recorded callback is the hook itself, and the hook is fail-open.
    callback = next(cb for name, cb in ctx.hooks
                    if name == "transform_llm_output")
    assert callback is nexus_plugin._on_llm_output
    assert callback("hello", "sess-g") is None  # healthy probe -> no-op


def test_register_output_warning_survives_missing_hook_api(monkeypatch, tmp_path):
    """An older Hermes without register_hook must not break registration."""
    _reset_warning_state(monkeypatch)
    monkeypatch.setattr(nexus_plugin, "_selfcheck_path",
                        lambda: tmp_path / "agent-selfcheck.json")
    monkeypatch.setattr(nexus_plugin, "_health_probe", lambda: (True, ""))
    monkeypatch.setattr(nexus_plugin, "_status_section_registered", False)
    monkeypatch.setattr(nexus_plugin, "_output_warning_registered", False)

    class _NoHookCtx:
        def __init__(self) -> None:
            self.providers: list = []

        def register_memory_provider(self, provider) -> None:
            self.providers.append(provider)

        def register_system_prompt_section(self, **kwargs) -> None:
            pass

        # No register_hook attribute at all: the guard must swallow the
        # AttributeError and keep the provider registration intact.
    ctx = _NoHookCtx()
    nexus_plugin.register(ctx)  # must not raise
    assert len(ctx.providers) == 1
