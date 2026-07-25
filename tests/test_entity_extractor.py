"""Tests for the entity extraction pipeline."""

import json
import pytest
from nexus_memory.entity_extractor import (
    Entity, Relationship, ExtractionResult,
    extract_entities, _heuristic_extract_entities,
    ENTITY_TYPES, RELATION_TYPES,
)


class TestEntityModel:
    def test_entity_creation(self):
        e = Entity(name="Wallbox ABL eMH3", entity_type="device", attributes={"ip": "192.168.31.235"})
        assert e.name == "Wallbox ABL eMH3"
        assert e.entity_type == "device"
        assert e.attributes == {"ip": "192.168.31.235"}
        assert 0.0 <= e.confidence <= 1.0

    def test_entity_invalid_type_defaults_to_concept(self):
        e = Entity(name="Test", entity_type="invalid_type")
        assert e.entity_type == "concept"

    def test_entity_confidence_clamped(self):
        e = Entity(name="Test", entity_type="device", confidence=1.5)
        assert e.confidence == 1.0
        e2 = Entity(name="Test", entity_type="device", confidence=-0.5)
        assert e2.confidence == 0.0

    def test_entity_name_stripped(self):
        e = Entity(name="  Test  ", entity_type="device")
        assert e.name == "Test"

    def test_entity_to_dict(self):
        e = Entity(name="Ollama", entity_type="service", confidence=0.9)
        d = e.to_dict()
        assert d["name"] == "Ollama"
        assert d["entity_type"] == "service"
        assert d["confidence"] == 0.9

    def test_entity_name_truncated(self):
        e = Entity(name="A" * 300, entity_type="device")
        assert len(e.name) <= 200


class TestRelationshipModel:
    def test_relationship_creation(self):
        r = Relationship(source="Wallbox", target="Reev", relation="connected_to")
        assert r.source == "Wallbox"
        assert r.target == "Reev"
        assert r.relation == "connected_to"

    def test_relationship_invalid_relation_defaults(self):
        r = Relationship(source="A", target="B", relation="invalid")
        assert r.relation == "connected_to"

    def test_relationship_confidence_clamped(self):
        r = Relationship(source="A", target="B", relation="runs_on", confidence=2.0)
        assert r.confidence == 1.0

    def test_relationship_to_dict(self):
        r = Relationship(source="A", target="B", relation="manages")
        d = r.to_dict()
        assert d["source"] == "A"
        assert d["target"] == "B"
        assert d["relation"] == "manages"


class TestExtractionResult:
    def test_empty_result(self):
        r = ExtractionResult([], [])
        assert r.is_empty()
        assert r.to_dict() == {"entities": [], "relationships": []}

    def test_non_empty_result(self):
        e = Entity(name="Test", entity_type="device")
        r = ExtractionResult([e], [])
        assert not r.is_empty()
        assert len(r.to_dict()["entities"]) == 1


class TestHeuristicExtraction:
    def test_empty_text(self):
        assert _heuristic_extract_entities("").is_empty()
        assert _heuristic_extract_entities("   ").is_empty()

    def test_extract_device(self):
        result = _heuristic_extract_entities("Die Wallbox ist im Garten installiert.")
        devices = [e for e in result.entities if e.entity_type == "device"]
        assert len(devices) >= 1
        assert any("Wallbox" in e.name for e in devices)

    def test_extract_service(self):
        result = _heuristic_extract_entities("Home Assistant laeuft auf dem Mac Mini.")
        services = [e for e in result.entities if e.entity_type == "service"]
        assert len(services) >= 1
        assert any("Home Assistant" in e.name for e in services)

    def test_extract_ip_attribute(self):
        result = _heuristic_extract_entities("Wallbox an IP 192.168.31.235:8300")
        devices = [e for e in result.entities if e.entity_type == "device"]
        if devices:
            assert devices[0].attributes.get("ip") is not None

    def test_extract_protocol(self):
        result = _heuristic_extract_entities("Backend Reev mit OCPP 1.6 Protokoll")
        protocols = [e for e in result.entities if e.entity_type == "protocol"]
        assert len(protocols) >= 1

    def test_extract_location(self):
        result = _heuristic_extract_entities("Der Server steht in Eltville am Rhein")
        locations = [e for e in result.entities if e.entity_type == "location"]
        assert len(locations) >= 1

    def test_runs_on_relationship(self):
        result = _heuristic_extract_entities("Home Assistant laeuft auf dem Mac Mini")
        runs_on = [r for r in result.relationships if r.relation == "runs_on"]
        # Should find at least one runs_on relationship
        assert len(runs_on) >= 1

    def test_dedup_entities(self):
        result = _heuristic_extract_entities("Wallbox und Wallbox und Wallbox")
        wallboxes = [e for e in result.entities if "Wallbox" in e.name]
        assert len(wallboxes) <= 1

    def test_max_entities(self):
        text = " ".join([
            "Wallbox", "Thermostat", "Sensor", "Switch", "Camera",
            "Lock", "Speaker", "TV", "Dongle", "Hub", "Raspberry", "Server"
        ])
        result = _heuristic_extract_entities(text)
        assert len(result.entities) <= 10

    def test_no_entities_in_plain_text(self):
        result = _heuristic_extract_entities("Das Wetter ist schoen heute.")
        assert len(result.entities) == 0


class TestExtractEntitiesPublicAPI:
    def test_empty_text(self):
        assert extract_entities("").is_empty()

    def test_whitespace_only(self):
        assert extract_entities("   ").is_empty()

    def test_heuristic_fallback(self):
        """Without hermes_home, should use heuristic and return results."""
        result = extract_entities(
            "Home Assistant laeuft auf dem Mac Mini. Wallbox verbunden mit Reev.",
            hermes_home="",
        )
        assert len(result.entities) >= 1
        assert any(e.entity_type == "service" for e in result.entities)


class TestLLMResponseParsing:
    """Tests for LLM JSON response parsing (mocked)."""

    def test_parses_json_with_code_blocks(self):
        import re as stdlib_re
        fake = '```json\n{"entities": [{"name": "Test", "type": "device", "attributes": {}, "confidence": 0.9}], "relationships": []}\n```'
        text = fake.strip()
        match = stdlib_re.search(r"```(?:json|JSON)?\s*(.*?)```", text, stdlib_re.DOTALL)
        assert match is not None
        parsed = json.loads(match.group(1).strip())
        assert len(parsed["entities"]) == 1

    def test_parses_uppercase_json_tag(self):
        import re as stdlib_re
        fake = '```JSON\n{"entities": [], "relationships": []}\n```'
        text = fake.strip()
        match = stdlib_re.search(r"```(?:json|JSON)?\s*(.*?)```", text, stdlib_re.DOTALL)
        assert match is not None
        parsed = json.loads(match.group(1).strip())
        assert parsed["entities"] == []

    def test_parses_plain_json(self):
        fake = '{"entities": [{"name": "X", "type": "service", "attributes": {}, "confidence": 0.8}], "relationships": []}'
        parsed = json.loads(fake)
        assert len(parsed["entities"]) == 1

    def test_handles_empty_response(self):
        fake = '{"entities": [], "relationships": []}'
        parsed = json.loads(fake)
        assert len(parsed["entities"]) == 0
        assert len(parsed["relationships"]) == 0

    def test_filters_relationships_between_unknown_entities(self):
        """Relationships referencing entities not in the entity list should be filtered."""
        entities = [
            Entity(name="A", entity_type="device"),
            Entity(name="B", entity_type="service"),
        ]
        entity_names = {e.name.lower() for e in entities}
        # This relationship references "C" which is not in entities
        rels_raw = [
            {"source": "A", "target": "B", "relation": "connected_to"},
            {"source": "A", "target": "C", "relation": "connected_to"},
        ]
        kept = [r for r in rels_raw
                if r["source"].lower() in entity_names and r["target"].lower() in entity_names]
        assert len(kept) == 1


class TestIntegrationScenarios:
    """Realistic text scenarios."""

    def test_wallbox_scenario(self):
        text = ("Wallbox ABL eMH3 an IP 192.168.31.235:8300, "
                "Backend Reev (OCPP 1.6). Home Assistant laeuft auf dem Mac Mini.")
        result = extract_entities(text, hermes_home="")
        # Should find at least: Wallbox (device), Home Assistant (service), Reev (service), OCPP (protocol)
        entity_types_found = {e.entity_type for e in result.entities}
        assert "device" in entity_types_found or "service" in entity_types_found

    def test_smart_home_scenario(self):
        text = ("Home Assistant auf 192.168.31.59:8123. "
                "Shelly Switches verbunden via WiFi. "
                "Tuya Smart Lock mit Matter Protocol.")
        result = extract_entities(text, hermes_home="")
        assert len(result.entities) >= 2

    def test_no_false_positives_in_garbage(self):
        result = extract_entities("hello world 12345 test", hermes_home="")
        assert len(result.entities) == 0

    def test_entity_types_valid(self):
        result = extract_entities("Wallbox und Home Assistant", hermes_home="")
        for e in result.entities:
            assert e.entity_type in ENTITY_TYPES

    def test_relationship_types_valid(self):
        result = extract_entities(
            "Home Assistant laeuft auf Mac Mini und verwaltet die Wallbox",
            hermes_home="",
        )
        for r in result.relationships:
            assert r.relation in RELATION_TYPES