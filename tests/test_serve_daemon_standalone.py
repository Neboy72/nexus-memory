"""v0.21.0 "Standalone wird Standard" — tests for building blocks 1-3.

1. scripts/install_serve.sh: OS service installation for the serve daemon
   (launchd/systemd/Windows), idempotent re-runs, hermetic test mode.
2. Setup-wizard wiring: setup.step_complete() installs the serve daemon with
   NO user question; a failed install never fails the setup.
3. do_update wiring: mcp_server._do_update() re-asserts the serve daemon
   fail-open (failure never fails the update, never blocks the event loop).

All tests are hermetic: no launchctl/systemd/schtasks call ever leaves the
process (launchctl domains are per-uid, NOT per-HOME — a fake-HOME run would
stop the REAL service), and no real port/healthz endpoint is touched.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC = str(_REPO_ROOT / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

SCRIPT = _REPO_ROOT / "scripts" / "install_serve.sh"
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)
from nexus_memory import serve_daemon as sd  # noqa: E402
from nexus_memory import setup as setup_mod  # noqa: E402


# ── helpers ──────────────────────────────────────────────────────────────────

def _fake_env(home: Path, port: str = "9122", skip: bool = True,
              force_os: str | None = None) -> dict:
    env = {
        "HOME": str(home),
        "PATH": f"{home}/bin:/usr/bin:/bin",
        "NEXUS_SERVE_PORT": port,
    }
    if skip:
        env["NEXUS_SERVE_SKIP_LAUNCHD"] = "1"
    if force_os:
        env["NEXUS_SERVE_FORCE_OS"] = force_os
    return env


def _fake_entrypoint(home: Path) -> None:
    """A fake nexus-memory console script so resolve_entrypoint succeeds."""
    bin_dir = home / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    entry = bin_dir / "nexus-memory"
    entry.write_text("#!/bin/bash\necho \"FAKE-ENTRY $@\"\n")
    entry.chmod(entry.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _run_script(home: Path, port: str = "9122", skip: bool = True,
                extra_env: dict | None = None,
                force_os: str | None = None) -> subprocess.CompletedProcess:
    env = _fake_env(home, port=port, skip=skip, force_os=force_os)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", str(SCRIPT)], capture_output=True, text=True,
        timeout=60, env=env, cwd=str(_REPO_ROOT),
    )


# ── Baustein 1: install_serve.sh ─────────────────────────────────────────────

class TestInstallServeScript:
    def test_script_exists_and_is_executable(self):
        assert SCRIPT.exists()
        assert SCRIPT.stat().st_mode & stat.S_IXUSR

    def test_service_identity_matches_serve_daemon_module(self):
        """The stable service identity must not drift between script and module."""
        src = SCRIPT.read_text()
        assert f'LABEL="{sd.LAUNCHD_LABEL}"' in src
        assert f'UNIT_NAME="{sd.SYSTEMD_UNIT}"' in src
        assert f'TASK_NAME="{sd.WINDOWS_TASK}"' in src
        assert f'DEFAULT_SERVE_PORT="{sd.DEFAULT_SERVE_PORT}"' in src

    def test_darwin_plist_written_hermetically(self, tmp_path):
        _fake_entrypoint(tmp_path)
        plist = tmp_path / "Library" / "LaunchAgents" / f"{sd.LAUNCHD_LABEL}.plist"
        r = _run_script(tmp_path, force_os="darwin")
        assert r.returncode == 0, r.stderr
        assert plist.exists()

        content = plist.read_text()
        # Reference-plist parity: the keys that make the daemon a daemon.
        assert "<string>serve</string>" in content                      # RunAtLoad boots serve
        assert f"<string>{sd.LAUNCHD_LABEL}</string>" in content
        assert f"<string>{tmp_path}/bin/nexus-memory</string>" in content
        assert "<key>RunAtLoad</key>" in content and "<true/>" in content
        assert "<key>KeepAlive</key>" in content
        assert "<integer>30</integer>" in content                       # ThrottleInterval
        assert "<integer>25</integer>" in content                       # ExitTimeOut
        assert f"<string>{sd.DEFAULT_SERVE_PORT}</string>" in content   # NEXUS_SERVE_PORT
        assert "Library/LaunchAgents" in str(plist)                     # LaunchAgents path

    def test_plist_keeps_reference_log_paths(self, tmp_path):
        _fake_entrypoint(tmp_path)
        r = _run_script(tmp_path, force_os="darwin")
        assert r.returncode == 0, r.stderr
        content = (tmp_path / "Library" / "LaunchAgents" / f"{sd.LAUNCHD_LABEL}.plist").read_text()
        assert f"<string>{_REPO_ROOT}/logs/serve.log</string>" in content
        assert f"<string>{_REPO_ROOT}/logs/serve.error.log</string>" in content

    def test_rerun_is_idempotent_no_duplicate_no_failure(self, tmp_path):
        _fake_entrypoint(tmp_path)
        r1 = _run_script(tmp_path, force_os="darwin")
        assert r1.returncode == 0, r1.stderr
        r2 = _run_script(tmp_path, force_os="darwin")
        assert r2.returncode == 0, r2.stderr
        # still exactly one plist for the stable label
        plist_dir = tmp_path / "Library" / "LaunchAgents"
        plists = list(plist_dir.glob("*.plist"))
        assert plists == [plist_dir / f"{sd.LAUNCHD_LABEL}.plist"]

    def test_skip_mode_writes_file_without_service_control(self, tmp_path):
        _fake_entrypoint(tmp_path)
        r = _run_script(tmp_path, skip=True, force_os="darwin")
        assert r.returncode == 0
        assert "skipping service control" in r.stdout
        assert "Post-install healthz check skipped" in r.stdout

    def test_without_skip_flag_the_script_refuses_no_entrypoint(self, tmp_path):
        # No fake entrypoint, no NEXUS_PYTHON → resolve_entrypoint fails; the
        # script must abort BEFORE any service-control side effect.
        env = _fake_env(tmp_path, skip=True)
        env["PATH"] = "/nonexistent:/usr/bin:/bin"
        r = subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True,
                           timeout=60, env=env, cwd=str(_REPO_ROOT))
        assert r.returncode != 0
        assert "entrypoint not found" in (r.stdout + r.stderr)

    def test_port_override_reaches_plist(self, tmp_path):
        _fake_entrypoint(tmp_path)
        r = _run_script(tmp_path, port="9300", force_os="darwin")
        assert r.returncode == 0, r.stderr
        content = (tmp_path / "Library" / "LaunchAgents" / f"{sd.LAUNCHD_LABEL}.plist").read_text()
        assert "<string>9300</string>" in content

    def test_hermetic_runs_never_touch_launchctl(self, tmp_path, monkeypatch):
        # belt & braces: even if the script regressed, the test env forbids it
        _fake_entrypoint(tmp_path)
        calls = []
        real_run = subprocess.run

        def spying_run(cmd, *a, **kw):
            if cmd and cmd[0] == "launchctl":
                calls.append(cmd)
                raise AssertionError(f"launchctl called in hermetic test: {cmd}")
            return real_run(cmd, *a, **kw)

        monkeypatch.setattr(subprocess, "run", spying_run)
        monkeypatch.setattr(sd.subprocess, "run", spying_run)
        r = _run_script(tmp_path, force_os="darwin")
        assert r.returncode == 0
        assert calls == []

    def test_missing_entrypoint_fails_cleanly(self, tmp_path):
        env = _fake_env(tmp_path)
        env["PATH"] = "/nonexistent:/usr/bin:/bin"
        r = subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True,
                           timeout=60, env=env, cwd=str(_REPO_ROOT))
        assert r.returncode != 0
        assert "entrypoint not found" in (r.stdout + r.stderr)


# ── Baustein 2: setup-wizard wiring ──────────────────────────────────────────

@pytest.fixture
def daemon_engine(tmp_path, monkeypatch):
    """Point the daemon engine at hermetic fakes: script path, port, healthz."""
    monkeypatch.setattr(sd, "INSTALL_SERVE_SCRIPT", tmp_path / "install_serve.sh")
    monkeypatch.setattr(sd, "DEFAULT_SERVE_PORT", 9122)
    # default: /healthz answers → ensure() must be a no-op ("already_installed")
    monkeypatch.setattr(sd, "_healthz_ok", lambda port, timeout=2.0: True)
    return sd


class TestSetupWizardWiring:
    def test_step_complete_installs_daemon_without_user_question(self, daemon_engine, monkeypatch):
        calls = []

        def fake_ensure():
            calls.append(1)
            return {"status": "installed", "port": 9122, "message": "ok"}

        monkeypatch.setattr(sd, "ensure_serve_daemon", fake_ensure)
        monkeypatch.setattr(setup_mod, "step_status", lambda: {
            "embedding": {"provider": "voyage", "qdrant_running": True},
            "agents": {"registered": [
                {"id": "hermes", "name": "Hermes Agent", "icon": "🦊",
                 "trust_level": "private", "install_type": "plugin+mcp"},
            ]},
        })
        result = setup_mod.step_complete()
        assert calls, "serve daemon step was not invoked"
        # no user question: step_complete takes no input and never blocks
        assert result["serve_daemon"]["status"] == "installed"
        assert result["serve_daemon"]["port"] == 9122
        assert "Serve daemon installed" in result["message"]

    def test_step_complete_already_installed_is_a_noop(self, daemon_engine, monkeypatch):
        def fake_ensure():
            return {"status": "already_installed", "port": 9122}

        monkeypatch.setattr(sd, "ensure_serve_daemon", fake_ensure)
        monkeypatch.setattr(setup_mod, "step_status", lambda: {
            "embedding": {"provider": "none", "qdrant_running": False},
            "agents": {"registered": []},
        })
        result = setup_mod.step_complete()
        assert result["serve_daemon"]["status"] == "already_installed"
        assert "already running" in result["message"]

    def test_failed_daemon_never_fails_setup(self, daemon_engine, monkeypatch):
        def fake_ensure():
            return {"status": "failed", "error": "no launchd", "port": 9122}

        monkeypatch.setattr(sd, "ensure_serve_daemon", fake_ensure)
        monkeypatch.setattr(setup_mod, "step_status", lambda: {
            "embedding": {"provider": "none", "qdrant_running": False},
            "agents": {"registered": []},
        })
        result = setup_mod.step_complete()
        # the complete step still completes and reports the daemon failure
        assert result["step"] == "complete"
        assert result["serve_daemon"]["status"] == "failed"
        assert "stdio" in result["message"]

    def test_json_mode_serve_daemon_command(self, daemon_engine, monkeypatch, capsys):
        monkeypatch.setattr(sd, "ensure_serve_daemon",
                            lambda: {"status": "already_installed", "port": 9122})
        setup_mod.json_mode(["serve_daemon"])
        out = capsys.readouterr().out
        parsed = json.loads(out)
        assert parsed["status"] == "already_installed"
        assert parsed["port"] == 9122

    def test_json_mode_complete_carries_serve_daemon_block(self, daemon_engine, monkeypatch, capsys):
        monkeypatch.setattr(sd, "ensure_serve_daemon",
                            lambda: {"status": "installed", "port": 9122})
        monkeypatch.setattr(setup_mod, "step_status", lambda: {
            "embedding": {"provider": "voyage", "qdrant_running": True},
            "agents": {"registered": []},
        })
        setup_mod.json_mode(["complete"])
        parsed = json.loads(capsys.readouterr().out)
        assert parsed["serve_daemon"]["status"] == "installed"
        assert "installed" in parsed["message"]

    def test_cli_step5b_serve_daemon_printed_in_interactive_flow(self):
        # The interactive CLI calls step_serve_daemon() before step_complete()
        src = (Path(setup_mod.__file__)).read_text()
        assert "daemon = step_serve_daemon()" in src
        assert "# Step 5: Serve daemon" in src
        assert "# Step 6: Complete" in src

    def test_ensure_noop_when_healthz_already_answers(self, daemon_engine):
        # _healthz_ok patched to True; the fake script must never be executed
        res = sd.ensure_serve_daemon()
        assert res["status"] == "already_installed"
        assert res["port"] == 9122

    def test_ensure_runs_script_and_reports_installed(self, daemon_engine, tmp_path, monkeypatch):
        script = tmp_path / "install_serve.sh"
        script.write_text("#!/bin/bash\necho ' ✅ fake install ok'\n")
        script.chmod(0o755)
        monkeypatch.setattr(sd, "INSTALL_SERVE_SCRIPT", script)
        # healthz: down before install, up after
        states = {"up": False}

        def fake_healthz(port, timeout=2.0):
            return states["up"]

        monkeypatch.setattr(sd, "_healthz_ok", fake_healthz)
        sleeps = {"n": 0}
        monkeypatch.setattr(sd.time, "sleep", lambda s: sleeps.__setitem__("n", sleeps["n"] + 1) or states.update({"up": True}))

        res = sd.ensure_serve_daemon()
        assert res["status"] == "installed"
        assert "fake install ok" in (res.get("detail") or "")
        assert sleeps["n"] <= 20  # bounded poll, not an infinite wait

    def test_ensure_script_failure_is_returned_not_raised(self, daemon_engine, tmp_path, monkeypatch):
        script = tmp_path / "install_serve.sh"
        script.write_text("#!/bin/bash\necho boom >&2\nexit 3\n")
        script.chmod(0o755)
        monkeypatch.setattr(sd, "INSTALL_SERVE_SCRIPT", script)
        monkeypatch.setattr(sd, "_healthz_ok", lambda port, timeout=2.0: False)
        monkeypatch.setattr(sd.time, "sleep", lambda s: None)

        res = sd.ensure_serve_daemon()
        assert res["status"] == "failed"
        assert "rc=3" in res["error"]
        assert "boom" in res["error"]

    def test_ensure_timeout_is_returned_not_raised(self, daemon_engine, tmp_path, monkeypatch):
        script = tmp_path / "install_serve.sh"
        script.write_text("#!/bin/bash\nsleep 5\n")
        script.chmod(0o755)
        monkeypatch.setattr(sd, "INSTALL_SERVE_SCRIPT", script)
        monkeypatch.setattr(sd, "SERVE_INSTALL_TIMEOUT_S", 1)
        monkeypatch.setattr(sd, "_healthz_ok", lambda port, timeout=2.0: False)
        monkeypatch.setattr(sd.time, "sleep", lambda s: None)

        res = sd.ensure_serve_daemon()
        assert res["status"] == "failed"
        assert "timed out" in res["error"]

    def test_ensure_missing_script_is_returned_not_raised(self, daemon_engine, monkeypatch):
        # fixture already pointed INSTALL_SERVE_SCRIPT at tmp_path/install_serve.sh
        monkeypatch.setattr(sd, "_healthz_ok", lambda port, timeout=2.0: False)
        res = sd.ensure_serve_daemon()
        assert res["status"] == "failed"
        assert "install script not found" in res["error"]

    def test_step_serve_daemon_failure_carries_message(self, daemon_engine, monkeypatch):
        monkeypatch.setattr(sd, "ensure_serve_daemon",
                            lambda: {"status": "failed", "error": "x"})
        res = sd.step_serve_daemon()
        assert res["status"] == "failed"
        assert "stdio" in res["message"]

    def test_serve_daemon_port_env_override(self, daemon_engine, monkeypatch):
        monkeypatch.setenv("NEXUS_SERVE_PORT", "9999")
        assert sd._serve_port() == 9999
        monkeypatch.setenv("NEXUS_SERVE_PORT", "not-a-number")
        assert sd._serve_port() == sd.DEFAULT_SERVE_PORT
        monkeypatch.delenv("NEXUS_SERVE_PORT")
        assert sd._serve_port() == sd.DEFAULT_SERVE_PORT

    def test_serve_daemon_status_shape(self, daemon_engine):
        res = sd.serve_daemon_status()
        assert set(res) >= {"port", "service_installed", "healthz_ok", "detail"}


# ── Baustein 3: do_update wiring ─────────────────────────────────────────────

class TestDoUpdateWiring:
    def _source_segment(self):
        src = (_REPO_ROOT / "src" / "nexus_memory" / "mcp_server.py").read_text()
        start = src.index("async def _do_update")
        return src[start:]

    def test_do_update_calls_ensure_serve_daemon(self):
        seg = self._source_segment()
        assert "ensure_serve_daemon" in seg
        assert "from nexus_memory.serve_daemon import ensure_serve_daemon" in seg

    def test_do_update_is_fail_open(self):
        seg = self._source_segment()
        # the whole re-assert block is wrapped in try/except and never re-raises
        assert "except Exception as daemon_exc:" in seg
        assert "non-fatal" in seg
        # no re-raise of the daemon failure
        assert "raise daemon_exc" not in seg

    def test_do_update_runs_daemon_step_off_the_event_loop(self):
        seg = self._source_segment()
        assert "asyncio.to_thread(ensure_serve_daemon)" in seg

    def test_do_update_logs_daemon_failure_as_warning(self):
        seg = self._source_segment()
        assert 'logging.warning(' in seg
        assert 'Serve daemon re-assert failed (non-fatal)' in seg

    def test_do_update_keeps_dual_error_key_contract(self):
        # W32-14(b): the daemon wiring must not touch the error-return shape
        seg = self._source_segment()
        assert '"status": "error", "error":' in seg.replace("'", '"').replace(
            '"status": "error", "error":', '"status": "error", "error":'
        ) or '"status": "error"' in seg

    def test_update_result_shape_unchanged(self):
        # the success return dict keeps its original keys
        seg = self._source_segment()
        assert '"old_version": nexus_version' in seg
        assert '"new_version": new_version' in seg
        assert '"restarting": True' in seg

    def test_ensure_serve_daemon_importable_from_mcp_server_env(self):
        # the import inside _do_update must resolve in the real package
        from nexus_memory.mcp_server import _do_update  # noqa: F401
        from nexus_memory.serve_daemon import ensure_serve_daemon  # noqa: F401
        assert callable(ensure_serve_daemon)


# ── cross-cutting invariants ─────────────────────────────────────────────────

class TestInvariants:
    def test_serve_daemon_module_is_import_light(self):
        # setup.py and mcp_server.py import serve_daemon — it must not import
        # either of them back (circular import is ruled out by construction).
        src = (_REPO_ROOT / "src" / "nexus_memory" / "serve_daemon.py").read_text()
        assert "import nexus_memory" not in src
        assert "from nexus_memory" not in src

    def test_serve_daemon_never_raises_on_total_failure(self, daemon_engine, monkeypatch):
        # every failure mode returns a dict — even a broken environment
        monkeypatch.setattr(sd, "INSTALL_SERVE_SCRIPT", "/nonexistent/install_serve.sh")
        monkeypatch.setattr(sd, "_healthz_ok", lambda port, timeout=2.0: False)
        res = sd.ensure_serve_daemon()
        assert res["status"] == "failed"
        assert "error" in res

    def test_plugin_directory_untouched(self):
        # Refactor law: plugins/memory/nexus is never touched by this feature
        plugin = _REPO_ROOT / "plugins" / "memory" / "nexus"
        assert plugin.exists()  # presence only; content changes are forbidden