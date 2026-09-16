"""OCR review wave 20 - fixes for findings Nr 322-352 (no 339) + 339.

One test class per finding: source-inspection plus behavior tests.
Written by Kiosha (CC was contractually not allowed to touch this file).
"""
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

_TESTS = Path(__file__).resolve().parent
_ROOT = _TESTS.parent


def _read(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


# ── helpers ──────────────────────────────────────────────────────────────────

def _load(rel: str, name: str):
    import importlib.util
    path = _ROOT / rel
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── Nr 322: report encoding utf-8 ────────────────────────────────────────────

class TestNr322ReportEncoding:
    def test_open_carries_utf8(self):
        src = _read("src/nexus_memory/selective_forgetting.py")
        assert 'open(report["report_file"], "w", encoding="utf-8")' in src


# ── Nr 323: cosine dimension mismatch fail-closed ───────────────────────────

class TestNr323CosineMismatch:
    def test_behavioral(self):
        from nexus_memory.scope_auto import _cosine
        assert _cosine([1.0, 0.0], [1.0, 0.0, 5.0]) == 0.0
        assert _cosine([1.0, 0.0, 0.0], [1.0]) == 0.0
        # sane vector still works
        assert abs(_cosine([1.0, 0.0], [1.0, 0.0]) - 1.0) < 1e-9

    def test_code_present(self):
        src = _read("src/nexus_memory/scope_auto.py")
        assert "len(a) != len(b)" in src


# ── Nr 324/325: trust_service updated_at + _collection used ─────────────────

class TestNr324TrustUpdatedAt:
    def test_payload_carries_updated_at(self):
        src = _read("src/nexus_memory/trust_service.py")
        idx = src.find("if not dry_run and")
        body = src[idx:idx + 1200]
        assert '"updated_at"' in body
        assert "datetime.now(timezone.utc).isoformat()" in body


class TestNr325CollectionUsed:
    def test_no_store_collection_reads(self):
        src = _read("src/nexus_memory/trust_service.py")
        assert src.count("self._store.collection_name") == 1  # only the __init__ fallback
        assert "self._collection = collection or self._store.collection_name" in src

    def test_param_none_falls_back(self):
        import tempfile
        from nexus_memory.trust_service import TrustService

        class _S:
            collection_name = "fallback-coll"
        svc = TrustService(_S(), None, data_dir=tempfile.mkdtemp())
        assert svc._collection == "fallback-coll"


# ── Nr 326: find_spec instead of pkgutil.find_loader ────────────────────────

class TestNr326FindSpec:
    def test_no_pkgutil_find_loader(self):
        src = _read("src/nexus_memory/wizard.py")
        assert "pkgutil.find_loader" not in src
        assert "find_spec" in src


# ── Nr 327: asyncio.run for the embedding probe ─────────────────────────────

class TestNr327AsyncioRun:
    def test_no_manual_loop(self):
        src = _read("src/nexus_memory/wizard.py")
        idx = src.find("async def _test()")
        body = src[idx:idx + 1200]
        assert "asyncio.run(_test())" in body
        assert "new_event_loop" not in body


# ── Nr 328: pip failure skips HF download; ollama False aborts ──────────────

class TestNr328SetupChecks:
    def test_pip_result_checked_before_hf(self):
        src = _read("src/nexus_memory/wizard.py")
        idx = src.find("Install bge-m3 via HuggingFace")
        body = src[idx:idx + 2000]
        assert "if not _run_pip(" in body

    def test_wizard_checks_setup_ollama(self):
        src = _read("src/nexus_memory/wizard.py")
        idx = src.find('elif provider_id == "ollama"')
        body = src[idx:idx + 300]
        assert "if not _setup_ollama(" in body


# ── Nr 329: corrupt config backup ───────────────────────────────────────────

class TestNr329CorruptConfigBackup:
    def test_backup_and_warn(self, tmp_path, monkeypatch):
        from nexus_memory import wizard as wiz
        cfg = tmp_path / "config.json"
        cfg.write_text("{not json")
        monkeypatch.setattr(wiz, "_get_config_dir", lambda: tmp_path)
        out = []
        monkeypatch.setattr(wiz, "_print", lambda s: out.append(s))
        wiz._save_config("ollama", "qwen3-embedding:0.6b")
        backups = list(tmp_path.glob("config.json.corrupt-*"))
        assert backups, "corrupt config must be backed up"
        assert cfg.read_text().startswith("{")

    def test_valid_config_not_backed_up(self, tmp_path, monkeypatch):
        from nexus_memory import wizard as wiz
        cfg = tmp_path / "config.json"
        cfg.write_text('{"embedding_provider": "openai"}')
        monkeypatch.setattr(wiz, "_get_config_dir", lambda: tmp_path)
        wiz._save_config("ollama", "m")
        assert not list(tmp_path.glob("config.json.corrupt-*"))


# ── Nr 330: declined install respected ──────────────────────────────────────

class TestNr330DeclinedInstall:
    def test_skip_flag_honoured(self, monkeypatch, capsys):
        from nexus_memory import wizard as wiz
        monkeypatch.setattr(wiz, "_LOCAL_ST_SKIP_INSTALL", True)
        monkeypatch.setattr(wiz, "_check_pip_package", lambda p: False)
        ps = SimpleNamespace(provider={"pip_package": "sentence-transformers"})
        assert wiz._install_pip_package(ps) is False
        assert "declined" in capsys.readouterr().out


# ── Nr 331: env int range validation ────────────────────────────────────────

class TestNr331EnvInt:
    def test_env_int_helper(self, monkeypatch):
        from nexus_memory import retrieval_watch as rw
        monkeypatch.setenv("NEXUS_RETRIEVAL_INTERVAL_SEC", "0")
        assert rw._env_int("NEXUS_RETRIEVAL_INTERVAL_SEC", 86400, 1) == 86400
        monkeypatch.setenv("NEXUS_RETRIEVAL_INTERVAL_SEC", "-3")
        assert rw._env_int("NEXUS_RETRIEVAL_INTERVAL_SEC", 86400, 1) == 86400
        monkeypatch.setenv("NEXUS_RETRIEVAL_INTERVAL_SEC", "abc")
        assert rw._env_int("NEXUS_RETRIEVAL_INTERVAL_SEC", 86400, 1) == 86400
        monkeypatch.setenv("NEXUS_RETRIEVAL_INTERVAL_SEC", "60")
        assert rw._env_int("NEXUS_RETRIEVAL_INTERVAL_SEC", 86400, 1) == 60


# ── Nr 332: docstring honesty ───────────────────────────────────────────────

class TestNr332DocstringHonesty:
    def test_no_webhook_claim(self):
        src = _read("src/nexus_memory/retrieval_watch.py")
        assert "Webhook (falls NEXUS_WEBHOOK_URL gesetzt)" not in src
        assert "NICHT implementiert" in src or "not wired" in src.lower()


# ── Nr 333: setup.py index validation ───────────────────────────────────────

class TestNr333IndexValidation:
    def test_negative_index_rejected(self):
        src = _read("src/nexus_memory/setup.py")
        idx = src.find("def cli_interactive")
        body = src[idx:idx + 3000]
        assert "idx < 0 or idx >= len(scan[\"providers\"])" in body


# ── Nr 334: background tasks strong refs ────────────────────────────────────

class TestNr334BackgroundTasks:
    def test_module_set_exists(self):
        src = _read("src/nexus_memory/mcp_server.py")
        assert "_BACKGROUND_TASKS" in src
        assert "add_done_callback(_BACKGROUND_TASKS.discard)" in src


# ── Nr 335: canonical revert guarded + restore ──────────────────────────────

class TestNr335CanonicalRevertGuarded:
    def test_pre_read_and_restore_present(self):
        src = _read("src/nexus_memory/mcp_server.py")
        idx = src.find("_revert_payload = {")
        body = src[max(0, idx - 2500):idx + 4500]
        assert "point not found" in body
        assert "_orig_lifecycle" in body
        assert 'status == "error"' in body


# ── Nr 336: _to_point_id bad-hex passthrough ────────────────────────────────

class TestNr336ToPointId:
    def test_bad_uuid_shape_passes_through(self):
        from nexus_memory.mcp_server import _to_point_id
        bad = "zzzzzzzz-aaaa-bbbb-cccc-dddddddddddd"
        assert _to_point_id(bad) == bad
        import uuid as _u
        assert _to_point_id(str(_u.uuid4())) == _u.UUID(str(_u.uuid4())) or isinstance(
            _to_point_id(str(_u.uuid4())), _u.UUID)
        assert isinstance(_to_point_id(str(_u.uuid4())), _u.UUID)

    def test_int_passthrough(self):
        from nexus_memory.mcp_server import _to_point_id
        assert _to_point_id("42") == 42


# ── Nr 337: nudge appended after slice ──────────────────────────────────────

class TestNr337NudgeAfterSlice:
    def test_stamp_after_append(self):
        src = _read("src/nexus_memory/mcp_server.py")
        idx = src.find("notice = None")
        body = src[idx:idx + 2000]
        stamp = body.find("self._update_nudged_at = time.time()")
        append = body.find("results.append(notice)")
        slice_pos = body.find("results[:limit]")
        assert 0 < slice_pos < stamp < append


# ── Nr 338: access tracking off the event loop ──────────────────────────────

class TestNr338TrackInThread:
    def test_to_thread_wraps_track_block(self):
        src = _read("src/nexus_memory/mcp_server.py")
        idx = src.find("_track_access_sync")
        assert idx >= 0
        body = src[idx:idx + 2500]
        assert "asyncio.to_thread(_track_access_sync)" in body


# ── Nr 339/340/341/342: install_hermes_plugin.sh ────────────────────────────

class TestNr339HermesInstallerBackup:
    def test_backup_and_remove_verified(self):
        src = _read("scripts/install_hermes_plugin.sh")
        assert "backup_and_remove" in src
        assert 'missing after move' in src


class TestNr340HermesExit2:
    def test_hermes_missing_exits_2(self):
        src = _read("scripts/install_hermes_plugin.sh")
        idx = src.find("HERMES_PLUGIN_DIR} missing")
        body = src[idx:idx + 400]
        assert "exit 2" in body


class TestNr341HermesConfigGuard:
    def test_config_set_failure_not_fatal(self):
        src = _read("scripts/install_hermes_plugin.sh")
        idx = src.find("if hermes config set memory.provider nexus; then")
        assert idx >= 0
        body = src[idx:idx + 400]
        assert "Recovery" in body


class TestNr342DashboardPort:
    def test_both_installers_print_9121(self):
        for rel in ("scripts/install_hermes_plugin.sh",
                    "scripts/install_openclaw_plugin.sh"):
            src = _read(rel)
            assert "9210" not in src, rel
            assert "127.0.0.1:9121" in src, rel


# ── Nr 343-347: openclaw plugin installer ───────────────────────────────────

class TestNr343SymlinkFallbackGuard:
    def test_target_absent_check_before_copy(self):
        src = _read("plugins/openclaw/scripts/install_openclaw_plugin.sh")
        assert "target already exists after failed symlink" in src


class TestNr344TrapOnErr:
    def test_err_trap(self):
        src = _read("plugins/openclaw/scripts/install_openclaw_plugin.sh")
        assert "trap" in src and "install incomplete" in src


class TestNr345MissingStateDirAborts:
    def test_aborts(self):
        src = _read("plugins/openclaw/scripts/install_openclaw_plugin.sh")
        idx = src.find("OpenClaw state directory not found")
        body = src[idx:idx + 300]
        assert "exit 1" in body


class TestNr346PluginManifestSanity:
    def test_manifest_check(self):
        src = _read("plugins/openclaw/scripts/install_openclaw_plugin.sh")
        assert "openclaw.plugin.json" in src


class TestNr347LeastPrivilegeConfig:
    def test_config_defaults(self):
        src = _read("plugins/openclaw/scripts/install_openclaw_plugin.sh")
        assert "allowPromptInjection\\\": false" in src or "\"allowPromptInjection\": false" in src
        # W27-10 superseded "default": that value is not in lib/config.ts's
        # enum (public|trusted|private) and made the printed snippet throw.
        # "private" is the valid least-privilege default now.
        assert '"accessLevel": "private"' in src
        assert '"accessLevel": "default"' not in src


# ── Nr 348/349/350: release_gate.sh ─────────────────────────────────────────

class TestNr348VersionShapeFilter:
    def test_filter_before_sort(self):
        src = _read("scripts/release_gate.sh")
        idx = src.find("VERSION_TAGS=")
        assert idx >= 0
        body = src[idx:idx + 400]
        assert "grep -E '^v?[0-9]+" in body
        assert "sort -V" in body


class TestNr349GateOutputEveryPath:
    def test_grep_guarded(self):
        src = _read("scripts/release_gate.sh")
        assert "| sed 's/version" in src and "|| true" in src


class TestNr350NormalizationAndHeadWarn:
    def test_suffix_strip(self):
        src = _read("scripts/release_gate.sh")
        assert 'REMOTE_TAG_VER%%[-+]*' in src

    def test_head_tagged_warning_alarm_only(self):
        src = _read("scripts/release_gate.sh")
        assert "describe --tags --exact-match" in src
        assert "GATE-WARN" in src


# ── Nr 351/352: setup.sh ────────────────────────────────────────────────────

class TestNr351GitWorktreeCheck:
    def test_rev_parse_guard(self):
        src = _read("setup.sh")
        assert "rev-parse --is-inside-work-tree" in src
        assert "not a git checkout" in src


class TestNr352HealthProbeTimeout:
    def test_max_time_and_fallback(self):
        src = _read("setup.sh")
        assert "--max-time 5" in src
        assert "urllib.request.urlopen" in src


# ── Verhalten: release_gate Normalisierung (echter bash-Lauf) ───────────────

class TestNr350BehavioralGate:
    def test_gate_tolerates_rc_tag(self, tmp_path, monkeypatch):
        import subprocess
        pyproj = tmp_path / "pyproject.toml"
        pyproj.write_text('version = "0.19.1"\n')
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        (repo_dir / "pyproject.toml").write_text('version = "0.19.1"\n')
        env = dict(os.environ)
        env["NEXUS_REPO_DIR"] = str(tmp_path)
        # fake gh via PATH shim
        shim = tmp_path / "bin"
        shim.mkdir()
        gh = shim / "gh"
        gh.write_text('#!/bin/sh\necho "v0.19.1-rc1"\necho "nightly"\n')
        gh.chmod(0o755)
        env["PATH"] = f"{shim}:{env.get('PATH','')}"
        env["NEXUS_REPO"] = "fake/repo"
        r = subprocess.run(
            ["bash", str(_ROOT / "scripts" / "release_gate.sh")],
            capture_output=True, text=True, env=env, timeout=30)
        assert "GATE-GRUEN" in r.stdout
        assert "0.19.1-rc1" in r.stdout  # rc tag tolerated, normalized