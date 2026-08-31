"""Tests for agent registry cleanup of removed (ghost) agents.

Regression context: uninstalling an agent (e.g. Gemini CLI, removed 31.08.2026)
left a stale entry in agents.json forever — the registry had no deregistration
path. cleanup_removed_agents() fixes that; these tests pin the contract.
"""
import json
from datetime import datetime, timedelta, timezone

import pytest

from nexus_memory import agent_detect


def _iso(days_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


@pytest.fixture
def registry_file(tmp_path, monkeypatch):
    """Redirect the agents registry to a temp file (never touch real state)."""
    path = tmp_path / "agents.json"
    monkeypatch.setattr(agent_detect, "_get_agents_registry_path", lambda: path)
    return path


@pytest.fixture
def no_detection(monkeypatch):
    """Freeze detection: no agent detected on the machine."""
    monkeypatch.setattr(
        agent_detect, "detect_all_agents", lambda: {"agents": []}
    )


def _write_registry(path, agents):
    path.write_text(json.dumps({"agents": agents}))


def test_removed_agent_is_cleaned(registry_file, no_detection):
    """Ghost entry (undetected, dir gone, stale) -> removed."""
    _write_registry(registry_file, [{
        "id": "ghost-agent",
        "config_dir": "/nonexistent/ghost-dir",
        "last_seen": _iso(30),
    }])
    report = agent_detect.cleanup_removed_agents(grace_days=14)
    assert report["removed"] and report["removed"][0]["id"] == "ghost-agent"
    assert agent_detect.load_agents_registry()["agents"] == []


def test_fresh_agent_survives_reinstall_window(registry_file, no_detection):
    """Undetected + dir gone, but seen recently -> kept (re-install window)."""
    _write_registry(registry_file, [{
        "id": "recent-agent",
        "config_dir": "/nonexistent/ghost-dir",
        "last_seen": _iso(2),
    }])
    report = agent_detect.cleanup_removed_agents()
    assert report["removed"] == []
    ids = [a["id"] for a in agent_detect.load_agents_registry()["agents"]]
    assert ids == ["recent-agent"]


def test_existing_config_dir_keeps_entry(registry_file, no_detection):
    """Dir still on disk (e.g. skill-deploy remnants like ~/.cursor) -> kept."""
    import pathlib
    real_dir = pathlib.Path(registry_file.parent) / "cursor-remnant"
    real_dir.mkdir()
    _write_registry(registry_file, [{
        "id": "cursor",
        "config_dir": str(real_dir),
        "last_seen": _iso(100),
    }])
    report = agent_detect.cleanup_removed_agents(grace_days=0)
    assert report["removed"] == []


def test_detected_agent_never_cleaned(monkeypatch, registry_file):
    """Detector still reports the agent -> kept, even if dir path in registry
    is stale/wrong."""
    monkeypatch.setattr(
        agent_detect, "detect_all_agents",
        lambda: {"agents": [{"id": "hermes", "detected": True}]},
    )
    _write_registry(registry_file, [{
        "id": "hermes",
        "config_dir": "/nonexistent/stale-path",
        "last_seen": _iso(100),
    }])
    report = agent_detect.cleanup_removed_agents(grace_days=0)
    assert report["removed"] == []
    ids = [a["id"] for a in agent_detect.load_agents_registry()["agents"]]
    assert "hermes" in ids


def test_missing_last_seen_treated_as_stale(registry_file, no_detection):
    """Hand-crafted entry without last_seen + dir gone -> removable."""
    _write_registry(registry_file, [{
        "id": "no-ts-agent",
        "config_dir": "/nonexistent/x",
    }])
    report = agent_detect.cleanup_removed_agents(grace_days=0)
    assert report["removed"] and report["removed"][0]["id"] == "no-ts-agent"


def test_detect_error_is_fail_open(registry_file, monkeypatch):
    """If detection itself blows up, touch nothing (fail-open)."""
    def boom():
        raise RuntimeError("detector explosion")
    monkeypatch.setattr(agent_detect, "detect_all_agents", boom)
    _write_registry(registry_file, [{
        "id": "hermes",
        "config_dir": "/nonexistent/x",
        "last_seen": _iso(30),
    }])
    report = agent_detect.cleanup_removed_agents()
    assert report["status"] == "error"
    ids = [a["id"] for a in agent_detect.load_agents_registry()["agents"]]
    assert ids == ["hermes"]