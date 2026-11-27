"""Das Reparaturwerkzeug fuer doppelte Agenten-Eintraege.

Zwei Dinge muessen stimmen, sonst ist das Werkzeug gefaehrlich:

  1. Es legt NUR echte Dubletten still — gleicher Harness auf DEMSELBEN
     Rechner. Zwei Installationen desselben Harness auf zwei Rechnern (DSH auf
     Mac mini und Windows-PC) sind gewollt und bleiben unberuehrt.
  2. Der neue Schluessel des Keepers existiert genau einmal: in der Datei mit
     mode 0600. Er darf in keiner Ausgabe auftauchen.

Dazu: ein stillgelegter Eintrag bekommt keinen Zugang mehr und wird nie wieder
uebernommen — das prueft die Dienstseite mit.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "scripts" / "fix_duplicate_agent.py"


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


tool = _load(TOOL, "fix_duplicate_agent")


def _entry(**kw):
    base = {
        "id": "x", "name": "Agent", "harness": "hermes", "role": "know",
        "trust_level": "trusted", "install_type": "plugin", "config_dir": None,
        "connected_at": "2026-11-26T13:00:00+00:00",
        "last_seen": "2026-11-26T13:00:00+00:00",
        "reads": 0, "writes": 0, "host_label": "Mac Mini", "host_type": "local",
        "icon": "🔌", "enroll_count": 1,
        "key_hash": "sha256:" + "a" * 64, "key_issued_at": "2026-11-26T13:00:00+00:00",
        "enrolled_at": "2026-11-26T13:00:00+00:00",
    }
    base.update(kw)
    return base


@pytest.fixture()
def umgebung(tmp_path):
    """Register mit der echten Dublette + der legitimen zweiten DSH-Installation."""
    registry = {
        "version": 1,
        "agents": [
            _entry(id="hx7m2p", name="Hermes Agent", harness="hermes", role="admin",
                   trust_level="private", reads=3610, writes=16888,
                   connected_at="2026-06-20T19:52:13+00:00"),
            _entry(id="674zw5", name="Hermes Agent", harness="hermes", role="know",
                   reads=0, writes=11,
                   connected_at="2026-11-26T13:54:22+00:00",
                   key_hash="sha256:" + "b" * 64),
            # DSH: zwei Rechner, beide gewollt — darf NICHT angefasst werden
            _entry(id="ds5w6k", name="DSH", harness="dsh", host_label="Windows PC",
                   host_type="remote", reads=48, writes=32),
            _entry(id="6c5c59", name="DSH", harness="dsh", host_label="Mac Mini",
                   reads=0, writes=0),
        ],
    }
    reg_path = tmp_path / "agents.json"
    reg_path.write_text(json.dumps(registry, indent=2), encoding="utf-8")
    key_dir = tmp_path / "data"
    key_dir.mkdir()
    return reg_path, key_dir


def _argv(reg_path, key_dir, *extra):
    return ["--registry", str(reg_path), "--key-dir", str(key_dir), *extra]


def _registry(reg_path):
    return json.loads(reg_path.read_text(encoding="utf-8"))


def _by_id(reg_path, agent_id):
    return next(a for a in _registry(reg_path)["agents"] if a["id"] == agent_id)


# ── Trockenlauf ───────────────────────────────────────────────────────────

def test_dry_run_names_the_keeper_and_the_duplicate(umgebung, capsys):
    reg_path, key_dir = umgebung
    vorher = reg_path.read_bytes()

    assert tool.main(_argv(reg_path, key_dir)) == 0
    out = capsys.readouterr().out

    assert "Keeper     : hx7m2p" in out, out
    assert "stilllegen : 674zw5" in out, out
    assert "Doppelte Gruppen   : 1" in out, out
    assert "Trockenlauf: nichts geschrieben" in out, out
    # die Windows-Installation taucht nirgends als Dublette auf
    assert "ds5w6k" not in out and "6c5c59" not in out, out
    assert reg_path.read_bytes() == vorher, "der Trockenlauf hat das Register angefasst"
    assert not (key_dir / "agent_keys" / "hermes.key").exists()


def test_two_hosts_of_the_same_harness_are_not_duplicates(umgebung):
    reg_path, _key_dir = umgebung
    registry = _registry(reg_path)
    groups = tool.find_groups(registry["agents"])
    assert len(groups) == 1
    _key, keeper, retire = groups[0]
    assert keeper["id"] == "hx7m2p"
    assert [e["id"] for e in retire] == ["674zw5"]


def test_a_single_entry_per_group_is_left_alone(tmp_path):
    reg_path = tmp_path / "agents.json"
    reg_path.write_text(json.dumps({"agents": [
        _entry(id="a", harness="x"), _entry(id="b", harness="y")]}), encoding="utf-8")
    assert tool.find_groups(_registry(reg_path)["agents"]) == []


# ── Anwenden ──────────────────────────────────────────────────────────────

def test_apply_writes_the_key_retires_the_duplicate_and_prints_no_secret(umgebung, capsys):
    reg_path, key_dir = umgebung
    assert tool.main(_argv(reg_path, key_dir, "--apply")) == 0
    out = capsys.readouterr().out

    key_path = key_dir / "agent_keys" / "hermes.key"
    assert key_path.exists(), "keine Schluesseldatei geschrieben"
    assert oct(key_path.stat().st_mode & 0o777) == "0o600"
    key = key_path.read_text(encoding="utf-8").strip()
    assert len(key) > 20
    assert key not in out, "der Klartextschluessel steht in der Ausgabe"
    assert key[:8] not in out, "auch nicht gekuerzt"

    keeper = _by_id(reg_path, "hx7m2p")
    dup = _by_id(reg_path, "674zw5")
    assert keeper["key_hash"] and keeper.get("retired_at") is None
    assert dup.get("retired_at"), "die Dublette wurde nicht stillgelegt"
    assert dup.get("retired_into") == "hx7m2p"
    assert not dup.get("key_hash"), "der Schluessel der Dublette lebt noch"
    # Historie bleibt zurechenbar
    assert dup["writes"] == 11 and dup["reads"] == 0
    # die Windows-Installation ist unberuehrt
    assert _by_id(reg_path, "ds5w6k").get("retired_at") is None
    assert _by_id(reg_path, "6c5c59").get("retired_at") is None


def test_apply_is_idempotent(umgebung, capsys):
    reg_path, key_dir = umgebung
    assert tool.main(_argv(reg_path, key_dir, "--apply")) == 0
    capsys.readouterr()
    assert tool.main(_argv(reg_path, key_dir, "--apply")) == 0
    out = capsys.readouterr().out
    assert "Nichts zu tun." in out, out


def test_the_written_key_is_the_one_the_service_knows(umgebung):
    """Nach dem Lauf muss der Dienst den Dateischluessel dem Keeper zuordnen."""
    reg_path, key_dir = umgebung
    assert tool.main(_argv(reg_path, key_dir, "--apply")) == 0

    from nexus_memory import service_auth

    key = (key_dir / "agent_keys" / "hermes.key").read_text(encoding="utf-8").strip()
    old_verify = service_auth._agent_entries
    try:
        service_auth._agent_entries = lambda: _registry(reg_path)["agents"]
        agent_id, legacy = service_auth.verify_key(key)
    finally:
        service_auth._agent_entries = old_verify
    assert agent_id == "hx7m2p", "der Schluessel gehoert nicht dem Keeper"
    assert legacy is False


# ── der Dienst beachtet das Stilllegen ────────────────────────────────────

def test_a_retired_entry_cannot_authenticate(tmp_path):
    from nexus_memory import service_auth

    secret = "GEHEIM"
    agents = [
        _entry(id="alt", harness="hermes",
               key_hash=service_auth.hash_key(secret)),
        _entry(id="neu", harness="hermes", key_hash=service_auth.hash_key("ANDERS")),
    ]
    agents[0][service_auth.RETIRED_AT_FIELD] = "2026-11-27T00:00:00+00:00"
    original = service_auth._agent_entries
    try:
        service_auth._agent_entries = lambda: agents
        with pytest.raises(service_auth.ServiceAuthError):
            service_auth.verify_key(secret)
    finally:
        service_auth._agent_entries = original


def test_a_retired_entry_is_never_adopted_again(tmp_path):
    from nexus_memory import service_auth

    retired = _entry(id="alt", harness="hermes", key_hash=None, writes=11)
    retired[service_auth.RETIRED_AT_FIELD] = "2026-11-27T00:00:00+00:00"
    assert service_auth._find_enroll_target([retired], "hermes") is None, (
        "eine stillgelegte Leiche darf nicht wieder als Einschreibung dienen")
