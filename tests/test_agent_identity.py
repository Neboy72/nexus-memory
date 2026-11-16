"""Kurze Kennung, freies Label, Badge (Punkt 4b, 15.11.2026).

Die Kennung ist ein Griff, kein Name: sechs Zeichen, erzeugt beim Ausstellen,
nicht abgeleitet aus Typ oder Rechner (zwei Hermes auf einem Mac mini sind
zwei Agenten). Das Label ist frei waehlbar, darf doppelt vorkommen und laesst
sich aendern, ohne die Kennung anzufassen.

Das Protokoll traegt beides: ``actor`` (Kennung) und ``actor_label`` (Label
zum Zeitpunkt der Handlung). Ein spaeter geaendertes Label schreibt die
Historie nicht um — sonst waere die Zeile von gestern morgen falsch beschriftet.

Keine Datenmigration: diese Tests beruehren nur ``agents.json`` und das
Protokoll im Temp-Verzeichnis (autouse-Fixtures in conftest.py).
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import nexus_memory.service_auth as service_auth
from nexus_memory import service_log

ISO = "2026-01-01T00:00:00+00:00"
KENNUNG_RE = re.compile(r"^Kennung: (\S+)$", re.MULTILINE)


def _entry(agent_id: str, name: str = "", **extra) -> dict:
    entry = {
        "id": agent_id, "name": name or agent_id, "icon": "x",
        "trust_level": "public", "install_type": "plugin+mcp",
        "config_dir": None, "connected_at": ISO, "last_seen": ISO,
        "reads": 0, "writes": 0,
    }
    entry.update(extra)
    return entry


def _registry_file(path: Path, *entries: dict) -> None:
    path.write_text(json.dumps({"agents": list(entries)}, indent=2) + "\n",
                    encoding="utf-8")


def _registry(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# ===========================================================================
# 1. --issue ohne --id erzeugt eine kurze, freie Kennung
# ===========================================================================


def test_issue_ohne_id_erzeugt_sechs_zeichen(isolated_agents_registry, capsys):
    """Sechs Zeichen aus a-z0-9 ohne l/o/0/1 — und zwei Aufrufe kollidieren nicht."""
    _registry_file(isolated_agents_registry)

    ids = []
    for _ in range(2):
        assert service_auth.main(["--issue"]) == 0
        match = KENNUNG_RE.search(capsys.readouterr().out)
        assert match, "die Ausgabe muss die Kennung nennen"
        ids.append(match.group(1))

    for agent_id in ids:
        assert len(agent_id) == 6, agent_id
        assert set(agent_id) <= set(service_auth.ID_ALPHABET), agent_id
        # Verwechslungssicher: diese vier Zeichen kommen nie vor.
        assert not set(agent_id) & set("lo01"), agent_id

    assert ids[0] != ids[1], "zwei Aufrufe duerfen nicht dieselbe Kennung liefern"
    assert {a["id"] for a in _registry(isolated_agents_registry)["agents"]} == set(ids)


def test_issue_zeigt_kennung_label_und_schluessel(isolated_agents_registry, capsys):
    """Die Ausgabe nennt Kennung und Label; der Schluessel steht genau einmal da."""
    _registry_file(isolated_agents_registry)

    assert service_auth.main(["--issue", "--name", "Hermes Buero"]) == 0
    out = capsys.readouterr().out

    agent_id = KENNUNG_RE.search(out).group(1)
    assert f"Label:   Hermes Buero" in out
    key = [line for line in out.splitlines() if line.strip()][-1].strip()
    assert len(key) >= 40, out
    assert out.count(key) == 1
    assert key not in isolated_agents_registry.read_text(encoding="utf-8")


def test_generate_agent_id_meidet_auch_formerly(isolated_agents_registry):
    """Ein alter Name bleibt Alias — eine neue Kennung darf ihn nicht tragen."""
    _registry_file(isolated_agents_registry,
                   _entry("hermes-mac-mini", formerly=["hermes"]))

    taken = service_auth._taken_agent_names()

    assert taken == {"hermes-mac-mini", "hermes"}


# ===========================================================================
# 2. --label aendert nur das Label
# ===========================================================================


def test_label_aendert_nur_den_namen(isolated_agents_registry, capsys):
    """Die Kennung und der eigene Schluessel bleiben, nur ``name`` wandert."""
    _registry_file(isolated_agents_registry, _entry("hx7m2p", "Hermes Agent"))
    key = service_auth.issue_key("hx7m2p")
    before = _registry(isolated_agents_registry)["agents"][0]

    assert service_auth.main(
        ["--label", "--id", "hx7m2p", "--set", "Hermes Buero"]) == 0

    after = _registry(isolated_agents_registry)["agents"][0]
    assert after["id"] == "hx7m2p"
    assert after["name"] == "Hermes Buero"
    assert after["key_hash"] == before["key_hash"]
    assert service_auth.verify_key(key)[0] == "hx7m2p"
    assert "Hermes Buero" in capsys.readouterr().out


def test_label_darf_doppelt_vorkommen(isolated_agents_registry):
    """Zwei „Hermes Agent" sind erlaubt — die Kennung unterscheidet sie."""
    _registry_file(isolated_agents_registry, _entry("hx7m2p"), _entry("q4rt9w"))

    assert service_auth.main(
        ["--label", "--id", "hx7m2p", "--set", "Hermes Agent"]) == 0
    assert service_auth.main(
        ["--label", "--id", "q4rt9w", "--set", "Hermes Agent"]) == 0

    names = [a["name"] for a in _registry(isolated_agents_registry)["agents"]]
    assert names == ["Hermes Agent", "Hermes Agent"]


def test_label_eines_unbekannten_agenten_aendert_nichts(isolated_agents_registry,
                                                        capsys):
    _registry_file(isolated_agents_registry, _entry("hx7m2p"))
    before = isolated_agents_registry.read_bytes()

    assert service_auth.main(["--label", "--id", "gibtsnicht", "--set", "X"]) == 2

    assert isolated_agents_registry.read_bytes() == before
    assert "unknown agent id" in capsys.readouterr().err


# ===========================================================================
# 3. Protokollzeile: Kennung und Label
# ===========================================================================


def test_protokollzeile_traegt_kennung_und_label(isolated_agents_registry):
    _registry_file(isolated_agents_registry, _entry("hx7m2p", "Hermes Buero"))

    assert service_log.record("write", actor="hx7m2p") is not None

    entry = service_log.read_events(limit=1)[0]
    assert entry["actor"] == "hx7m2p"
    assert entry["actor_label"] == "Hermes Buero"


def test_label_aenderung_schreibt_die_alte_zeile_nicht_um(isolated_agents_registry):
    """Das Label ist ein Snapshot: die Historie bleibt lesbar, wie sie war."""
    _registry_file(isolated_agents_registry, _entry("hx7m2p", "Hermes Buero"))

    service_log.record("write", actor="hx7m2p")
    assert service_auth.main(
        ["--label", "--id", "hx7m2p", "--set", "Hermes Zuhause"]) == 0
    service_log.record("write", actor="hx7m2p")

    # ``read_events`` liefert die juengste Zeile zuerst.
    newer, older = service_log.read_events(limit=2, action="write")
    assert older["actor_label"] == "Hermes Buero"
    assert newer["actor_label"] == "Hermes Zuhause"
    assert older["actor"] == newer["actor"] == "hx7m2p"


def test_zeile_ohne_registereintrag_hat_kein_label(isolated_agents_registry):
    """Altbestand und Systemvorgaenge: kein Label, aber dieselbe Kennung."""
    _registry_file(isolated_agents_registry, _entry("hx7m2p", "Hermes Buero"))

    service_log.record("write", actor="fremder-aufrufer")
    service_log.record("backup", actor="system:consolidation")

    entries = service_log.read_events(limit=2)
    foreign = next(e for e in entries if e["actor"] == "fremder-aufrufer")
    system = next(e for e in entries if e["actor"] == "system:consolidation")
    assert "actor_label" not in foreign
    assert "actor_label" not in system


# ===========================================================================
# 4. --list zeigt Kennung, Label, formerly und Rolle
# ===========================================================================


def test_list_zeigt_kennung_label_formerly_und_rolle(isolated_agents_registry,
                                                     capsys):
    _registry_file(
        isolated_agents_registry,
        _entry("hx7m2p", "Hermes Buero", role="work", formerly=["hermes"]),
        _entry("q4rt9w", "OpenClaw"),
    )

    assert service_auth.main(["--list"]) == 0
    listing = capsys.readouterr().out

    header = listing.splitlines()[0]
    assert "KENNUNG" in header and "LABEL" in header
    assert "FRUEHER" in header and "ROLLE" in header

    line = next(row for row in listing.splitlines() if row.startswith("hx7m2p"))
    assert "Hermes Buero" in line
    assert "hermes" in line
    assert "work" in line
