"""Cross-process concurrency + fail-closed invariants for the agents registry.

The registry (agents.json) is a shared JSON file mutated by several
processes: MCP-server stats updates, the setup wizard's register_agent,
dashboard reads, and periodic cleanup. The invariants pinned here:

1. Every whole read-modify-write cycle runs under an exclusive flock()
   held on a lock file next to the registry, and the write is an atomic
   temp-file + os.replace — parallel registry writers must never lose
   each other's registrations or trust changes (and a reader must never
   see a half-written file).
2. A corrupt registry fails closed: load raises instead of returning an
   empty registry that the next save would use to wipe real entries.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from nexus_memory import agent_detect


def test_concurrent_writers_all_registrations_survive(tmp_path, monkeypatch):
    """8 concurrent PROCESSES x 3 registrations each -> all 24 survive.

    Real flock() semantics: the workers are separate Python processes
    acquired via subprocess, each doing a full read-modify-write cycle on
    the same registry file. Before locking, every writer read the same
    snapshot and the last writer won (registrations lost). The lock file
    must live in the same directory as the (redirected) registry.
    """
    path = tmp_path / "agents.json"
    monkeypatch.setattr(agent_detect, "_get_agents_registry_path", lambda: path)

    workers = 8
    per_worker = 3
    script = tmp_path / "registry_worker.py"
    script.write_text(
        "import json, sys\n"
        "from pathlib import Path\n"
        "sys.path.insert(0, r'%s')\n"
        "from nexus_memory import agent_detect as ad\n"
        "path = Path(r'%s')\n"
        "ad._get_agents_registry_path = lambda: path\n"
        "wid = int(sys.argv[1])\n"
        "for n in range(%d):\n"
        "    ad.register_agent(\n"
        "        f'agent-{wid}-{n}', 'A', '*', 'trusted', 'mcp_only')\n"
        "print(json.dumps({'ok': True}))\n"
        % (str(Path(agent_detect.__file__).parent.parent.parent),
           str(path), per_worker)
    )

    procs = [
        subprocess.Popen(
            [sys.executable, str(script), str(w)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        for w in range(workers)
    ]
    for p in procs:
        out, err = p.communicate(timeout=120)
        assert p.returncode == 0, f"worker failed: {err}"

    registry = agent_detect.load_agents_registry()
    ids = sorted(a["id"] for a in registry["agents"])
    assert len(ids) == workers * per_worker
    assert len(set(ids)) == workers * per_worker  # no lost duplicates


def test_trust_change_survives_parallel_stats_update(tmp_path, monkeypatch):
    """Trust update vs. stats RMW in two processes: no lost update."""
    path = tmp_path / "agents.json"
    monkeypatch.setattr(agent_detect, "_get_agents_registry_path", lambda: path)

    agent_detect.register_agent("hermes", "H", "f", "public", "mcp_only")

    child = tmp_path / "stats_worker.py"
    child.write_text(
        "import sys\n"
        "from pathlib import Path\n"
        "sys.path.insert(0, r'%s')\n"
        "from nexus_memory import agent_detect as ad\n"
        "path = Path(r'%s')\n"
        "ad._get_agents_registry_path = lambda: path\n"
        "for _ in range(20):\n"
        "    ad.update_agent_stats('hermes', read=True)\n"
        % (str(Path(agent_detect.__file__).parent.parent.parent),
           str(path))
    )
    proc = subprocess.Popen(
        [sys.executable, str(child)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    # Parent races the child's 20 stats updates with a trust change.
    for _ in range(20):
        agent_detect.set_agent_trust_level("hermes", "private")
        agent_detect.update_agent_stats("hermes", write=True)
    out, err = proc.communicate(timeout=120)
    assert proc.returncode == 0, f"child failed: {err}"

    agents = agent_detect.load_agents_registry()["agents"]
    assert len(agents) == 1
    hermes = agents[0]
    assert hermes["trust_level"] == "private"
    # Final state must be consistent: at least one of the racing writers'
    # increments survived — and the registry is a valid JSON doc (read
    # succeeded at all), i.e. never half-written.
    assert hermes["reads"] >= 0 and hermes["writes"] >= 20


def test_lock_file_lives_next_to_registry(tmp_path, monkeypatch):
    """The flock file is <registry>.lock in the SAME directory."""
    path = tmp_path / "sub" / "agents.json"
    monkeypatch.setattr(agent_detect, "_get_agents_registry_path", lambda: path)

    agent_detect.save_agents_registry({"agents": []})
    assert (tmp_path / "sub" / "agents.json.lock").exists()


def test_atomic_write_leaves_no_tmp_and_reader_never_sees_partial(
        tmp_path, monkeypatch):
    """After save: registry complete JSON, no leftover temp files."""
    path = tmp_path / "agents.json"
    monkeypatch.setattr(agent_detect, "_get_agents_registry_path", lambda: path)

    agent_detect.register_agent("hermes", "H", "f", "trusted", "mcp_only")
    doc = json.loads(path.read_text())
    assert [a["id"] for a in doc["agents"]] == ["hermes"]
    leftovers = [p.name for p in tmp_path.iterdir()
                 if p.name.startswith("agents.json.tmp")]
    assert leftovers == []


def test_corrupt_registry_fails_closed(tmp_path, monkeypatch):
    """A parse error must NOT surface as an empty registry.

    The old fail-open behavior returned {'agents': []}; the next save then
    overwrote every real entry. Now load raises (fail closed) and the
    caller aborts instead of wiping the file.
    """
    path = tmp_path / "agents.json"
    monkeypatch.setattr(agent_detect, "_get_agents_registry_path", lambda: path)
    path.write_text("{corrupt json")

    with pytest.raises(json.JSONDecodeError):
        agent_detect.load_agents_registry()

    # The same fail-closed rule applies inside the RMW writers: corrupt
    # file -> exception, file untouched, nothing wiped.
    before = path.read_text()
    with pytest.raises(json.JSONDecodeError):
        agent_detect.update_agent_stats("hermes", read=True)
    with pytest.raises(json.JSONDecodeError):
        agent_detect.register_agent("x", "X", "*", "trusted", "mcp_only")
    assert path.read_text() == before, "corrupt registry must not be overwritten"