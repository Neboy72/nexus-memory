"""Einschreiben uebernimmt NIE einen Eintrag, der schon einen Schluessel hat.

Geaendert am 16.11.2026: ``service_auth._find_enroll_target`` ueberspringt
Eintraege mit gesetztem ``key_hash`` in allen drei Stufen (``harness``,
``formerly``, normalisierter ``name``). Vorher ersetzte eine zweite Anmeldung
desselben Harness — etwa Windows neben dem Mac — den Schluessel des
bestehenden Eintrags und sperrte die erste Installation aus.

Jetzt entsteht in diesem Fall ein NEUER Eintrag mit eigener Kennung; der alte
behaelt seinen ``key_hash`` und sein Schluessel bleibt gueltig. Ein Eintrag
OHNE Schluessel wird weiterhin uebernommen (Altdaten aus der Zeit vor
``harness``). Wer seinen Schluessel verliert, bekommt also einen neuen Eintrag
mit Standardrolle — das ist gewollt.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

OLD_KEY = "old-own-key-of-the-first-installation"


def _write_registry(path: Path, agents: list[dict]) -> None:
    path.write_text(json.dumps({"agents": agents}), encoding="utf-8")


def _agents(path: Path) -> dict[str, dict]:
    entries = json.loads(path.read_text(encoding="utf-8")).get("agents", [])
    return {entry["id"]: entry for entry in entries}


def _keyed_entry(entry_id: str, name: str, key_hash: str, **extra) -> dict:
    """Ein Eintrag, der sich schon einmal angemeldet hat (hat einen Schluessel)."""
    entry = {
        "id": entry_id,
        "name": name,
        "role": "know",
        "trust_level": "trusted",
        "key_hash": key_hash,
    }
    entry.update(extra)
    return entry


# Jede der drei Suchstufen von ``_find_enroll_target`` bekommt einen eigenen
# Eintrag, damit der Test wirklich die Stufe trifft und nicht eine spaetere.
_KEYED_MATCHES = [
    pytest.param("DSH Mac", {"harness": "dsh"}, id="stage1-harness"),
    pytest.param("DSH Mac", {"formerly": ["dsh"]}, id="stage2-formerly"),
    pytest.param("DSH", {}, id="stage3-normalized-name"),
]


@pytest.mark.parametrize("label, match", _KEYED_MATCHES)
def test_enroll_creates_a_new_entry_instead_of_taking_a_keyed_one(
        label, match, isolated_agents_registry):
    """Fall 1: der Schluessel des bestehenden Eintrags bleibt unangetastet."""
    from nexus_memory import service_auth

    _write_registry(isolated_agents_registry, [
        _keyed_entry("6c5c59", label, service_auth.hash_key(OLD_KEY), **match),
    ])

    payload = service_auth.enroll("dsh")

    assert payload["created"] is True, "ein Eintrag MIT Schluessel wird nicht uebernommen"
    assert payload["agent_id"] != "6c5c59"

    agents = _agents(isolated_agents_registry)
    assert len(agents) == 2
    assert agents["6c5c59"]["key_hash"] == service_auth.hash_key(OLD_KEY), \
        "der alte Schluessel behaelt seinen Hash"
    assert service_auth.verify_key(OLD_KEY) == ("6c5c59", False), \
        "die erste Installation bleibt angemeldet"
    assert service_auth.verify_key(payload["key"]) == (payload["agent_id"], False)


def test_enroll_still_adopts_an_entry_without_a_key(isolated_agents_registry):
    """Fall 2: Altdaten ohne ``key_hash`` werden weiter uebernommen."""
    from nexus_memory import service_auth

    _write_registry(isolated_agents_registry, [{
        "id": "ds5w6k",
        "name": "DSH",
        "harness": "dsh",
        "role": "know",
        "trust_level": "trusted",
    }])

    payload = service_auth.enroll("dsh")

    assert payload["created"] is False
    assert payload["agent_id"] == "ds5w6k"

    agents = _agents(isolated_agents_registry)
    assert len(agents) == 1
    assert agents["ds5w6k"]["key_hash"] == service_auth.hash_key(payload["key"])
    assert service_auth.verify_key(payload["key"]) == ("ds5w6k", False)
