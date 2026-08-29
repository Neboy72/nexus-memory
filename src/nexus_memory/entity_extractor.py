"""Entity Extractor — extract entities and relationships from text.

Two-tier extraction (same pattern as extractor.py):
1. LLM extraction (preferred): uses the configured model to identify entities
2. Heuristic extraction (fallback): pattern-based, always works

Entities are stored as Qdrant points with category="entity" and a new
entity_type field (device, service, person, location, organization, concept).
Relationships between entities use new EdgeRelations (installed_at, connected_to,
manages, runs_on, part_of, owns, located_at).

Example:
    Input: "Wallbox ABL eMH3 an IP 192.168.31.235, Backend Reev (OCPP 1.6)"
    Output: [
        Entity(name="Wallbox ABL eMH3", type="device", attrs={"ip": "192.168.31.235"}),
        Entity(name="Reev", type="service", attrs={"protocol": "OCPP 1.6"}),
        Relationship(source="Wallbox ABL eMH3", target="Reev", relation="connected_to"),
    ]
"""

from __future__ import annotations

import json
import logging
import re
import socket
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# ─── Data Models ──────────────────────────────────────────────────────────────

ENTITY_TYPES = {
    "device", "service", "person", "location",
    "organization", "concept", "software", "protocol",
}

RELATION_TYPES = {
    "installed_at", "connected_to", "manages", "runs_on",
    "part_of", "owns", "located_at", "depends_on_service",
    "uses", "provides", "controls",
}


class Entity:
    """An extracted entity (device, service, person, etc.)."""

    __slots__ = ("name", "entity_type", "attributes", "confidence")

    def __init__(
        self,
        name: str,
        entity_type: str,
        attributes: Optional[Dict[str, Any]] = None,
        confidence: float = 0.8,
    ):
        self.name = name.strip()[:200]
        self.entity_type = entity_type if entity_type in ENTITY_TYPES else "concept"
        self.attributes = attributes or {}
        self.confidence = max(0.0, min(1.0, confidence))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "entity_type": self.entity_type,
            "attributes": self.attributes,
            "confidence": self.confidence,
        }

    def __repr__(self) -> str:
        return f"Entity({self.name!r}, {self.entity_type})"


class Relationship:
    """A typed relationship between two entities."""

    __slots__ = ("source", "target", "relation", "confidence")

    def __init__(
        self,
        source: str,
        target: str,
        relation: str,
        confidence: float = 0.7,
    ):
        self.source = source.strip()
        self.target = target.strip()
        self.relation = relation if relation in RELATION_TYPES else "connected_to"
        self.confidence = max(0.0, min(1.0, confidence))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source": self.source,
            "target": self.target,
            "relation": self.relation,
            "confidence": self.confidence,
        }

    def __repr__(self) -> str:
        return f"Rel({self.source} --[{self.relation}]--> {self.target})"


class ExtractionResult:
    """Result of entity + relationship extraction."""

    __slots__ = ("entities", "relationships")

    def __init__(
        self,
        entities: List[Entity],
        relationships: List[Relationship],
    ):
        self.entities = entities
        self.relationships = relationships

    def to_dict(self) -> Dict[str, Any]:
        return {
            "entities": [e.to_dict() for e in self.entities],
            "relationships": [r.to_dict() for r in self.relationships],
        }

    def is_empty(self) -> bool:
        return not self.entities and not self.relationships


# ─── LLM Extraction ──────────────────────────────────────────────────────────

_ENTITY_EXTRACTION_PROMPT = """Extract entities and their relationships from this text. Return JSON only.

Rules:
- Extract named entities: devices, services, software, protocols, people, organizations, locations
- Entity types: device, service, person, location, organization, concept, software, protocol
- Relationships: installed_at, connected_to, manages, runs_on, part_of, owns, located_at, depends_on_service, uses, provides, controls
- Include key attributes (IP, port, version, URL, etc.) as key-value pairs
- Confidence: 0.9+ for explicit mentions, 0.7-0.8 for inferred
- Language: same as the input text
- Only extract clear, named entities — skip generic nouns
- Max 10 entities, max 8 relationships

Return: {"entities": [{"name": "...", "type": "device", "attributes": {"ip": "..."}, "confidence": 0.9}], "relationships": [{"source": "...", "target": "...", "relation": "connected_to", "confidence": 0.8}]}
If no entities: return {"entities": [], "relationships": []}

Text:
"""

_MAX_TEXT_CHARS = 4000


def _quick_health_check(base_url: str, timeout: float = 1.0) -> bool:
    """TCP probe the LLM endpoint. Returns True if reachable within timeout."""
    try:
        parsed = urlparse(base_url)
        host = parsed.hostname or "localhost"
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        sock = socket.create_connection((host, port), timeout=timeout)
        sock.close()
        return True
    except Exception:
        return False


def _load_llm_config(hermes_home: str) -> Dict[str, str]:
    """Read model config from Hermes config.yaml and .env."""
    config: Dict[str, str] = {"model": "", "base_url": "", "api_key": ""}

    try:
        import yaml
        config_path = f"{hermes_home}/config.yaml"
        with open(config_path) as f:
            cfg = yaml.safe_load(f) or {}

        model = cfg.get("model", {})
        config["model"] = model.get("default", "")
        config["base_url"] = model.get("base_url", "")
        config["api_key"] = model.get("api_key", "")

        provider = model.get("provider", "")
        if provider and provider.startswith("custom:"):
            provider_name = provider[7:]
            providers = cfg.get("providers", {})
            if provider_name in providers:
                p = providers[provider_name]
                if not config["base_url"]:
                    config["base_url"] = p.get("base_url", "")
                if not config["api_key"]:
                    config["api_key"] = p.get("api_key", "")
    except Exception:
        pass

    env_path = f"{hermes_home}/.env"
    import os
    if os.path.exists(env_path) and not config["api_key"]:
        try:
            with open(env_path) as f:
                for line in f:
                    line = line.strip()
                    if "=" in line and not line.startswith("#"):
                        key, val = line.split("=", 1)
                        key = key.strip()
                        val = val.strip().strip('"').strip("'")
                        if key == "OLLAMA_API_KEY" and not config["api_key"]:
                            config["api_key"] = val
                        elif key == "OPENAI_API_KEY" and not config["api_key"]:
                            config["api_key"] = val
        except Exception:
            pass

    if not config["base_url"]:
        config["base_url"] = "http://localhost:11434/v1"
    if not config["api_key"]:
        config["api_key"] = "ollama"
    if not config["model"]:
        config["model"] = "gemma3:4b"

    return config


def _llm_extract_entities(
    text: str,
    hermes_home: str,
) -> ExtractionResult:
    """Use LLM to extract entities. Returns empty result on failure."""
    config = _load_llm_config(hermes_home)
    if not config["model"]:
        return ExtractionResult([], [])

    if not _quick_health_check(config["base_url"]):
        logger.debug("EntityExtractor: LLM endpoint unreachable, using heuristic")
        return ExtractionResult([], [])

    prompt = _ENTITY_EXTRACTION_PROMPT + text[:_MAX_TEXT_CHARS]

    try:
        from openai import OpenAI
        client = OpenAI(
            base_url=config["base_url"],
            api_key=config["api_key"],
            timeout=10,
        )
        response = client.chat.completions.create(
            model=config["model"],
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            # max_tokens includes the hidden reasoning field: glm-5.3-flash "thinks"
            # even with think:false (reasoning moved to separate field, but tokens
            # still count). 800 was exhausted by reasoning alone on long texts ->
            # empty content -> JSON parse fail. 4000 verified 29.08 (worst-case
            # reasoning beobachtet). Unused budget costs nothing.
            max_tokens=4000,
            extra_body={"think": False},
            timeout=30,
        )
        if response.choices:
            raw = response.choices[0].message.content or ""
        else:
            raw = ""

        raw = raw.strip()
        if "```" in raw:
            match = re.search(r"```(?:json|JSON)?\s*(.*?)```", raw, re.DOTALL)
            if match:
                raw = match.group(1).strip()

        data = json.loads(raw)

        entities = []
        for e in data.get("entities", []):
            if not isinstance(e, dict):
                continue
            name = (e.get("name") or "").strip()
            if not name:
                continue
            entities.append(Entity(
                name=name[:200],
                entity_type=e.get("type", "concept"),
                attributes=e.get("attributes", {}),
                confidence=e.get("confidence", 0.8),
            ))

        relationships = []
        entity_names = {e.name.lower() for e in entities}
        for r in data.get("relationships", []):
            if not isinstance(r, dict):
                continue
            source = (r.get("source") or "").strip()
            target = (r.get("target") or "").strip()
            if not source or not target:
                continue
            # Only keep relationships between extracted entities
            if source.lower() not in entity_names or target.lower() not in entity_names:
                continue
            relationships.append(Relationship(
                source=source[:200],
                target=target[:200],
                relation=r.get("relation", "connected_to"),
                confidence=r.get("confidence", 0.7),
            ))

        logger.info(
            "EntityExtractor: LLM extracted %d entities, %d relationships",
            len(entities), len(relationships),
        )
        return ExtractionResult(entities, relationships)

    except Exception as exc:
        logger.warning("EntityExtractor: LLM extraction failed: %s", exc)
        return ExtractionResult([], [])


# ─── Heuristic Extraction (fallback, always works) ──────────────────────────

# Pattern-based entity extraction
_IP_PATTERN = re.compile(
    r"\b(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}(?::\d+)?)\b"
)
_URL_PATTERN = re.compile(
    r"\b(https?://[^\s<>\"]+\S)"
)
_VERSION_PATTERN = re.compile(
    r"\b(v?\d+\.\d+(?:\.\d+)?)\b"
)
# Device patterns: known device keywords
_DEVICE_KEYWORDS = re.compile(
    r"\b(?:Wallbox|Thermostat|Sensor|Switch|Camera|Lock|Speaker|TV|Dongle|Hub|"
    r"Raspberry|Mac Mini|Server|NAS|Router|Modem|Printer|Dock|Display)\b",
    re.IGNORECASE,
)
# Service patterns: known service keywords
_SERVICE_KEYWORDS = re.compile(
    r"\b(?:Home Assistant|Qdrant|Ollama|OpenClaw|Hermes|Nexus|Telegram|Discord|"
    r"Spotify|Docker|PostgreSQL|Redis|Nginx|Caddy|Vault|Paperless|ZimaOS|"
    r"Reev|Tuya|Shelly|Tado|Blink|Matter)\b",
    re.IGNORECASE,
)
# Protocol patterns
_PROTOCOL_KEYWORDS = re.compile(
    r"\b(?:OCPP\s*\d\.\d|Matter|Zigbee|Z-Wave|WiFi|Bluetooth|Thread|"
    r"MQTT|HTTP|HTTPS|WebSocket|gRPC|REST|SOAP)\b",
    re.IGNORECASE,
)
# Location patterns
_LOCATION_KEYWORDS = re.compile(
    r"\b(?:Eltville|Wohnung|Haus|Keller|Garage|Garten|Schlafzimmer|"
    r"Wohnzimmer|Kueche|Buero|Badezimmer|Dachboden)\b",
    re.IGNORECASE,
)

# Relationship indicators
_CONNECTS_PATTERN = re.compile(
    r"(?:connect(?:ed)?(?:_to|s_to|s)?|verbunden|verbindet|an\b|auf)\s+",
    re.IGNORECASE,
)
_RUNS_ON_PATTERN = re.compile(
    r"(?:lauft|laeuft|runs?|hosted|deployed|installed?)\s+(?:auf|on|im|in|bei)\s+",
    re.IGNORECASE,
)
_MANAGES_PATTERN = re.compile(
    r"(?:managed?|verwaltet|controls?|steuert)\s+",
    re.IGNORECASE,
)


def _heuristic_extract_entities(text: str) -> ExtractionResult:
    """Pattern-based entity extraction. Always works, no external deps."""
    entities: List[Entity] = []
    relationships: List[Relationship] = []
    seen_names: set = set()

    # Extract IPs (as attributes, not standalone entities)
    ips = _IP_PATTERN.findall(text)

    # Extract devices
    for m in _DEVICE_KEYWORDS.finditer(text):
        name = m.group()
        key = name.lower()
        if key not in seen_names:
            # Try to get context around the match for a fuller name
            start = max(0, m.start() - 20)
            end = min(len(text), m.end() + 30)
            context = text[start:end]
            # Extract IP if nearby
            attrs: Dict[str, Any] = {}
            nearby_ips = _IP_PATTERN.findall(context)
            if nearby_ips:
                attrs["ip"] = nearby_ips[0]
            entities.append(Entity(name=name, entity_type="device", attributes=attrs))
            seen_names.add(key)

    # Extract services
    for m in _SERVICE_KEYWORDS.finditer(text):
        name = m.group()
        key = name.lower()
        if key not in seen_names:
            entities.append(Entity(name=name, entity_type="service"))
            seen_names.add(key)

    # Extract protocols
    for m in _PROTOCOL_KEYWORDS.finditer(text):
        name = m.group()
        key = name.lower()
        if key not in seen_names:
            entities.append(Entity(name=name, entity_type="protocol"))
            seen_names.add(key)

    # Extract locations
    for m in _LOCATION_KEYWORDS.finditer(text):
        name = m.group()
        key = name.lower()
        if key not in seen_names:
            entities.append(Entity(name=name, entity_type="location"))
            seen_names.add(key)

    # Infer relationships from patterns
    entity_names = {e.name.lower(): e for e in entities}

    # "X runs on Y" / "X laeuft auf Y"
    for m in _RUNS_ON_PATTERN.finditer(text):
        # Find entity before and after the pattern
        before = text[max(0, m.start() - 50):m.start()].strip()
        after = text[m.end():m.end() + 50].strip()
        before_entity = _find_nearest_entity(before, entity_names, reverse=True)
        after_entity = _find_nearest_entity(after, entity_names)
        if before_entity and after_entity:
            relationships.append(Relationship(
                source=before_entity, target=after_entity,
                relation="runs_on", confidence=0.6,
            ))

    # "X manages Y" / "X verwaltet Y"
    for m in _MANAGES_PATTERN.finditer(text):
        before = text[max(0, m.start() - 50):m.start()].strip()
        after = text[m.end():m.end() + 50].strip()
        before_entity = _find_nearest_entity(before, entity_names, reverse=True)
        after_entity = _find_nearest_entity(after, entity_names)
        if before_entity and after_entity:
            relationships.append(Relationship(
                source=before_entity, target=after_entity,
                relation="manages", confidence=0.6,
            ))

    # "X connected to Y" / "X verbunden mit Y"
    for m in _CONNECTS_PATTERN.finditer(text):
        before = text[max(0, m.start() - 50):m.start()].strip()
        after = text[m.end():m.end() + 50].strip()
        before_entity = _find_nearest_entity(before, entity_names, reverse=True)
        after_entity = _find_nearest_entity(after, entity_names)
        if before_entity and after_entity and before_entity != after_entity:
            relationships.append(Relationship(
                source=before_entity, target=after_entity,
                relation="connected_to", confidence=0.5,
            ))

    # Cap results
    entities = entities[:10]
    relationships = relationships[:8]

    if entities:
        logger.info(
            "EntityExtractor: heuristic extracted %d entities, %d relationships",
            len(entities), len(relationships),
        )

    return ExtractionResult(entities, relationships)


def _find_nearest_entity(
    text: str,
    entity_names: Dict[str, Entity],
    reverse: bool = False,
) -> Optional[str]:
    """Find the nearest entity name in the given text."""
    words = text.split()
    if reverse:
        words = list(reversed(words))
    for word in words:
        clean = word.strip(".,;:!?()[]{}\"'").lower()
        for name, entity in entity_names.items():
            if clean and (clean == name or name in clean or clean in name):
                return entity.name
    return None


# ─── Public API ──────────────────────────────────────────────────────────────

def extract_entities(
    text: str,
    hermes_home: str = "",
) -> ExtractionResult:
    """Extract entities and relationships from text.

    Tries LLM extraction first, falls back to heuristic.
    Returns an ExtractionResult with entities and relationships.
    """
    if not text or not text.strip():
        return ExtractionResult([], [])

    # Try LLM first
    if hermes_home:
        try:
            result = _llm_extract_entities(text, hermes_home)
            if not result.is_empty():
                return result
        except Exception as exc:
            logger.warning("EntityExtractor: LLM path failed: %s", exc)

    # Fallback to heuristic
    return _heuristic_extract_entities(text)