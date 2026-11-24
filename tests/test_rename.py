"""Umbenennen mit Uebergang (Punkt 4a).

Warum: die Kennung eines Agenten ist gleichzeitig sein Bereich und sein
Eigentuemer-Feld. Wer umbenannt wird, verliert ohne Uebergang seine bisherigen
Protokolle und kann seine eigenen alten Eintraege nicht mehr zurueckziehen.
Deshalb traegt der Eintrag den alten Namen als ``formerly``: ein Aufrufer, der
sich noch als ``hermes`` ausweist, wird als ``hermes-mac-mini`` behandelt — im
Eintrag und im Protokoll steht immer die neue Kennung.

Keine Datenmigration: diese Tests beruehren nur ``agents.json``; die Sammlung
wird in ``/write`` durch einen Fake ersetzt.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

SHARED_KEY = "test-global-key"
FAKT = "Der Verteiler liegt im Flur neben der Kellertuer"  # 47 Zeichen
ALT = "hermes"
NEU = "hermes-mac-mini"
ISO = "2026-01-01T00:00:00+00:00"


def _entry(agent_id: str, **extra) -> dict:
    entry = {
        "id": agent_id, "name": agent_id, "icon": "x",
        "trust_level": "private", "install_type": "plugin+mcp",
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


@pytest.fixture(autouse=True)
def _service_key(monkeypatch):
    monkeypatch.setenv("NEXUS_SERVICE_AUTH_KEY", SHARED_KEY)
    monkeypatch.delenv("NEXUS_AGENT_ID", raising=False)


class _FakeQdrant:
    """Nur da, damit ``remember`` ein Attribut vorfindet (Kategorie session)."""

    def query_points(self, **_kwargs):
        raise AssertionError("diese Kategorie darf keine Aehnlichkeitssuche starten")


@pytest.fixture
def written(monkeypatch):
    """Faengt den echten Schreibvorgang ab und gibt die Punkte zurueck."""
    import nexus_memory.mcp_server as mcp
    import nexus_memory.collection_vectors as collection_vectors

    points: list = []
    monkeypatch.setattr(
        collection_vectors, "upsert_named",
        lambda _client, _collection, pts, _vector_name, **_kw: points.extend(pts),
    )
    monkeypatch.setattr(mcp, "_index_written_point", lambda _payload: None)

    store = object.__new__(mcp.MemoryStore)
    store.vector_name = None
    store._hybrid_retriever = None
    store._skill_graph = None
    store._scope_centroids = None
    store.client = _FakeQdrant()

    async def _embed(_text, is_query=True):
        return [0.1] * 4

    store._embed = _embed
    monkeypatch.setattr(mcp, "get_store", lambda: store)
    return points


def _write(key: str):
    """Ein echter ``POST /write`` mit dem EIGENEN Schluessel des Aufrufers.

    15.11.2026: der Installationsschluessel schreibt keine Daten mehr — der
    Absender ist der Schluessel, nicht der Kopf.
    """
    from starlette.testclient import TestClient

    import nexus_memory.mcp_server as mcp
    from nexus_memory import service_auth

    headers = {service_auth.AUTH_HEADER: key}
    client = TestClient(mcp.build_serve_app(), base_url="http://127.0.0.1:9122")
    return client.post("/write", json={
        "content": FAKT, "category": "session", "access_level": "private",
        "source": "hermes-plugin", "source_type": "session",
    }, headers=headers)


# ===========================================================================
# 1. Nach --rename schreibt der alte Header unter der neuen Kennung
# ===========================================================================


def test_rename_traegt_den_schluessel_auf_die_neue_kennung(
        isolated_agents_registry, written):
    """Ein VOR der Umbenennung ausgestellter Schluessel liefert die neue Kennung."""
    import nexus_memory.service_auth as service_auth
    from nexus_memory import service_log

    _registry_file(isolated_agents_registry, _entry(ALT))
    key = service_auth.issue_key(ALT, ALT)

    assert service_auth.main(["--rename", "--from", ALT, "--to", NEU]) == 0

    r = _write(key)

    assert r.status_code == 200, r.text
    assert len(written) == 1
    assert written[0].payload["agent"] == NEU
    # Und im Protokoll steht derselbe Absender — nicht der alte Name.
    write_lines = service_log.read_events(limit=5, action="write")
    assert write_lines and write_lines[0]["actor"] == NEU


def test_umbenennung_ruehrt_die_sammlung_nicht_an(isolated_agents_registry):
    """``--rename`` schreibt in agents.json und sonst nirgendwohin."""
    import nexus_memory.service_auth as service_auth
    from nexus_memory import service_log

    _registry_file(isolated_agents_registry, _entry(ALT, reads=3393, writes=15831))

    assert service_auth.main(["--rename", "--from", ALT, "--to", NEU]) == 0

    entry = _registry(isolated_agents_registry)["agents"][0]
    assert entry["id"] == NEU
    assert entry["formerly"] == [ALT]
    # Zaehler und Herkunft bleiben beim Eintrag — auch der eigene Schluessel
    # (hier keiner) wandert mit, weil der Eintrag derselbe ist.
    assert (entry["reads"], entry["writes"]) == (3393, 15831)
    # Kein weiterer Vorgang als die eine Protokollzeile der Umbenennung.
    assert [e["action"] for e in service_log.read_events(limit=10)] == ["role_change"]


# ===========================================================================
# 2. Der alte Schluessel bleibt gueltig
# ===========================================================================


def test_alte_eigene_schluessel_liefert_die_neue_kennung(isolated_agents_registry):
    import nexus_memory.service_auth as service_auth

    _registry_file(isolated_agents_registry, _entry(ALT))
    key = service_auth.issue_key(ALT)

    assert service_auth.main(["--rename", "--from", ALT, "--to", NEU]) == 0

    assert service_auth.verify_key(key, declared="openclaw") == (NEU, False)
    # Der Alias gilt auch fuer den globalen Schluessel.
    assert service_auth.verify_key(SHARED_KEY, declared=ALT) == (NEU, True)


# ===========================================================================
# 3. Doppelte Zuordnung = Ladefehler (fail closed)
# ===========================================================================


def test_zwei_agenten_mit_demselben_formerly_verhindern_das_laden(
        isolated_agents_registry):
    from nexus_memory import agent_detect

    _registry_file(
        isolated_agents_registry,
        _entry(NEU, formerly=[ALT]),
        _entry("anderer-agent", formerly=[ALT]),
    )

    with pytest.raises(ValueError) as excinfo:
        agent_detect.load_agents_registry()

    message = str(excinfo.value)
    assert ALT in message
    assert NEU in message and "anderer-agent" in message
    assert "ambiguous" in message


def test_zweimal_derselbe_name_bei_einem_agenten_ist_kein_konflikt(
        isolated_agents_registry):
    from nexus_memory import agent_detect

    _registry_file(isolated_agents_registry, _entry(NEU, formerly=[ALT, ALT]))

    assert agent_detect.resolve_former_name(ALT) == NEU


# ===========================================================================
# 4. --rename auf eine existierende Kennung bricht ab, Datei unveraendert
# ===========================================================================


@pytest.mark.parametrize("ziel", [ALT, "openclaw"])
def test_rename_auf_bekannte_kennung_aendert_nichts(isolated_agents_registry,
                                                    capsys, ziel):
    import nexus_memory.service_auth as service_auth

    _registry_file(isolated_agents_registry, _entry(ALT), _entry("openclaw"))
    before = isolated_agents_registry.read_bytes()

    assert service_auth.main(["--rename", "--from", ALT, "--to", ziel]) == 2

    assert isolated_agents_registry.read_bytes() == before
    assert "service_auth:" in capsys.readouterr().err


def test_rename_einer_unbekannten_kennung_aendert_nichts(isolated_agents_registry,
                                                         capsys):
    import nexus_memory.service_auth as service_auth

    _registry_file(isolated_agents_registry, _entry("openclaw"))
    before = isolated_agents_registry.read_bytes()

    assert service_auth.main(["--rename", "--from", ALT, "--to", NEU]) == 2

    assert isolated_agents_registry.read_bytes() == before
    assert "unknown agent id" in capsys.readouterr().err


def test_rename_verweigert_einen_namen_der_schon_alias_eines_anderen_ist(
        isolated_agents_registry, capsys):
    import nexus_memory.service_auth as service_auth

    _registry_file(isolated_agents_registry,
                   _entry(ALT), _entry("anderer-agent", formerly=[ALT]))
    before = isolated_agents_registry.read_bytes()

    assert service_auth.main(["--rename", "--from", ALT, "--to", NEU]) == 2

    assert isolated_agents_registry.read_bytes() == before
    assert "ambiguous" in capsys.readouterr().err


# ===========================================================================
# 5. Protokollzeile der Umbenennung
# ===========================================================================


def test_umbenennung_hinterlaesst_eine_rollenwechsel_zeile(
        isolated_agents_registry):
    import nexus_memory.service_auth as service_auth
    from nexus_memory import service_log

    _registry_file(isolated_agents_registry, _entry(ALT))

    assert service_auth.main(["--rename", "--from", ALT, "--to", NEU]) == 0

    events = service_log.read_events(limit=5, action="role_change")
    assert len(events) == 1
    entry = events[0]
    assert entry["action"] == "role_change"
    assert entry["result"] == "ok"
    assert entry["reason"] == f"rename {ALT} to {NEU}"
    assert entry["target_id"] == NEU
    assert entry["via"] == "cli"


# ===========================================================================
# 6. --list zeigt den alten Namen
# ===========================================================================


def test_list_zeigt_formerly(isolated_agents_registry, capsys):
    import nexus_memory.service_auth as service_auth

    _registry_file(isolated_agents_registry, _entry(ALT))

    assert service_auth.main(["--rename", "--from", ALT, "--to", NEU]) == 0
    capsys.readouterr()

    assert service_auth.main(["--list"]) == 0

    listing = capsys.readouterr().out
    line = next(row for row in listing.splitlines() if row.startswith(NEU))
    assert ALT in line, listing
    assert "FRUEHER" in listing


# ===========================================================================
# 7. Ein Name, den es nicht gibt — und der kein formerly ist
# ===========================================================================


def test_unbekannter_name_bleibt_der_name_des_aufrufers(isolated_agents_registry,
                                                        written):
    """Der Alias erfindet keine Kennung: unbekannt bleibt, was der Aufrufer sagt."""
    from nexus_memory import agent_detect

    _registry_file(isolated_agents_registry, _entry(NEU, formerly=[ALT]))

    import nexus_memory.service_auth as service_auth

    assert agent_detect.resolve_former_name("niemand") == "niemand"
    assert agent_detect.resolve_agent_id("niemand") == "niemand"

    key = service_auth.issue_key("niemand", "niemand")
    r = _write(key)

    assert r.status_code == 200, r.text
    assert written[0].payload["agent"] == "niemand"
    # Er bleibt ein eigener Aufrufer — nicht dem umbenannten Agenten
    # zugeschlagen, dessen ``formerly`` nur ``hermes`` ist.
    ids = {a["id"] for a in _registry(isolated_agents_registry)["agents"]}
    assert ids == {NEU, "niemand"}
