"""Pytest tests for the Nexus Memory Hermes Plugin (MemoryProvider).

Tests the NexusMemoryProvider class from plugins/memory/nexus/__init__.py,
which speaks directly to Qdrant via qdrant_client and uses the shared
EmbeddingProvider from nexus_memory.embeddings.

The plugin lives at plugins/memory/nexus/ (package name: 'nexus'), but the
repo root also contains a top-level 'nexus' package.  To avoid import
ambiguity we load the plugin module point-blank with importlib.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Path setup — must happen before any Nexus imports
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC = str(_REPO_ROOT / "src")

# Make nexus_memory.* importable (needed by the plugin's internal imports)
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

# Load the Hermes plugin module directly from its file path so we don't
# collide with the top-level ``nexus`` package at the repo root.
_PLUGIN_PATH = _REPO_ROOT / "plugins" / "memory" / "nexus" / "__init__.py"
_spec = importlib.util.spec_from_file_location(
    "nexus_hermes_plugin", str(_PLUGIN_PATH)
)
assert _spec is not None, f"Could not create module spec for {_PLUGIN_PATH}"
nexus_plugin = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(nexus_plugin)

NexusMemoryProvider = nexus_plugin.NexusMemoryProvider
_Embedder = nexus_plugin._Embedder
_QD_HOST = nexus_plugin._HOST
_QD_PORT = nexus_plugin._PORT
_COLLECTION = nexus_plugin._COLLECTION


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def provider():
    """Return a fresh, un-initialised NexusMemoryProvider instance."""
    return NexusMemoryProvider()


@pytest.fixture
def tmp_hermes_home():
    """Create a temp directory to use as hermes_home during testing."""
    tmp = tempfile.mkdtemp(prefix="test-nexus-")
    yield tmp
    shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture
def initialized_provider(tmp_hermes_home):
    """Return a fully initialised provider (connected to real Qdrant)."""
    p = NexusMemoryProvider()
    p.initialize("test-session", hermes_home=tmp_hermes_home)
    yield p
    # Shut down cleanly
    try:
        p.shutdown()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestProviderBasics:
    """Tests that don't require initialisation."""

    def test_provider_name(self, provider):
        """Provider name must be 'nexus'."""
        assert provider.name == "nexus"

    def test_is_available(self, provider):
        """is_available returns True when Qdrant is up and nexus_memory is importable."""
        assert provider.is_available() is True

    def test_tool_schemas(self, provider):
        """get_tool_schemas returns exactly 3 tools with the expected names."""
        schemas = provider.get_tool_schemas()
        assert len(schemas) == 3
        names = {s["name"] for s in schemas}
        assert names == {"nexus_recall", "nexus_remember", "nexus_forget"}

    def test_config_schema(self, provider):
        """get_config_schema returns qdrant_url, voyage_api_key, collection_name."""
        schema = provider.get_config_schema()
        assert len(schema) == 3
        keys = {item["key"] for item in schema}
        assert keys == {"qdrant_url", "voyage_api_key", "collection_name"}

    def test_system_prompt_block(self, provider):
        """system_prompt_block returns a non-empty string mentioning Nexus Memory."""
        block = provider.system_prompt_block()
        assert isinstance(block, str)
        assert len(block) > 0
        assert "Nexus Memory" in block


class TestEmbedder:
    """Tests for the internal _Embedder class."""

    def test_embedder_init(self):
        """_Embedder initialises and reports dim > 0."""
        embedder = _Embedder()
        assert embedder.dim > 0
        # With VOYAGE_API_KEY set, we expect 1024
        assert embedder.dim == 1024

    def test_embedder_embed(self):
        """_Embedder.embed('test') returns a list of floats with len == dim."""
        embedder = _Embedder()
        dim = embedder.dim
        vector = embedder.embed("test")
        assert isinstance(vector, list)
        assert len(vector) == dim
        assert all(isinstance(v, float) for v in vector)


class TestProviderInitialized:
    """Tests that require a fully initialised provider."""

    def test_initialize(self, tmp_hermes_home):
        """initialize() does not crash and sets up internals."""
        p = NexusMemoryProvider()
        p.initialize("test-session-init", hermes_home=tmp_hermes_home)
        assert p._session_id == "test-session-init"
        assert p._qdrant is not None
        assert p._embedder is not None
        p.shutdown()

    def test_handle_tool_call_recall(self, initialized_provider):
        """nexus_recall returns valid JSON (even if empty results)."""
        result_json = initialized_provider.handle_tool_call(
            "nexus_recall", {"query": "test recall query", "limit": 3}
        )
        result = json.loads(result_json)
        # Should be a list (or empty list)
        assert isinstance(result, list)

    def test_handle_tool_call_remember(self, initialized_provider):
        """nexus_remember returns JSON with 'id' and 'status': 'ok'."""
        result_json = initialized_provider.handle_tool_call(
            "nexus_remember",
            {"text": "test memory from pytest", "category": "temp"},
        )
        result = json.loads(result_json)
        assert result.get("status") == "ok"
        assert "id" in result
        assert result.get("category") == "temp"

    def test_handle_tool_call_forget(self, initialized_provider):
        """nexus_forget deletes a memory that was just created."""
        # First, create a memory
        remember_json = initialized_provider.handle_tool_call(
            "nexus_remember",
            {"text": "ephemeral test memory to be deleted", "category": "temp"},
        )
        created = json.loads(remember_json)
        assert created.get("status") == "ok"
        memory_id = created["id"]

        # Now delete it
        forget_json = initialized_provider.handle_tool_call(
            "nexus_forget", {"memory_id": memory_id}
        )
        result = json.loads(forget_json)
        assert result.get("status") == "ok"
        assert result.get("id") == memory_id

    def test_handle_unknown_tool(self, initialized_provider):
        """Calling an unknown tool returns JSON with an 'error' key."""
        result_json = initialized_provider.handle_tool_call("unknown_tool", {})
        result = json.loads(result_json)
        assert "error" in result
        assert "Unknown tool" in result["error"]

    def test_save_config(self, tmp_hermes_home):
        """save_config writes a nexus/config.json file."""
        p = NexusMemoryProvider()
        p.save_config(
            {"qdrant_url": "http://localhost:6333", "collection_name": "nexus"},
            tmp_hermes_home,
        )
        config_path = Path(tmp_hermes_home) / "nexus" / "config.json"
        assert config_path.is_file()

        with open(config_path) as f:
            data = json.load(f)
        assert data["qdrant_url"] == "http://localhost:6333"
        assert data["collection_name"] == "nexus"
