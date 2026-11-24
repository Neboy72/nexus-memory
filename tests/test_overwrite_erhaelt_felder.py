"""Ueberschreiben erhaelt die Felder des vorhandenen Punktes.

Auftrag 24.11.2026, nach der Messung: ein Ueberschreiben setzte ``use_count``
7→0, ``salience`` 0,95→0,5, ``created_at``/``valid_from`` neu, ``scope``
``hermes``→``default``, ``state_value`` weg, ``expiry_policy`` leer→``static``.
Erlaubt ist nur, was der Auftrag nennt: ``content``/``text``, ``search_terms``,
der Vektor und ``updated_at`` werden neu; alles andere bleibt, wie der Punkt
entstanden ist.

Geprueft werden alle drei Schreibstellen — die beiden Plugin-Kopien und der
MCP-Zweig ``remember(memory_id=...)`` — gegen einen eingebetteten Qdrant im
Temp-Ordner (nie ``nexus_hf``). Der Punkt traegt vorher in JEDEM der neun
Felder einen Nicht-Vorgabewert; nach dem Ueberschreiben wird jedes einzeln
geprueft.

Ohne den Fix: rot (jedes Feld faellt auf seinen Vorgabewert zurueck).
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
COLLECTION = "zz_test_overwrite_felder"

OLD_TS = "2026-06-01T10:00:00+00:00"
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
OLD_TRAIL = ["alt-eintrag"]

# Die neun Felder, die ein Ueberschreiben NICHT anfassen darf.
GESCHUETZT = {
    "created_at": OLD_TS,
    "valid_from": OLD_TS,
    "use_count": 7,
    "salience": 0.95,
    "state_value": True,
    "expiry_policy": "volatile",
    "edges": [EDGE],
    "scope": "hermes",
    # Nachtrag aus demselben Grundsatz (kein stiller Rueckfall):
    "lifecycle_status": "deprecated",
    "valid_to": "2026-06-02T00:00:00+00:00",
}


class _StubEmbedder:
    """Textabhaengiger Vektor — so laesst sich pruefen, dass er erneuert wird."""

    def embed(self, text: str, is_query: bool = True) -> list[float]:
        return [float(sum(map(ord, text)) % 97), 0.5]


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(f"nexus_overwrite_{name}", str(path))
    assert spec is not None, f"could not load {path}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _provider_for(module, client, collection: str, vector_name: str):
    prov = module.NexusMemoryProvider.__new__(module.NexusMemoryProvider)
    prov._qdrant = client
    prov._collection = collection
    prov._embedder = _StubEmbedder()
    prov._hermes_home = ""
    prov._write_stop = threading.Event()
    prov.vector_name = vector_name
    prov._embed_cached = lambda text, is_query=False: _StubEmbedder().embed(text)
    return prov


class _Site:
    """Eine Schreibstelle: Punkt anlegen, ueberschreiben, lesen."""

    def __init__(self, name: str, client, collection: str, vector_name: str,
                 point_id: str, seed_fields: dict, overwrite):
        self.name = name
        self.client = client
        self.collection = collection
        self.vector_name = vector_name
        self.point_id = point_id
        self._seed_fields = seed_fields
        self._overwrite = overwrite

    def seed(self, access_level: str = "private") -> None:
        self.client.upsert(
            collection_name=self.collection,
            points=[models.PointStruct(
                id=self.point_id,
                vector={self.vector_name: [0.0] * DIM},
                payload={
                    "id": self.point_id,
                    "content": "alte Fassung",
                    "access_level": access_level,
                    # Verfasser: ohne ihn gilt der Punkt fuer ``know`` als
                    # herrenlos und darf nicht ueberschrieben werden
                    # (Eigentuemer-Regel 24.11.2026). Der MCP-Zweig schreibt
                    # hier als ``hermes`` (actor unten).
                    "agent": "hermes",
                    "source": OLD_SOURCE,
                    "provenance": dict(OLD_PROVENANCE),
                    "provenance_trail": list(OLD_TRAIL),
                    **self._seed_fields,
                    **GESCHUETZT,
                },
            )],
        )

    async def overwrite(self, access_level: str) -> None:
        await self._overwrite(access_level)

    def read(self, with_vectors: bool = False) -> dict:
        record = self.client.retrieve(
            collection_name=self.collection, ids=[self.point_id],
            with_payload=True, with_vectors=with_vectors,
        )[0]
        return record.payload or {}

    def vector(self) -> list[float]:
        record = self.client.retrieve(
            collection_name=self.collection, ids=[self.point_id],
            with_payload=False, with_vectors=True,
        )[0]
        vec = record.vector
        return vec[self.vector_name] if isinstance(vec, dict) else vec


@pytest.fixture(params=["plugin", "integrations", "mcp"])
def site(request, embedded_qdrant, monkeypatch, tmp_path):
    import os

    monkeypatch.setenv("NEXUS_AGENT_ID", "test-agent")
    monkeypatch.setenv("NEXUS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("NEXUS_LOG_DIR", str(tmp_path / "logs"))
    client = embedded_qdrant()
    bind_test_collection(client, COLLECTION, DIM)
    vector_name = _vector_name(DIM)

    if request.param in ("plugin", "integrations"):
        path = (REPO_ROOT / "plugins" / "memory" / "nexus" / "__init__.py"
                if request.param == "plugin"
                else REPO_ROOT / "integrations" / "hermes-plugin" / "__init__.py")
        module = _load(request.param, path)
        monkeypatch.setattr(module, "_log_movement", lambda *a, **k: None, raising=False)
        monkeypatch.setattr(module, "_index_written_point", lambda *a, **k: None, raising=False)
        provider = _provider_for(module, client, COLLECTION, vector_name)
        Entity = importlib.import_module("nexus_memory.entity_extractor").Entity

        entity_before = Entity("ZZ-Feld-Knoten", "device", {"probe": "1"}, confidence=0.9)
        point_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, "device:ZZ-Feld-Knoten"))

        async def _overwrite(level: str) -> None:
            entity_after = Entity("ZZ-Feld-Knoten", "device", {"probe": "2"}, confidence=0.9)
            provider._upsert_entity(entity_after, access_level=level, source="probe")

        seed_fields = {
            "category": "entity",
            "entity_type": entity_before.entity_type,
            "entity_name": entity_before.name,
            "entity_attributes": entity_before.attributes,
            "search_terms": "alt varianten",
            "entity_evidence": "alter Belegsatz",
        }
        site = _Site(request.param, client, COLLECTION, vector_name, point_id,
                     seed_fields, _overwrite)

        async def _write_new(level: str) -> None:
            provider._upsert_entity(
                Entity("ZZ-Feld-Neu", "device", {"probe": "neu"}, confidence=0.9),
                access_level=level, source="probe",
            )

        site.write_new = _write_new
        site.new_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, "device:ZZ-Feld-Neu"))
        return site

    # ── MCP: remember(memory_id=...) ──────────────────────────────────────
    import nexus_memory.mcp_server as mcp

    monkeypatch.setattr(mcp, "COLLECTION_NAME", COLLECTION)
    monkeypatch.setattr(mcp, "_index_written_point", lambda *a, **k: None)
    monkeypatch.setattr(mcp, "_count_memory_access", lambda *a, **k: None)

    store = object.__new__(mcp.MemoryStore)
    store.client = client
    store.vector_name = vector_name
    store._hybrid_retriever = None
    store._skill_graph = None
    store._scope_centroids = None
    store._embedder = _StubEmbedder()

    async def _embed(text, is_query=True):
        return _StubEmbedder().embed(text)

    store._embed = _embed

    point_id = "33333333-3333-4333-8333-333333333333"

    async def _overwrite(level: str) -> None:
        await store.remember(
            "Die neue Fassung nennt einen anderen Inhalt als die alte Fassung.",
            category="fact", source="probe", access_level=level,
            memory_id=point_id, actor="hermes", via="mcp", source_type="chat",
        )

    seed_fields = {"category": "fact", "text": "alte Fassung"}
    site = _Site("mcp", client, COLLECTION, vector_name, point_id, seed_fields, _overwrite)

    async def _write_new(level: str) -> None:
        result = await store.remember(
            "Ein anderer Fakt fuer den neuen Punkt, ohne vorhandene Kennung.",
            category="fact", source="probe", access_level=level,
            actor="hermes", via="mcp", source_type="chat",
        )
        site.new_id = result["id"]

    site.write_new = _write_new
    site.new_id = None
    return site


def _assert_geschuetzt(payload: dict, site) -> None:
    for feld, erwartet in GESCHUETZT.items():
        assert payload.get(feld) == erwartet, (
            f"[{site.name}] {feld} wurde beim Ueberschreiben zurueckgesetzt: "
            f"{payload.get(feld)!r} statt {erwartet!r}"
        )


@pytest.mark.asyncio
async def test_ueberschreiben_erhaelt_alle_felder(site):
    site.seed(access_level="private")
    vector_before = site.vector()

    await site.overwrite(access_level="public")

    payload = site.read()
    _assert_geschuetzt(payload, site)

    # Immer aktualisieren: Inhalt, Vektor und updated_at.
    assert payload["content"] != "alte Fassung", "content muss der neue Text sein"
    assert site.vector() != vector_before, "der Vektor muss der neue sein"
    assert payload.get("updated_at"), "updated_at muss beim Ueberschreiben gesetzt sein"

    # Zugriffsstufe: die strengere gewinnt (alt private, neu public → private).
    assert payload["access_level"] == "private"

    # ``source`` bleibt der Ursprung; ``provenance`` ist die Aussage des
    # letzten Schreibers, der erste steht in ``provenance_origin``.
    assert payload["source"] == OLD_SOURCE, "source ist der Ursprung, nicht der neue Aufruf"
    assert payload["provenance"] != OLD_PROVENANCE, (
        "provenance muss dem letzten Schreiber folgen")
    assert payload["provenance_origin"] == OLD_PROVENANCE, (
        "provenance_origin muss der erste Schreiber bleiben")
    trail = payload.get("provenance_trail") or []
    assert trail[: len(OLD_TRAIL)] == OLD_TRAIL, "der alte Trail bleibt erhalten"
    assert trail[-1]["action"] == "overwrite"
    assert trail[-1]["source"] == "probe"
    assert trail[-1]["timestamp"]


@pytest.mark.asyncio
async def test_zugriffsstufe_wird_nie_geoeffnet_schwaecher(site):
    """Alt private, neu trusted → private bleibt."""
    site.seed(access_level="private")
    await site.overwrite(access_level="trusted")
    assert site.read()["access_level"] == "private"


@pytest.mark.asyncio
async def test_zugriffsstufe_uebernimmt_die_strengere(site):
    """Alt public, neu trusted → trusted (der neue, strengere Wert gilt)."""
    site.seed(access_level="public")
    await site.overwrite(access_level="trusted")
    assert site.read()["access_level"] == "trusted"


@pytest.mark.asyncio
async def test_neuer_punkt_verhaelt_sich_wie_bisher(site):
    """Kein Ueberschreiben: kein updated_at, kein edges-Feld, Werte wie berechnet."""
    await site.write_new("public")

    payload = site.client.retrieve(
        collection_name=COLLECTION, ids=[getattr(site, "new_id")],
        with_payload=True, with_vectors=False,
    )[0].payload or {}

    assert "updated_at" not in payload, "nur ein Ueberschreiben setzt updated_at"
    assert "edges" not in payload, "ein neuer Punkt traegt kein leeres edges-Feld"
    assert "provenance_trail" not in payload
    assert payload["provenance_origin"] == payload["provenance"], (
        "ein neuer Punkt setzt seinen Ursprung beim ersten Schreiben")
    assert payload["access_level"] == "public", "ohne Ueberschreiben gilt die Angabe"
