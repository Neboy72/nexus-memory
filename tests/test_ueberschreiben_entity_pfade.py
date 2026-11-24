"""Ueberschreiben an den beiden ENTITAETS-Pfaden: Felder bleiben, Kante bleibt.

Auftrag 24.11.2026: dieselbe Opt-in-Regel wie in ``remember(memory_id=...)``
gilt auch fuer ``_upsert_entity`` — in ``plugins/memory/nexus`` (die Kopie, die
Hermes laedt) und in ``integrations/hermes-plugin``.

Der Punkt traegt vorher in jedem Feld einen Nicht-Vorgabewert; nach EINEM
Ueberschreiben wird jedes Feld einzeln geprueft. Dazu: der Kanten-Fall, ein
neuer Punkt, die Zugriffsstufe (schwaecher und strenger) und der Trail-Deckel.

Eingebetteter Qdrant im Temp-Ordner, nie ``nexus_hf``.
"""

from __future__ import annotations

import importlib
import importlib.util
import threading
import uuid
from pathlib import Path

import pytest
from qdrant_client import models

from conftest import (
    bind_test_collection,
    embedded_qdrant,
    test_vector_name as _vector_name,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
DIM = 2
COLLECTION = "zz_test_entity_ueberschreiben"

ENTITY_NAME = "ZZ-Entitaet"
ENTITY_TYPE = "device"
POINT_ID = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{ENTITY_TYPE}:{ENTITY_NAME}"))
NEW_POINT_ID = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{ENTITY_TYPE}:ZZ-Entitaet-Neu"))

OLD_TS = "2026-06-01T10:00:00+00:00"
OLD_UPDATED = "2026-07-01T10:00:00+00:00"
OLD_SOURCE = "hermes-plugin-session-end"
OLD_PROVENANCE = {
    "source_type": "hermes-plugin",
    "created_by": "frueherer-schreiber",
    "timestamp": OLD_TS,
    "confidence": 0.9,
}
EDGE = {
    "edge_id": "edge-1",
    "target_fact_id": "ziel-punkt",
    "relation": "part_of",
    "status": "active",
    "reason": "nexus_remember",
}
REPORT = {"by": "agent-b", "at": "2026-11-01T08:00:00+00:00",
          "reason": "Widerspruch zum Handbuch", "status": "open"}
EVIDENCE = {"source_id": "src-handbuch", "source_type": "doc",
            "captured_at": "2026-05-01T08:00:00+00:00", "trust_contribution": 0.8}

# Alle Felder, die ein Ueberschreiben NICHT anfassen darf — je mit
# Nicht-Vorgabewert, damit ein Rueckfall auf den Vorgabewert auffaellt.
PRESERVED = {
    "created_at": OLD_TS,
    "valid_from": OLD_TS,
    "use_count": 7,
    "access_count": 42,
    "last_accessed": "2026-09-01T09:00:00+00:00",
    "salience": 0.93,
    "confidence": 0.91,
    "trust": 0.77,
    "evidences": [EVIDENCE],
    "reports": [dict(REPORT)],
    "supersedes": ["alt-1", "alt-2"],
    "source_url": "https://example.org/handbuch",
    "lifecycle_status": "deprecated",
    "valid_to": "2026-06-02T00:00:00+00:00",
    "expiry_policy": "volatile",
    "state_value": True,
    "edges": [EDGE],
    "source": OLD_SOURCE,
    "type": "memory",
    "scope": "hermes",
}


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(f"nexus_entity_over_{name}", str(path))
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _provider_for(module, client, vector_name: str):
    prov = module.NexusMemoryProvider.__new__(module.NexusMemoryProvider)
    prov._qdrant = client
    prov._collection = COLLECTION
    prov._embedder = object()
    prov._hermes_home = ""
    prov._write_stop = threading.Event()
    prov.vector_name = vector_name
    prov._embed_cached = lambda text, is_query=False: [float(len(text) % 97), 0.5]
    return prov


@pytest.fixture(params=["plugin", "integrations"])
def site(request, embedded_qdrant, monkeypatch, tmp_path):
    monkeypatch.setenv("NEXUS_AGENT_ID", "test-agent")
    monkeypatch.setenv("NEXUS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("NEXUS_LOG_DIR", str(tmp_path / "logs"))

    path = (REPO_ROOT / "plugins" / "memory" / "nexus" / "__init__.py"
            if request.param == "plugin"
            else REPO_ROOT / "integrations" / "hermes-plugin" / "__init__.py")
    source = path.read_text(encoding="utf-8")
    # Die integrations-Kopie ist die aeltere Fassung: sie schreibt weder
    # ``entity_evidence`` noch ``search_terms``. Der Test prueft sie deshalb
    # nur dort, wo sie diese Felder wirklich bestimmt.
    caps = {
        "entity_evidence": "entity_evidence" in source,
        "search_terms": "search_terms" in source,
    }
    module = _load(request.param, path)
    monkeypatch.setattr(module, "_log_movement", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(module, "_index_written_point", lambda *a, **k: None, raising=False)
    # Suchvarianten sind eine Zugabe; hier fest, damit die Pruefung eindeutig ist.
    monkeypatch.setattr("nexus.variants.search_variants", lambda text: "neu varianten")

    client = embedded_qdrant()
    bind_test_collection(client, COLLECTION, DIM)
    vector_name = _vector_name(DIM)
    provider = _provider_for(module, client, vector_name)
    Entity = importlib.import_module("nexus_memory.entity_extractor").Entity

    def seed(access_level: str = "private", with_edges: bool = True,
             with_trail: bool = False) -> None:
        payload = {
            "id": POINT_ID,
            "content": "alte Fassung",
            "category": "entity",
            "agent": "hermes",
            "instance": "hermes-instanz-1",
            "updated_at": OLD_UPDATED,
            "provenance": dict(OLD_PROVENANCE),
            "entity_type": ENTITY_TYPE,
            "entity_name": ENTITY_NAME,
            "entity_attributes": {"ip": "10.0.0.1"},
            "entity_evidence": "alter Belegsatz",
            "search_terms": "alt varianten",
        }
        payload.update(PRESERVED)
        if not with_edges:
            payload.pop("edges")
        if with_trail:
            payload["provenance_trail"] = ["alt-eintrag"]
            payload["provenance_trail_count"] = 1
        payload["access_level"] = access_level
        client.upsert(
            collection_name=COLLECTION,
            points=[models.PointStruct(
                id=POINT_ID, vector={vector_name: [0.0] * DIM}, payload=payload,
            )],
        )

    def overwrite(access_level: str = "public", attributes: dict | None = None) -> None:
        entity = Entity(
            ENTITY_NAME, ENTITY_TYPE,
            attributes if attributes is not None else {"ip": "10.0.0.2"},
            confidence=0.9,
        )
        entity.evidence = "neuer Belegsatz"
        provider._upsert_entity(entity, access_level=access_level, source="probe")

    def write_new(access_level: str = "public") -> None:
        entity = Entity("ZZ-Entitaet-Neu", ENTITY_TYPE, {"ip": "10.0.0.9"}, confidence=0.9)
        provider._upsert_entity(entity, access_level=access_level, source="probe")

    def read(pid: str = POINT_ID) -> dict:
        return client.retrieve(
            collection_name=COLLECTION, ids=[pid],
            with_payload=True, with_vectors=False,
        )[0].payload or {}

    return {
        "name": request.param, "client": client, "caps": caps, "seed": seed,
        "overwrite": overwrite, "write_new": write_new, "read": read,
        "new_id": NEW_POINT_ID,
    }


def _edge_ids(payload: dict) -> list[str]:
    return [e["edge_id"] for e in payload.get("edges") or []]


def test_ueberschreiben_erhaelt_jedes_feld(site):
    site["seed"](access_level="private")
    site["overwrite"](access_level="public", attributes={"ip": "10.0.0.2"})
    after = site["read"]()

    fehler = []
    for key, expected in PRESERVED.items():
        if after.get(key) != expected:
            fehler.append(f"{key}: {after.get(key)!r} statt {expected!r}")
    assert not fehler, "Felder sind beim Ueberschreiben verloren gegangen: " + "; ".join(fehler)

    # Was der Schreibvorgang bestimmt, ist neu.
    assert after["content"] != "alte Fassung"
    assert "10.0.0.2" in after["content"]
    assert after["entity_attributes"] == {"ip": "10.0.0.2"}
    if site["caps"]["entity_evidence"]:
        assert after["entity_evidence"] == "neuer Belegsatz"
    else:
        # Diese Kopie schreibt das Feld nicht — dann bleibt der alte Wert.
        assert after["entity_evidence"] == "alter Belegsatz"
    if site["caps"]["search_terms"]:
        assert after["search_terms"] == "neu varianten"
    else:
        assert after["search_terms"] == "alt varianten"
    assert after["updated_at"] and after["updated_at"] != OLD_UPDATED
    # Der Ursprung bleibt sichtbar: ``source`` bleibt, ``provenance`` folgt dem
    # letzten Schreiber, der erste steht in ``provenance_origin``.
    assert after["source"] == OLD_SOURCE
    assert after["provenance"] != OLD_PROVENANCE, (
        "provenance muss die Aussage des letzten Schreibers sein")
    assert after["provenance_origin"] == OLD_PROVENANCE
    # Der neue Vorgang steht im Trail.
    trail = after["provenance_trail"] or []
    assert trail[-1]["action"] == "overwrite"
    assert trail[-1]["source"] == "probe"
    assert after["provenance_trail_count"] == 1


def test_kante_bleibt_mit_derselben_kennung(site):
    site["seed"]()
    assert _edge_ids(site["read"]()) == ["edge-1"]
    site["overwrite"]()
    after = site["read"]()
    assert _edge_ids(after) == ["edge-1"], "die Kante ist beim Ueberschreiben verloren gegangen"
    assert len(after["edges"]) == 1, "die Kante darf nicht doppelt angelegt werden"


def test_neuer_punkt_ohne_ueberschreiben(site):
    site["write_new"](access_level="public")
    payload = site["read"](site["new_id"])
    assert "provenance_trail" not in payload
    assert payload["provenance_origin"] == payload["provenance"], (
        "ein neuer Punkt setzt seinen Ursprung beim ersten Schreiben")
    assert "updated_at" not in payload
    assert "edges" not in payload
    assert payload["entity_attributes"] == {"ip": "10.0.0.9"}
    assert payload["access_level"] == "public"


def test_schwache_zugriffsstufe_oeffnet_nicht(site):
    site["seed"](access_level="private")
    site["overwrite"](access_level="public")
    assert site["read"]()["access_level"] == "private"


def test_strenge_zugriffsstufe_gewinnt(site):
    site["seed"](access_level="public")
    site["overwrite"](access_level="trusted")
    assert site["read"]()["access_level"] == "trusted"


def test_trail_bleibt_begrenzt(site):
    site["seed"]()
    for i in range(25):
        site["overwrite"](attributes={"ip": f"10.0.0.{i}"})
    after = site["read"]()
    assert len(after["provenance_trail"]) == 20, "der Trail muss bei 20 gedeckelt sein"
    assert after["provenance_trail_count"] == 25, "die Gesamtzahl zaehlt weiter"
    assert after["provenance_origin"] == OLD_PROVENANCE, (
        "der Ursprung darf auch nach 25 Ueberschreibungen nicht aus dem Punkt fallen")
    assert _edge_ids(after) == ["edge-1"]
    assert after["created_at"] == OLD_TS
