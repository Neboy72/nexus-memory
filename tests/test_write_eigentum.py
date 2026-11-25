"""``POST /write`` (und ``remember`` mit ``memory_id``) darf fremde Punkte nicht
ueberschreiben.

Grund (belegt 24.11.2026): der ``update``-Weg prueft ``roles.is_owner``, der
``remember``-Zweig mit ``memory_id`` — und damit der HTTP-Schreibweg der
Plugins — prueft nichts. Ein ``know``-Agent ueberschrieb mit fremder ``id``
einen fremden Punkt (HTTP 200) und schrieb dabei ``agent`` auf sich selbst um.

Geprueft wird am echten ``MemoryStore.remember`` und an der echten ASGI-App
(``build_serve_app``) gegen eine eingebettete Wegwerf-Sammlung im Temp-Ordner —
nie gegen ``nexus_hf``. Der abgelehnte Fall wird nicht nur an der Antwort
gemessen: Payload und Vektor muessen danach Zeichen fuer Zeichen gleich sein,
und das Bewegungsprotokoll darf keine Zeile enthalten, die einen Schreibvorgang
behauptet.

Ohne den Fix: rot (der fremde Punkt wird ueberschrieben, HTTP 200).
"""

from __future__ import annotations

import json
import uuid

import pytest
from qdrant_client import models

from conftest import (
    bind_test_collection,
    embedded_qdrant,
    test_vector_name as _vector_name,
)

DIM = 2
COLLECTION = "zz_test_write_eigentum"
FAKT = "Der Verteiler liegt im Flur neben der Kellertuer und bleibt dauerhaft aus."

EIGNER = "eigner-bot"
FREMDER = "fremder-bot"
TEAM = "team-bot"


# ── Aufbau ───────────────────────────────────────────────────────────


@pytest.fixture
def welt(monkeypatch, embedded_qdrant):
    """Echter Store + echte ASGI-App auf einer eingebetteten Sammlung."""
    import nexus_memory.mcp_server as mcp

    client = embedded_qdrant()
    binding = bind_test_collection(client, COLLECTION, DIM)
    vector_name = _vector_name(DIM)

    monkeypatch.setattr(mcp, "COLLECTION_NAME", COLLECTION)
    # Zugaben, die mit dieser Frage nichts tun: Stichwortindex und
    # Zugriffszahlen. Das Bewegungsprotokoll bleibt ABSICHTLICH echt.
    monkeypatch.setattr(mcp, "_index_written_point", lambda *a, **k: None)
    monkeypatch.setattr(mcp, "_count_memory_access", lambda *a, **k: None)

    store = object.__new__(mcp.MemoryStore)
    store.client = client
    store.vector_name = vector_name
    store._hybrid_retriever = None
    store._skill_graph = None
    store._scope_centroids = None

    async def _embed(_text, is_query=True):
        return [0.1] * DIM

    store._embed = _embed
    monkeypatch.setattr(mcp, "get_store", lambda: store)
    return store, client, binding, vector_name


@pytest.fixture
def rolle(monkeypatch, isolated_nexus_data_dir):
    """Gibt einem Agenten eine Rolle und einen eigenen Schluessel (HTTP)."""
    from nexus_memory import agent_detect, service_auth

    def _rolle(agent_id: str, role: str = "know") -> dict:
        key = service_auth.issue_key(agent_id, agent_id)
        result = agent_detect.set_agent_role(agent_id, role)
        assert "error" not in result, result
        return {service_auth.AUTH_HEADER: key}

    return _rolle


def _seed(client, vector_name: str, pid: str, owner: str | None) -> None:
    payload = {
        "id": pid,
        "content": "alte Fassung, gehoert jemand anderem",
        "category": "fact",
        "access_level": "private",
        "lifecycle_status": "canonical",
        "source": "fremde-quelle",
        "provenance": {"source_type": "hermes-plugin", "created_by": "x"},
        "created_at": "2026-06-01T10:00:00+00:00",
        "use_count": 5,
        "salience": 0.9,
        "scope": "hermes",
    }
    if owner is not None:
        payload["agent"] = owner
    client.upsert(
        collection_name=COLLECTION,
        points=[models.PointStruct(
            id=pid,
            vector={vector_name: [0.25, 0.5]},
            payload=payload,
        )],
    )


def _stand(client, vector_name: str, pid: str) -> tuple[str, list[float]]:
    """Payload als vergleichbarer Text + Vektor — der "byte-gleiche" Vorher-Stand."""
    record = client.retrieve(
        collection_name=COLLECTION, ids=[pid],
        with_payload=True, with_vectors=True,
    )[0]
    payload = record.payload or {}
    vector = record.vector
    if isinstance(vector, dict):
        vector = vector.get(vector_name) or []
    return json.dumps(payload, sort_keys=True, ensure_ascii=False), list(vector)


def _client():
    from starlette.testclient import TestClient

    import nexus_memory.mcp_server as mcp

    return TestClient(mcp.build_serve_app(), base_url="http://127.0.0.1:9122",
                      client=("127.0.0.1", 40000))


def _write(headers: dict, pid: str, text: str = FAKT):
    return _client().post(
        "/write",
        json={
            "id": pid,
            "content": text,
            "category": "fact",
            "access_level": "private",
            "source": "openclaw_tool",
            "source_type": "chat",
        },
        headers=headers,
    )


def _protokoll() -> list[dict]:
    from nexus_memory import service_log

    zeilen: list[str] = []
    for path in sorted(service_log.log_dir().glob("service-*.jsonl")):
        zeilen.extend(path.read_text(encoding="utf-8").splitlines())
    return [json.loads(z) for z in zeilen if z.strip()]


# ── 1. Eigentuemer schreibt weiter (der legitime Wiederholungsfall) ──


def test_eigentuemer_ueberschreibt_seinen_punkt(welt, rolle):
    _store, client, _binding, vector_name = welt
    pid = str(uuid.uuid4())
    _seed(client, vector_name, pid, EIGNER)

    antwort = _write(rolle(EIGNER, "know"), pid)

    assert antwort.status_code == 200, antwort.text
    payload = client.retrieve(
        collection_name=COLLECTION, ids=[pid], with_payload=True,
        with_vectors=False,
    )[0].payload or {}
    assert payload["content"] == FAKT, "der eigene Punkt muss ueberschrieben werden"
    assert payload["agent"] == EIGNER


def test_work_rolle_darf_einen_fremden_punkt_ueberschreiben(welt, rolle):
    """``is_owner`` laesst ``work``/``admin`` fremde Eintraege anfassen — wie im
    ``update``-Weg. Das OpenClaw-Plugin laeuft als ``work``."""
    _store, client, _binding, vector_name = welt
    pid = str(uuid.uuid4())
    _seed(client, vector_name, pid, FREMDER)

    antwort = _write(rolle(TEAM, "work"), pid)

    assert antwort.status_code == 200, antwort.text
    payload = client.retrieve(
        collection_name=COLLECTION, ids=[pid], with_payload=True,
        with_vectors=False,
    )[0].payload or {}
    assert payload["content"] == FAKT


# ── 2. Fremder ``know``-Agent: 403, Punkt unveraendert ───────────────


def test_fremder_know_agent_bekommt_403_und_der_punkt_bleibt_gleich(welt, rolle):
    _store, client, _binding, vector_name = welt
    pid = str(uuid.uuid4())
    _seed(client, vector_name, pid, FREMDER)
    vorher_payload, vorher_vektor = _stand(client, vector_name, pid)

    antwort = _write(rolle(EIGNER, "know"), pid)

    assert antwort.status_code == 403, antwort.text
    assert antwort.json().get("error") == "not owner"
    nachher_payload, nachher_vektor = _stand(client, vector_name, pid)
    assert nachher_payload == vorher_payload, (
        "ein abgelehnter Schreibvorgang hat den fremden Punkt veraendert"
    )
    assert nachher_vektor == vorher_vektor, (
        "ein abgelehnter Schreibvorgang hat den Vektor des fremden Punktes ersetzt"
    )


# ── 3. Altbestand ohne Eigentuemer: fail-closed fuer ``know`` ─────────


def test_punkt_ohne_eigentuemer_wird_fuer_know_abgelehnt(welt, rolle):
    _store, client, _binding, vector_name = welt
    pid = str(uuid.uuid4())
    _seed(client, vector_name, pid, None)
    vorher = _stand(client, vector_name, pid)

    antwort = _write(rolle(EIGNER, "know"), pid)

    assert antwort.status_code == 403, antwort.text
    assert _stand(client, vector_name, pid) == vorher


# ── 4. Der abgelehnte Fall behauptet keinen Schreibvorgang ───────────


def test_abgelehnter_schreibvorgang_hat_keine_schreibzeile(welt, rolle):
    _store, client, _binding, vector_name = welt
    pid = str(uuid.uuid4())
    _seed(client, vector_name, pid, FREMDER)

    antwort = _write(rolle(EIGNER, "know"), pid)
    assert antwort.status_code == 403, antwort.text

    zeilen = [z for z in _protokoll() if z.get("target_id") == pid]
    assert [z.get("action") for z in zeilen] == ["denied"], (
        f"erwartet genau eine Zeile 'denied', gefunden: {zeilen}"
    )
    assert zeilen[0].get("result") == "refused"
    assert not [z for z in zeilen if z.get("action") == "write"], (
        "ein abgelehnter Schreibvorgang steht als Schreibvorgang im Protokoll"
    )


# ── 5. Ein NEUER Punkt mit gelieferter Kennung bleibt erlaubt ────────


def test_neuer_punkt_mit_gelieferter_kennung_ist_kein_ueberschreiben(welt, rolle):
    """Die Retry-Queue liefert eine Kennung, die es noch nicht gibt: anlegen ok."""
    _store, client, _binding, _vector_name_ = welt
    pid = str(uuid.uuid4())

    antwort = _write(rolle(EIGNER, "know"), pid)

    assert antwort.status_code == 200, antwort.text
    payload = client.retrieve(
        collection_name=COLLECTION, ids=[pid], with_payload=True,
        with_vectors=False,
    )[0].payload or {}
    assert payload["content"] == FAKT
    assert payload["agent"] == EIGNER


def test_neuer_punkt_ohne_memory_id_ist_erlaubt(welt, rolle):
    """Ohne ``id`` ist es kein Ueberschreiben — der Weg bleibt offen."""
    _store, client, _binding, _vector_name_ = welt

    antwort = _client().post(
        "/write",
        json={
            "content": FAKT,
            "category": "fact",
            "access_level": "private",
            "source": "openclaw_tool",
            "source_type": "chat",
        },
        headers=rolle(EIGNER, "know"),
    )

    assert antwort.status_code == 200, antwort.text
    assert antwort.json()["status"] == "ok"
    neu = antwort.json()["id"]
    payload = client.retrieve(
        collection_name=COLLECTION, ids=[neu], with_payload=True,
        with_vectors=False,
    )[0].payload or {}
    assert payload["content"] == FAKT
    assert payload["agent"] == EIGNER


def test_hermes_punkt_und_ds_agent_schluessel_wird_abgelehnt(welt, rolle):
    """Der gemeldete Fall, mit den echten Kennungen aus dem Register:
    Hermes (``hx7m2p``) schreibt den Punkt, DSH (``ds5w6k``, Rolle ``know``)
    versucht ihn mit fremder Kennung zu ueberschreiben."""
    _store, client, _binding, vector_name = welt
    pid = str(uuid.uuid4())
    _seed(client, vector_name, pid, "hx7m2p")
    vorher = _stand(client, vector_name, pid)

    antwort = _write(rolle("ds5w6k", "know"), pid)

    assert antwort.status_code == 403, antwort.text
    assert _stand(client, vector_name, pid) == vorher
    zeilen = [z for z in _protokoll() if z.get("target_id") == pid]
    assert [z.get("action") for z in zeilen] == ["denied"]


# ── 6. Derselbe Zweig ohne HTTP (Bibliothek) ─────────────────────────


@pytest.mark.asyncio
async def test_remember_mit_fremder_kennung_lehnt_in_der_bibliothek_ab(welt):
    store, client, _binding, vector_name = welt
    pid = str(uuid.uuid4())
    _seed(client, vector_name, pid, FREMDER)
    vorher = _stand(client, vector_name, pid)

    ergebnis = await store.remember(
        FAKT, category="fact", source="probe",
        memory_id=pid, actor=EIGNER, via="mcp",
    )

    assert ergebnis.get("status") == "denied", ergebnis
    assert ergebnis.get("error") == "not owner"
    assert _stand(client, vector_name, pid) == vorher
