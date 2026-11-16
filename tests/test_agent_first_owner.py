"""The first agent in an empty registry is the owner.

Nebo's rule (long agreed, easily lost): the very first agent that connects to a
fresh installation gets PRIVATE visibility and the ADMIN role — otherwise a new
user has nobody who can manage the installation. Every later agent starts
small: trusted at most, role know, and the user raises it.

Without this test the behaviour silently regressed to "everyone starts public",
which looks like a broken product to a first-time user.
"""

from __future__ import annotations

import json

import pytest

from nexus_memory import agent_detect


@pytest.fixture()
def empty_registry(tmp_path, monkeypatch):
    """Point the registry at an empty file so 'first contact' can be tested."""
    path = tmp_path / "agents.json"
    path.write_text(json.dumps({"agents": []}))
    monkeypatch.setattr(agent_detect, "REGISTRY_PATH", str(path), raising=False)
    monkeypatch.setattr(agent_detect, "_registry_path", lambda: path, raising=False)
    monkeypatch.setenv("NEXUS_DATA_DIR", str(tmp_path))
    return path


def _agents(path) -> list[dict]:
    return json.loads(path.read_text())["agents"]


def test_first_agent_becomes_owner(empty_registry):
    agent_detect.update_agent_stats("erster-agent", read=True)

    agents = _agents(empty_registry)
    assert len(agents) == 1
    first = agents[0]
    assert first["trust_level"] == "private", "der erste Agent ist der Eigentuemer"
    assert first["role"] == "admin", "der erste Agent muss verwalten koennen"


def test_second_agent_starts_small(empty_registry):
    agent_detect.update_agent_stats("erster-agent", read=True)
    agent_detect.update_agent_stats("zweiter-agent", read=True)

    agents = {a["id"]: a for a in _agents(empty_registry)}
    assert agents["erster-agent"]["trust_level"] == "private"
    assert agents["zweiter-agent"]["trust_level"] == "public", (
        "jeder weitere Ankoemmling startet ohne Sicht, nicht als Eigentuemer"
    )
    assert agents["zweiter-agent"].get("role") != "admin"
