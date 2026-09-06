"""Invariant tests for the agent_detect MEDIUM review fixes.

1. :430 — register_agent validates trust_level (no unknown levels).
2. :451 — re-registration preserves usage stats + connected_at.
3. :587 — cleanup never removes explicitly registered remote agents.
4. :600 — timezone-less last_seen cannot crash the cleanup.
5. :660 — local registration refuses ids registered as REMOTE.
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from nexus_memory import agent_detect as ad


@pytest.fixture
def reg_path(tmp_path, monkeypatch):
    path = tmp_path / "agents.json"
    monkeypatch.setattr(ad, "_get_agents_registry_path", lambda: path)
    return path


# ── :430 trust validation ────────────────────────────────────────────

def test_register_rejects_invalid_trust_level(reg_path):
    res = ad.register_agent("a1", "Agent One", "A", "superuser", "mcp_only")
    assert "error" in res
    # Nothing must have been written (either no file or no agents).
    if reg_path.exists():
        agents = json.loads(reg_path.read_text()).get("agents", [])
        assert not agents, "invalid registration must not write the registry"


def test_register_accepts_valid_trust_level(reg_path):
    res = ad.register_agent("a1", "Agent One", "A", "trusted", "mcp_only")
    assert "error" not in res


# ── :451 stats preserved on re-registration ─────────────────────────

def test_reregistration_preserves_stats(reg_path):
    ad.register_agent("a1", "A1", "A", "trusted", "mcp_only")
    ad.update_agent_stats("a1", read=True, write=True)
    ad.update_agent_stats("a1", read=True)
    before = json.loads(reg_path.read_text())["agents"][0]

    ad.register_agent("a1", "A1-renamed", "A", "trusted", "mcp_only")
    after = json.loads(reg_path.read_text())["agents"][0]

    assert after["reads"] == 2, "re-registration must preserve reads"
    assert after["writes"] == 1, "re-registration must preserve writes"
    assert after["connected_at"] == before["connected_at"]
    assert after["name"] == "A1-renamed", "metadata refreshed"


# ── :587 remote agents survive cleanup ──────────────────────────────

def test_cleanup_never_removes_fresh_remote_agents(reg_path, monkeypatch):
    """Remote seats survive regular cleanup: they are undetected + no
    config_dir by design. Only the much longer REMOTE_GHOST_DAYS horizon
    (default 30d) reaps a seat whose agent never called back."""
    ad.register_remote_agent("remote-seat", "Remote Seat", trust_level="trusted")
    # Stale beyond regular grace, but within the remote ghost horizon.
    data = json.loads(reg_path.read_text())
    old = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()
    data["agents"][0]["last_seen"] = old
    reg_path.write_text(json.dumps(data))

    monkeypatch.setattr(ad, "detect_all_agents",
                        lambda: {"detected_agents": []})
    rep = ad.cleanup_removed_agents(grace_days=7)

    assert rep["status"] == "ok"
    ids = json.loads(reg_path.read_text()).get("agents", [])
    assert any(a["id"] == "remote-seat" for a in ids), \
        "remote agent within the ghost horizon must survive cleanup"


def test_cleanup_reaps_remote_ghost_beyond_horizon(reg_path, monkeypatch):
    """A remote seat whose agent NEVER called back and went stale far
    beyond REMOTE_GHOST_DAYS is eventually cleaned (no immortal entries)."""
    ad.register_remote_agent("remote-ghost", "Ghost", trust_level="trusted")
    data = json.loads(reg_path.read_text())
    ancient = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
    data["agents"][0]["last_seen"] = ancient
    data["agents"][0]["connected_at"] = ancient
    reg_path.write_text(json.dumps(data))

    monkeypatch.setattr(ad, "detect_all_agents",
                        lambda: {"detected_agents": []})
    rep = ad.cleanup_removed_agents(grace_days=7)
    ids = [a["id"] for a in json.loads(reg_path.read_text()).get("agents", [])]
    assert "remote-ghost" not in ids, "ghost beyond horizon must be reaped"


# ── :600 naive last_seen cannot crash cleanup ───────────────────────

def test_cleanup_survives_naive_timestamp(reg_path, monkeypatch):
    ad.register_agent("a1", "A1", "A", "trusted", "mcp_only")
    data = json.loads(reg_path.read_text())
    old_naive = (datetime.now(timezone.utc) - timedelta(days=90)) \
        .replace(tzinfo=None).isoformat()
    data["agents"][0]["last_seen"] = old_naive  # no tzinfo
    reg_path.write_text(json.dumps(data))

    monkeypatch.setattr(ad, "detect_all_agents",
                        lambda: {"detected_agents": [{"id": "a1", "detected": False}]})
    rep = ad.cleanup_removed_agents(grace_days=7)  # must not raise
    assert rep["status"] == "ok"


# ── :660 local registration refuses remote ids ──────────────────────

def test_local_register_refuses_remote_id(reg_path):
    ad.register_remote_agent("my-remote-vps", "My VPS")
    res = ad.register_agent("my-remote-vps", "Imposter", "X", "trusted", "mcp_only")
    assert "error" in res
    agents = json.loads(reg_path.read_text())["agents"]
    entry = next(a for a in agents if a["id"] == "my-remote-vps")
    assert entry["name"] == "My VPS", "remote entry must not be clobbered"
    assert entry["host_type"] == "remote"