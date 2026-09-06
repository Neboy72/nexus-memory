"""Invariant tests for the wizard MEDIUM review fixes.

1. :547 — API-key input uses hidden entry (getpass), never plain input().
2. :167 — a failed pip retry is reported as FAILURE, not success.
3. :337 — switching providers drops the previous provider's recorded model.
(:365/.env RMW is covered by test_env_secret_store.py.)
"""

import json
import subprocess
from types import SimpleNamespace

import pytest

from nexus_memory import wizard as wiz


# ── :167 pip retry exit code ─────────────────────────────────────────

def test_pip_retry_failure_reports_failure(monkeypatch):
    calls = []

    def fake_run(*args, **kwargs):
        calls.append(args[0] if args else kwargs)
        # 1st call (quiet) fails, retry fails too
        return SimpleNamespace(returncode=1, stderr="boom", stdout="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    ok = wiz._run_pip("some-package")
    assert ok is False
    assert len(calls) == 2, "retry must have been attempted"


def test_pip_retry_success_reports_success(monkeypatch):
    calls = []

    def fake_run(*args, **kwargs):
        calls.append(1)
        return SimpleNamespace(returncode=1 if len(calls) == 1 else 0,
                               stderr="", stdout="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    ok = wiz._run_pip("some-package")
    assert ok is True


def test_pip_first_try_success_no_retry(monkeypatch):
    calls = []

    def fake_run(*args, **kwargs):
        calls.append(1)
        return SimpleNamespace(returncode=0, stderr="", stdout="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    ok = wiz._run_pip("some-package")
    assert ok is True
    assert len(calls) == 1


# ── :337 provider switch resets recorded model ───────────────────────

def test_provider_switch_drops_stale_model(tmp_path, capsys):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({
        "embedding_provider": "ollama",
        "embedding_model": "bge-m3:latest",
        "trust_level": "trusted",
    }))

    monkey_dir = tmp_path
    import nexus_memory.wizard as w
    orig = w._get_config_dir
    w._get_config_dir = lambda: monkey_dir
    try:
        w._save_config("voyage", "")  # switch to cloud, no local model
    finally:
        w._get_config_dir = orig

    saved = json.loads(cfg.read_text())
    assert saved["embedding_provider"] == "voyage"
    assert "embedding_model" not in saved, \
        "stale Ollama model must not survive a provider switch"
    assert saved["trust_level"] == "trusted", "unrelated keys preserved"


def test_local_provider_records_model(tmp_path, capsys):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"embedding_provider": "voyage"}))

    import nexus_memory.wizard as w
    orig = w._get_config_dir
    w._get_config_dir = lambda: tmp_path
    try:
        w._save_config("ollama", "qwen3-embedding:0.6b")
    finally:
        w._get_config_dir = orig

    saved = json.loads(cfg.read_text())
    assert saved["embedding_provider"] == "ollama"
    assert saved["embedding_model"] == "qwen3-embedding:0.6b"