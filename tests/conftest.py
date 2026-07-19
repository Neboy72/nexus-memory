"""Pytest configuration + shared fixtures for the nexus-memory test-suite.

The conftest sets ``NEXUS_COLLECTION`` to a test-only name *before* any nexus
module is imported, then exposes a handful of fixtures that the MCP-server
and library tests share:

- ``isolated_env`` — strips embedding-provider API keys from the environment
  *and* the MCP-server module's import-time constants so tests run
  hermetically.
- ``mock_qdrant_client`` — a fully-mocked ``QdrantClient`` (no network) that
  records ``upsert`` / ``query_points`` / ``retrieve`` / ``delete`` /
  ``get_collections`` calls so tests can assert on payloads.
- ``make_fake_point`` — builds a ``ScoredPoint``/``Record`` stand-in that
  mimics the bits of the Qdrant SDK we actually use.
"""

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# 1. Set the test collection name BEFORE any nexus import.
# ---------------------------------------------------------------------------
os.environ.setdefault("NEXUS_COLLECTION", "test-collection")

# Make sure both the source-layout (``src/nexus_memory``) and the legacy
# top-level ``nexus`` package are importable when tests are run from the
# repo root.
_REPO_ROOT = Path(__file__).resolve().parent.parent
for _candidate in (_REPO_ROOT / "src", _REPO_ROOT):
    s = str(_candidate)
    if s not in sys.path:
        sys.path.insert(0, s)


# ---------------------------------------------------------------------------
# 2. Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_env(monkeypatch):
    """Strip embedding-provider API keys from the environment.

    The MCP server's ``EmbeddingProvider._detect()`` walks Voyage → OpenAI →
    Google → Jina → Ollama → sentence-transformers. With no API keys and
    no Ollama running, the detector deterministically falls through to
    ``sentence-transformers`` (or the "no provider" warning if that import
    is also missing). Tests use this fixture to avoid leaking the
    developer's real credentials into the assertion path.

    Note: ``mcp_server.py`` reads the env vars *at import time* (it binds
    ``VOYAGE_API_KEY = os.environ.get(...)`` as a module-level constant).
    We therefore also patch the module-level constants directly so the
    tests stay hermetic even if a key is set in the developer's shell.
    """
    for var in (
        "VOYAGE_API_KEY",
        "OPENAI_API_KEY",
        "GOOGLE_API_KEY",
        "JINA_API_KEY",
        "NEXUS_REPO_PATH",
        "NEXUS_ENV_FILE",
    ):
        monkeypatch.delenv(var, raising=False)

    # Force a stable collection name and pretend Qdrant is on localhost.
    monkeypatch.setenv("NEXUS_COLLECTION", "test-collection")
    monkeypatch.setenv("NEXUS_QDRANT_HOST", "localhost")
    monkeypatch.setenv("NEXUS_QDRANT_PORT", "6333")

    # Clear the module-level constants that the MCP server captured at
    # import time. Tests that *want* to exercise a particular provider
    # can override these afterwards.
    monkeypatch.setattr("nexus_memory.mcp_server.VOYAGE_API_KEY", "")
    monkeypatch.setattr("nexus_memory.mcp_server.OPENAI_API_KEY", "")
    monkeypatch.setattr("nexus_memory.mcp_server.GOOGLE_API_KEY", "")

    # Also clear the constants in embeddings.py — it has its own
    # import-time bindings that EmbeddingProvider._detect() reads
    # directly (line 148: `if not VOYAGE_API_KEY`). Without this,
    # the developer's real ~/.hermes/.env leaks into test assertions.
    monkeypatch.setattr("nexus_memory.embeddings.VOYAGE_API_KEY", "")
    monkeypatch.setattr("nexus_memory.embeddings.OPENAI_API_KEY", "")
    monkeypatch.setattr("nexus_memory.embeddings.GOOGLE_API_KEY", "")
    return monkeypatch


@pytest.fixture
def mock_qdrant_client(monkeypatch):
    """Patch ``QdrantClient`` in the MCP server module with a MagicMock.

    The mock supports the methods that ``MemoryStore`` actually calls:
    ``get_collections``, ``create_collection``, ``create_payload_index``,
    ``upsert``, ``query_points``, ``retrieve``, ``delete``.

    Tests can attach side effects / return values to ``mock.client.<method>``
    to simulate Qdrant responses without running a real server.
    """
    mock_cls = MagicMock(name="QdrantClient")
    # Default: collection does not exist yet → triggers create_collection.
    mock_cls.return_value.get_collections.return_value = MagicMock(
        collections=[]
    )
    # The mock is module-level in mcp_server: import-time `QdrantClient(host=..., port=...)`.
    monkeypatch.setattr("qdrant_client.QdrantClient", mock_cls, raising=False)
    monkeypatch.setattr(
        "nexus_memory.mcp_server.QdrantClient", mock_cls, raising=False
    )
    return mock_cls.return_value


class _FakePoint:
    """Stand-in for ``qdrant_client.http.models.Record`` / ``ScoredPoint``.

    The MCP server only ever reads ``.id``, ``.payload``, and ``.score``
    off these objects — a tiny duck-type is enough.
    """

    def __init__(self, point_id: str, payload: dict, score: float = 0.9):
        self.id = point_id
        self.payload = payload
        self.score = score


def make_fake_point(point_id: str, payload: dict, score: float = 0.9) -> _FakePoint:
    """Public helper so test files can build fake Qdrant points."""
    return _FakePoint(point_id, payload, score)
