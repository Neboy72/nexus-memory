"""Das Zusammenfuehren sitzt im Schreibweg selbst — nicht bei den Aufrufern.

Auftrag 24.11.2026: Bis dahin hing das Zusammenfuehren an drei Aufrufstellen;
jeder neue Schreiber konnte es vergessen. Jetzt macht es
``collection_vectors.upsert_named``: es liest den vorhandenen Punkt, fuehrt ihn
mit ``nexus_memory.payload_merge`` zusammen und schreibt erst dann.

Geprueft wird gegen eine eingebettete Wegwerf-Sammlung im Temp-Ordner — nie
gegen ``nexus_hf``:

* ein Aufrufer, der ``payload_merge`` nicht kennt, bekommt das Zusammenfuehren
  trotzdem (Erinnerung und Entity-Punkt),
* eine frische Kennung kostet keinen Lesezugriff (``existing={}``), auch auf
  dem echten ``remember``-Weg ohne ``memory_id``,
* eine bereits gelesene Payload (``existing={id: …}``) spart den zweiten Read,
* ``merge=False`` ist der ausdrueckliche Ersatz-Fall (Wiederherstellung).

Ohne den Hook: rot (der zweite Schreibvorgang setzt die Felder zurueck).
"""

from __future__ import annotations

import uuid

import pytest
from qdrant_client import models

from conftest import (
    bind_test_collection,
    embedded_qdrant,
    test_vector_name as _vector_name,
)

DIM = 2
COLLECTION = "zz_test_upsert_merge"
NEUER_TEXT = "Die neue Fassung beschreibt denselben Punkt voellig anders."

ALT_TS = "2026-06-01T10:00:00+00:00"
ALT_PROVENANCE = {
    "source_type": "hermes-plugin",
    "created_by": "frueherer-schreiber",
    "timestamp": ALT_TS,
    "confidence": 0.9,
}
KANTE = {
    "edge_id": "edge-1",
    "target_fact_id": "ziel-punkt",
    "relation": "part_of",
    "status": "active",
    "reason": "nexus_remember",
}


class _ZaehlenderClient:
    """Delegiert alles an den echten Client und zaehlt die Lesezugriffe."""

    def __init__(self, inner):
        self._inner = inner
        self.retrieve_aufrufe = 0

    def retrieve(self, **kwargs):
        self.retrieve_aufrufe += 1
        return self._inner.retrieve(**kwargs)

    def __getattr__(self, name):
        return getattr(self._inner, name)


@pytest.fixture
def welt(monkeypatch, embedded_qdrant):
    client = embedded_qdrant()
    binding = bind_test_collection(client, COLLECTION, DIM)
    vector_name = _vector_name(DIM)
    return client, binding, vector_name


def _seed(client, vector_name: str, pid: str, **extra) -> dict:
    payload = {
        "id": pid,
        "content": "alte Fassung",
        "text": "alte Fassung",
        "category": "fact",
        "access_level": "private",
        "agent": "fremder-schreiber",
        "source": "alte-quelle",
        "provenance": dict(ALT_PROVENANCE),
        "created_at": ALT_TS,
        "valid_from": ALT_TS,
        "use_count": 7,
        "salience": 0.95,
        "state_value": True,
        "expiry_policy": "volatile",
        "scope": "hermes",
        "lifecycle_status": "canonical",
        "edges": [dict(KANTE)],
        "provenance_trail": ["alt-eintrag"],
        "provenance_trail_count": 1,
    }
    payload.update(extra)
    client.upsert(
        collection_name=COLLECTION,
        points=[models.PointStruct(
            id=pid, vector={vector_name: [0.0] * DIM}, payload=payload)],
    )
    return payload


def _neu(pid: str, **payload) -> models.PointStruct:
    """Die Payload, wie ein ahnungsloser Aufrufer sie baut: nur seine Felder."""
    basis = {
        "id": pid,
        "content": NEUER_TEXT,
        "category": "fact",
        "access_level": "private",
        "agent": "neuer-schreiber",
        "source": "neue-quelle",
        "created_at": "2026-11-24T02:00:00+00:00",
        "provenance": {
            "source_type": "mcp",
            "created_by": "nexus-memory-server",
            "timestamp": "2026-11-24T02:00:00+00:00",
            "confidence": 0.7,
        },
    }
    basis.update(payload)
    return models.PointStruct(id=pid, vector=[0.1] * DIM, payload=basis)


def _lies(client, pid: str) -> dict:
    return client.retrieve(
        collection_name=COLLECTION, ids=[pid],
        with_payload=True, with_vectors=False,
    )[0].payload or {}


# ── 1. Ein Aufrufer, der payload_merge nicht kennt ───────────────────


def test_unwissender_aufrufer_erhaelt_das_zusammenfuehren(welt):
    from nexus_memory.collection_vectors import upsert_named

    client, _binding, vector_name = welt
    pid = str(uuid.uuid4())
    alt = _seed(client, vector_name, pid)

    # Kein payload_merge, kein Alt-Payload: nur der Punkt und der Schreibweg.
    upsert_named(client, COLLECTION, [_neu(pid)], vector_name)

    after = _lies(client, pid)
    # Geschuetzte Felder bleiben der Ursprung.
    for feld in ("created_at", "valid_from", "use_count", "salience",
                 "state_value", "expiry_policy", "scope", "edges",
                 "lifecycle_status"):
        assert after.get(feld) == alt[feld], (
            f"{feld} wurde beim Ueberschreiben zurueckgesetzt")
    # Feld dieses Schreibvorgangs.
    assert after["content"] == NEUER_TEXT
    assert after["agent"] == "neuer-schreiber"
    assert after["updated_at"]
    assert after["provenance"] != ALT_PROVENANCE
    assert after["provenance_origin"] == ALT_PROVENANCE
    trail = after["provenance_trail"]
    assert trail[0] == "alt-eintrag"
    assert trail[-1]["action"] == "overwrite"
    assert trail[-1]["source"] == "neue-quelle"
    assert after["provenance_trail_count"] == 2


def test_unwissender_aufrufer_bei_einem_entity_punkt(welt):
    """Entity-Punkte haben eigene Inhaltsfelder — auch die ziehen mit."""
    from nexus_memory.collection_vectors import upsert_named

    client, _binding, vector_name = welt
    pid = str(uuid.uuid4())
    _seed(client, vector_name, pid, category="entity",
          entity_type="device", entity_name="Verteiler",
          entity_attributes={"ip": "10.0.0.1"},
          search_terms="verteiler stromkasten")

    upsert_named(
        client, COLLECTION,
        [_neu(pid, category="entity", entity_type="device",
              entity_name="Verteiler", entity_attributes={"ip": "10.0.0.9"},
              search_terms="")],
        vector_name,
    )

    after = _lies(client, pid)
    assert after["entity_attributes"] == {"ip": "10.0.0.9"}, (
        "das neue Entity-Feld muss den alten Stand ersetzen")
    assert after["search_terms"] == "verteiler stromkasten", (
        "ein leerer search_terms ist kein Update")
    assert after["edges"] == [KANTE]
    assert after["use_count"] == 7


# ── 2. Neue Kennung: kein Lesezugriff ────────────────────────────────


def test_neue_kennung_kostet_keinen_lesezugriff(welt):
    from nexus_memory.collection_vectors import upsert_named

    client, _binding, vector_name = welt
    zaehler = _ZaehlenderClient(client)
    pid = str(uuid.uuid4())

    upsert_named(zaehler, COLLECTION, [_neu(pid)], vector_name, existing={})

    assert zaehler.retrieve_aufrufe == 0, (
        "eine frische Kennung darf keinen zusaetzlichen Lesezugriff kosten")
    assert _lies(client, pid)["content"] == NEUER_TEXT


def test_merge_false_ist_der_ausdrueckliche_ersatz_fall(welt):
    from nexus_memory.collection_vectors import upsert_named

    client, _binding, vector_name = welt
    zaehler = _ZaehlenderClient(client)
    pid = str(uuid.uuid4())
    _seed(client, vector_name, pid)

    upsert_named(zaehler, COLLECTION, [_neu(pid)], vector_name, merge=False)

    after = _lies(client, pid)
    assert zaehler.retrieve_aufrufe == 0
    assert "use_count" not in after, "merge=False ersetzt bewusst (Sicherung)"


# ── 3. Schon gelesene Payload: kein zweiter Read ─────────────────────


def test_gelesene_payload_spart_den_zweiten_read(welt):
    from nexus_memory.collection_vectors import upsert_named

    client, _binding, vector_name = welt
    pid = str(uuid.uuid4())
    alt = _seed(client, vector_name, pid)
    zaehler = _ZaehlenderClient(client)

    upsert_named(zaehler, COLLECTION, [_neu(pid)], vector_name,
                 existing={pid: dict(alt)})

    assert zaehler.retrieve_aufrufe == 0
    assert _lies(client, pid)["use_count"] == 7


# ── 4. Derselbe Sparfall auf dem echten remember-Weg ─────────────────


@pytest.mark.asyncio
async def test_remember_ohne_memory_id_liest_nicht(welt, monkeypatch):
    """Ein normaler Schreibvorgang (frische Kennung) zahlt keinen Read."""
    import nexus_memory.mcp_server as mcp

    client, _binding, vector_name = welt
    zaehler = _ZaehlenderClient(client)
    monkeypatch.setattr(mcp, "COLLECTION_NAME", COLLECTION)
    monkeypatch.setattr(mcp, "_index_written_point", lambda *a, **k: None)
    monkeypatch.setattr(mcp, "_count_memory_access", lambda *a, **k: None)

    store = object.__new__(mcp.MemoryStore)
    store.client = zaehler
    store.vector_name = vector_name
    store._hybrid_retriever = None
    store._skill_graph = None
    store._scope_centroids = None

    async def _embed(_text, is_query=True):
        return [0.1] * DIM

    store._embed = _embed

    result = await store.remember(
        "Ein frischer Fakt ohne vorhandene Kennung.", category="fact",
        source="probe", actor="test-agent", via="mcp",
    )

    assert result["status"] == "ok"
    assert zaehler.retrieve_aufrufe == 0, (
        "ein neuer Punkt darf keinen Lesezugriff ausloesen")
    payload = _lies(client, result["id"])
    assert payload["provenance_origin"] == payload["provenance"]
