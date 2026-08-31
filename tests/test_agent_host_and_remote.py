"""Tests for strict detection signals, host annotations, remote registration.

Context (31.08.2026, Nebo's dashboard UX spec):
- "Available" must list only harnesses actually found on the machine —
  kilo-code and cursor produced false positives via ~/.vscode/extensions
  and our own ~/.cursor skill-deploy folder.
- Every agent card shows a local/remote badge; the registry carries
  host_type/host_label (+optional host_provider).
- Two harnesses of the same type are distinguished by unique agent ids
  (hermes on Mac Mini vs. another Hermes on Windows-PC = two ids), never
  "hermes 1/hermes 2".
"""
import json

import pytest

from nexus_memory import agent_detect
from nexus_memory.agent_detect import (
    annotate_host,
    _check_cursor,
    _check_kilo_code,
    _local_host_label,
    register_remote_agent,
    _get_agents_registry_path,
)

@pytest.fixture
def no_detection(monkeypatch):
    """Freeze detection: no agent detected on the machine."""
    monkeypatch.setattr(agent_detect, "detect_all_agents", lambda: {"agents": []})


# NOTE: registry redirection is handled GLOBALLY by the autouse
# `isolated_agents_registry` fixture in conftest.py. Do NOT define a
# module-local registry patch here — the module gets imported twice
# (editable install + tests-path), so a local patch can bind to the wrong
# module identity while save writes via the other one (real-file leak,
# 31.08.2026 incident).


# ── strict detection ─────────────────────────────────────────────────

def test_cursor_bare_dir_is_not_detected(monkeypatch, tmp_path):
    """Our skill-deploy ~/.cursor folder must NOT register as Cursor."""
    fake_home = tmp_path / "home"
    (fake_home / ".cursor" / "skills" / "browser-use").mkdir(parents=True)
    monkeypatch.setattr(agent_detect.Path, "home", staticmethod(lambda: fake_home))
    monkeypatch.setattr(agent_detect.shutil, "which", lambda name: None)
    assert not (fake_home / ".cursor" / "mcp.json").exists()
    info = _check_cursor()
    assert info["detected"] is False


def test_cursor_app_or_cli_is_detected(monkeypatch, tmp_path):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    (fake_home / ".cursor").mkdir()
    monkeypatch.setattr(agent_detect.Path, "home", staticmethod(lambda: fake_home))
    monkeypatch.setattr(agent_detect.shutil, "which", lambda name: "/usr/bin/cursor" if name == "cursor" else None)
    assert _check_cursor()["detected"] is True


def test_kilo_vscode_extensions_dir_does_not_detect(monkeypatch, tmp_path):
    """A plain VS Code install (~/.vscode/extensions) must NOT be Kilo Code."""
    fake_home = tmp_path / "home"
    (fake_home / ".vscode" / "extensions").mkdir(parents=True)
    monkeypatch.setattr(agent_detect.Path, "home", staticmethod(lambda: fake_home))
    monkeypatch.setattr(agent_detect.shutil, "which", lambda name: None)
    assert _check_kilo_code()["detected"] is False


def test_kilo_real_config_detects(monkeypatch, tmp_path):
    fake_home = tmp_path / "home"
    (fake_home / ".kilo").mkdir(parents=True)
    (fake_home / ".kilo" / "mcp.json").write_text("{}")
    monkeypatch.setattr(agent_detect.Path, "home", staticmethod(lambda: fake_home))
    monkeypatch.setattr(agent_detect.shutil, "which", lambda name: None)
    assert _check_kilo_code()["detected"] is True


# ── host annotation ──────────────────────────────────────────────────

def test_annotate_host_defaults_local(isolated_agents_registry):
    a = annotate_host({"id": "x"})
    assert a["host_type"] == "local"
    assert a["host_label"] == _local_host_label()


def test_annotate_host_never_clobbers():
    a = annotate_host({"id": "x", "host_type": "remote", "host_label": "Hetzner CX22"}, host_type="local")
    assert a["host_type"] == "remote"
    assert a["host_label"] == "Hetzner CX22"


def test_env_overrides_host_label(monkeypatch):
    monkeypatch.setenv("NEXUS_HOST_LABEL", "Mac Mini")
    assert _local_host_label() == "Mac Mini"


# ── remote registration ──────────────────────────────────────────────

def test_register_remote_agent_roundtrip(isolated_agents_registry):
    result = register_remote_agent(
        agent_id="vps-bot", name="VPS Bot", trust_level="trusted",
        host_label="Hetzner CX22", host_provider="Hetzner",
    )
    assert result["status"] == "registered"
    assert result["agent"]["host_type"] == "remote"
    assert result["agent"]["host_provider"] == "Hetzner"
    reg = json.loads(isolated_agents_registry.read_text())
    assert [a["id"] for a in reg["agents"]] == ["vps-bot"]


def test_register_remote_rejects_duplicate_and_local_ids(isolated_agents_registry):
    assert register_remote_agent("vps-bot", "VPS Bot")["status"] == "registered"
    dup = register_remote_agent("vps-bot", "again")
    assert "error" in dup
    shadow = register_remote_agent("hermes", "fake")
    assert "error" in shadow


def test_register_remote_rejects_bad_trust(isolated_agents_registry):
    assert "error" in register_remote_agent("vps-x", "X", trust_level="root")


def test_register_remote_survives_cleanup(isolated_agents_registry, no_detection):
    """A registered remote agent has no config_dir on this machine — the
    cleanup must NOT treat its 'config_dir gone' as a removal signal while
    last_seen is fresh within the grace window."""
    register_remote_agent("vps-bot", "VPS Bot", host_label="Hetzner")
    report = agent_detect.cleanup_removed_agents()
    ids = [a["id"] for a in json.loads(isolated_agents_registry.read_text())["agents"]]
    assert report["status"] == "ok"
    assert "vps-bot" in ids, "fresh remote entry must survive cleanup"


def test_stale_remote_ghost_is_cleaned(isolated_agents_registry, monkeypatch):
    """A remote entry whose agent never called back and went stale gets
    removed (last_seen older than grace, nothing on disk, undetected)."""
    register_remote_agent("vps-old", "Old", host_label="Hetzner",
                          trust_level="trusted")
    from datetime import datetime, timedelta, timezone
    path = isolated_agents_registry
    reg = json.loads(path.read_text())
    old = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
    reg["agents"][0]["last_seen"] = old
    reg["agents"][0]["connected_at"] = old
    path.write_text(json.dumps(reg))
    report = agent_detect.cleanup_removed_agents()
    ids = [a["id"] for a in json.loads(path.read_text())["agents"]]
    assert ids == [], "stale never-called-back remote ghost must be cleaned"


def test_backfill_annotates_legacy_entries(isolated_agents_registry, no_detection, monkeypatch):
    path = isolated_agents_registry
    path.write_text(json.dumps(
        {"agents": [{"id": "hermes", "last_seen": "2026-08-31T00:00:00+00:00"}]}
    ))
    monkeypatch_detect = {
        "detected_agents": [{"id": "hermes", "detected": True}]
    }
    monkeypatch.setattr(agent_detect, "detect_all_agents", lambda: monkeypatch_detect)
    agent_detect.cleanup_removed_agents()
    reg = json.loads(path.read_text())
    entry = reg["agents"][0]
    assert entry["host_type"] == "local"
    assert entry["host_label"] == _local_host_label()