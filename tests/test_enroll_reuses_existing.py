"""Einschreiben uebernimmt den BESTEHENDEN Agenten (15.11.2026).

Ein Register aus der Zeit vor ``harness`` kennt seine Agenten nur unter
``formerly`` oder ``name``. Wer sich damals schon angemeldet hat, darf durch
eine neue Einschreibung keinen zweiten Eintrag bekommen — sonst bliebe der
alte als Waise stehen und der Nutzer verloere seinen Eigentuemer-Status.

Drei Stufen, erster Treffer gewinnt: ``harness``, ``formerly``, normalisierter
``name``. Bei mehreren Treffern in Stufe 2/3 gewinnt der juengste
``last_seen``. ``unidentified`` und ``dashboard`` werden nie uebernommen.

Geaendert am 16.11.2026: uebernommen wird nur noch ein Eintrag OHNE eigenen
Schluessel. Ein Eintrag mit ``key_hash`` gehoert einer bereits angemeldeten
Installation; eine zweite Anmeldung legt fuer ihn einen neuen Eintrag an,
statt seinen Schluessel zu ersetzen. Die Uebernahme-Nachweise hier betreffen
deshalb ausschliesslich schluessellose Alteintraege.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def _write_registry(path: Path, agents: list[dict]) -> None:
    path.write_text(json.dumps({"agents": agents}), encoding="utf-8")


def _agents(path: Path) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8")).get("agents", [])


def test_enroll_reuses_entry_by_formerly(isolated_agents_registry):
    """``hx7m2p`` traegt kein ``harness``, aber ``formerly: ["hermes"]``."""
    from nexus_memory import service_auth

    _write_registry(isolated_agents_registry, [{
        "id": "abc123",
        "name": "Hermes Agent",
        "formerly": ["hermes"],
        "role": "admin",
        "trust_level": "private",
    }])

    payload = service_auth.enroll("hermes")

    assert payload["created"] is False
    assert payload["agent_id"] == "abc123"

    agents = _agents(isolated_agents_registry)
    assert len(agents) == 1, "der bestehende Eintrag wird uebernommen, kein zweiter"
    entry = agents[0]
    assert entry["harness"] == "hermes"
    assert entry["role"] == "admin", "die Rolle gehoert dem Nutzer"
    assert entry["trust_level"] == "private", "das Vertrauen gehoert dem Nutzer"
    assert entry["enroll_count"] == 1


def test_enroll_reuses_newest_entry_with_the_same_name(isolated_agents_registry):
    """Zwei "DSH" — uebernommen wird der zuletzt gesehene, nicht der erste."""
    from nexus_memory import service_auth

    _write_registry(isolated_agents_registry, [
        {
            "id": "old111",
            "name": "DSH",
            "role": "know",
            "trust_level": "trusted",
            "last_seen": "2026-11-15T21:50:47.858816+00:00",
        },
        {
            "id": "new222",
            "name": "DSH",
            "role": "know",
            "trust_level": "trusted",
            "last_seen": "2026-11-15T21:55:26.350920+00:00",
        },
    ])

    payload = service_auth.enroll("dsh")

    assert payload["created"] is False
    assert payload["agent_id"] == "new222", "der juengste last_seen gewinnt"
    assert len(_agents(isolated_agents_registry)) == 2
    adopted = {a["id"]: a for a in _agents(isolated_agents_registry)}["new222"]
    assert adopted["harness"] == "dsh"


def test_enroll_never_overwrites_the_unidentified_entry(isolated_agents_registry):
    """``unidentified`` ist kein Agent — die Anmeldung legt einen neuen an."""
    from nexus_memory import service_auth

    _write_registry(isolated_agents_registry, [{
        "id": "unidentified",
        "name": "unidentified",
        "trust_level": "public",
        "install_type": "unknown",
    }])

    payload = service_auth.enroll("unidentified")

    assert payload["created"] is True
    assert payload["agent_id"] != "unidentified"

    agents = {a["id"]: a for a in _agents(isolated_agents_registry)}
    assert len(agents) == 2, "der anonyme Eintrag bleibt, daneben ein neuer"
    anonymous = agents["unidentified"]
    assert anonymous.get("harness") is None
    assert anonymous["trust_level"] == "public"
    assert agents[payload["agent_id"]]["harness"] == "unidentified"

    # Seit 16.11.2026 findet die naechste Einschreibung ihren eigenen Eintrag
    # NICHT wieder — er hat jetzt einen Schluessel und ist damit kein
    # Uebernahme-Kandidat mehr. Sie legt einen weiteren an; der anonyme Platz
    # und der erste Eintrag bleiben unberuehrt.
    again = service_auth.enroll("unidentified")
    assert again["created"] is True
    assert again["agent_id"] != payload["agent_id"]
    agents = {a["id"]: a for a in _agents(isolated_agents_registry)}
    assert len(agents) == 3
    assert agents["unidentified"].get("harness") is None
    assert agents[payload["agent_id"]]["key_hash"], "der erste Schluessel bleibt"
