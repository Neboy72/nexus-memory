"""Tests for OCR review Wave 14 — manifest/meta findings (H154-H162, H403-H409).

The dashboard/TypeScript halves of this wave (H145-H153, H403-H406) live in
``dashboard/static/webui-js/test-wave14-contracts.mjs`` and
``plugins/openclaw/test-*.mjs`` and are run with ``node``. Here we pin the
JSON manifests and the claude-code plugin attribution contract.
"""

from __future__ import annotations

import json
import re

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ENVIRONMENT = REPO_ROOT / ".hermes" / "environment.json"
PYPROJECT = REPO_ROOT / "pyproject.toml"
LICENSE = REPO_ROOT / "LICENSE"
PKG = REPO_ROOT / "plugins" / "openclaw" / "package.json"
PLUGIN_MANIFEST = REPO_ROOT / "plugins" / "openclaw" / "openclaw.plugin.json"
CONFIG_TS = REPO_ROOT / "plugins" / "openclaw" / "lib" / "config.ts"
MARKETPLACE = REPO_ROOT / ".claude-plugin" / "marketplace.json"
CC_PLUGIN = REPO_ROOT / "plugins" / "claude-code" / ".claude-plugin" / "plugin.json"
CC_SCRIPTS = REPO_ROOT / "plugins" / "claude-code" / "scripts"


def _json(path: Path) -> dict:
    return json.loads(path.read_text())


def _allowed_keys() -> list[str]:
    text = CONFIG_TS.read_text()
    m = re.search(r"const ALLOWED_KEYS = \[(.*?)\]", text, re.S)
    assert m, "ALLOWED_KEYS not found in lib/config.ts"
    return re.findall(r'"([^"]+)"', m.group(1))


# ── H154: no readiness probe for a library recipe ────────────────────────────


def test_h154_readiness_path_is_null():
    env = _json(ENVIRONMENT)
    recipe = env["recipe"]
    assert recipe["start"] is None
    assert recipe["port"] is None
    # "/" claimed a readiness endpoint the library does not serve.
    assert recipe.get("readinessPath") is None


# ── H407: evidence line no longer cites a non-existent extra ─────────────────


def test_h407_evidence_matches_actual_extras():
    env = _json(ENVIRONMENT)
    evidence = "\n".join(env["recipe"]["evidence"])
    assert "installiert in der Test-Umgebung" in evidence
    assert "webui extras" not in evidence
    # there really is no webui extra — fastapi lives in `all`
    pyproject = PYPROJECT.read_text()
    assert not re.search(r"^webui\s*=", pyproject, re.M)
    assert "fastapi>=0.100.0" in pyproject


# ── H155 / H408: peer range bounded (H408: other pins are deliberate) ───────


def test_h155_peer_range_is_bounded():
    pkg = _json(PKG)
    assert pkg["peerDependencies"]["openclaw"] == ">=2026.5.7 <2027.0.0"


def test_h408_other_version_pins_are_deliberately_distinct():
    """H408 marked as deliberately kept: four `2026.5.7` sites, four meanings.

    peerDependencies is a range (H155 bounds it); compat.pluginApi is the host
    API gate; minGatewayVersion is the gateway floor; build.openclawVersion is
    the version this bundle was built against. Collapsing them would change the
    bundle contract, so only the peer range is touched.
    """
    pkg = _json(PKG)
    assert pkg["openclaw"]["compat"]["pluginApi"] == ">=2026.5.7"
    assert pkg["openclaw"]["compat"]["minGatewayVersion"] == "2026.5.7"
    assert pkg["openclaw"]["build"]["openclawVersion"] == "2026.5.7"


# ── H156: prepack type-checks before building ───────────────────────────────


def test_h156_prepack_runs_check_types():
    scripts = _json(PKG)["scripts"]
    assert scripts["prepack"] == "npm run check-types && npm run build"
    assert "check-types" in scripts


# ── H157: root description moved into metadata ──────────────────────────────


def test_h157_marketplace_description_lives_in_metadata():
    data = _json(MARKETPLACE)
    assert "description" not in data, "top-level description is ignored by the schema"
    assert data["metadata"]["description"]
    assert set(data) <= {"name", "owner", "plugins", "metadata"}


# ── H158: one source of truth for the license ───────────────────────────────


def test_h158_license_is_mit_everywhere():
    assert LICENSE.read_text().lstrip().startswith("MIT License")
    assert _json(MARKETPLACE)["plugins"][0]["license"] == "MIT"
    assert _json(PKG)["license"] == "MIT"


# ── H159/H161: configSchema + uiHints cover every ALLOWED_KEY ───────────────


def test_h159_config_schema_covers_all_allowed_keys():
    schema = _json(PLUGIN_MANIFEST)["configSchema"]["properties"]
    for key in _allowed_keys():
        assert key in schema, f"configSchema is missing {key}"


def test_h161_every_allowed_key_has_a_ui_hint():
    hints = _json(PLUGIN_MANIFEST)["uiHints"]
    for key in _allowed_keys():
        if key == "embedding":
            # `embedding` is an object; its sub-keys carry the hints.
            assert any(h.startswith("embedding.") for h in hints)
            continue
        assert key in hints, f"uiHints is missing {key}"


def test_h161_thought_filter_and_scope_have_hints():
    hints = _json(PLUGIN_MANIFEST)["uiHints"]
    assert hints["thoughtFilter"]["help"]
    assert hints["scope"]["help"]
    assert hints["scope"]["advanced"] is True


# ── H160: dimensions is a positive integer ──────────────────────────────────


def test_h160_dimensions_is_integer_with_minimum():
    dims = _json(PLUGIN_MANIFEST)["configSchema"]["properties"]["embedding"]["properties"]["dimensions"]
    assert dims["type"] == "integer"
    assert dims["minimum"] == 1


# ── H162: documented NEXUS_AGENT_ID split between MCP and hooks ─────────────


def test_h162_plugin_description_documents_agent_id_split():
    desc = _json(CC_PLUGIN)["description"]
    assert "NEXUS_AGENT_ID" in desc
    assert "claude-code" in desc


def test_h162_hooks_document_the_split():
    for name in ("auto_recall.py", "auto_capture.py", "session_start.py"):
        text = (CC_SCRIPTS / name).read_text()
        assert "NEXUS_AGENT_ID" in text, name
        # the note must name the MCP path and its forced id
        assert "MCP" in text, name
        assert "claude-code" in text, name


# ── H409: uiHints "defaults" claims verified against the runtime ─────────────


def test_h409_ui_hint_defaults_match_the_code():
    """H409 is a false alarm: every uiHints default claim is accurate.

    qdrantUrl → DEFAULT_QDRANT_URL, collection → DEFAULT_COLLECTION,
    maxRecallResults → toClampedInt(..., 10, 1, 20) default 10, and the ollama
    baseUrl note matches PROVIDER_DEFAULTS in lib/embedder.ts.
    """
    config = CONFIG_TS.read_text()
    assert 'export const DEFAULT_QDRANT_URL = "http://localhost:6333"' in config
    assert 'export const DEFAULT_COLLECTION = "nexus"' in config
    assert "toClampedInt(cfg.maxRecallResults, 10, 1, 20)" in config

    hints = _json(PLUGIN_MANIFEST)["uiHints"]
    assert "http://localhost:6333" in hints["qdrantUrl"]["help"]
    assert "'nexus'" in hints["collection"]["help"]
    assert hints["maxRecallResults"]["placeholder"] == "10"
    assert "http://localhost:11434" in hints["embedding.baseUrl"]["help"]

    embedder = (CONFIG_TS.parent / "embedder.ts").read_text()
    assert 'baseUrl: "http://localhost:11434"' in embedder
