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

@pytest.fixture(autouse=True)
def isolated_agents_registry(tmp_path, monkeypatch):
    """Redirect agents.json to a temp file for EVERY test.

    Tests must never touch the real ``~/.nexus-memory/agents.json``: a
    registry write from a test with a broken path-patch corrupted real
    agent state on 31.08.2026. The module binds its path helper lazily
    via ``_get_agents_registry_path()``, so patching the module attr is
    sufficient for every code path (load/save/register/cleanup).
    """
    import nexus_memory.agent_detect as _ad

    path = tmp_path / "agents.json"
    monkeypatch.setattr(_ad, "_get_agents_registry_path", lambda: path)
    yield path


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


# ---------------------------------------------------------------------------
# Environment-dependent test detection (CI portability).
#
# A subset of the suite requires a REAL, live environment:
#   * a Qdrant server listening on localhost:6333 (integration tests)
#   * a VOYAGE_API_KEY (the production 1024-dim embedder)
#   * the openclaw plugin's built dist/ bundle
#
# On machines that lack these (GitHub runners, fresh containers) those tests
# are skipped with an explicit reason instead of failing — the failures were
# environmental, not code bugs (proven 2026-09-14: identical failures on a
# bare ubuntu-python3.11 docker replica and the GitHub runner).
# ---------------------------------------------------------------------------

def _qdrant_reachable() -> bool:
    """True when a Qdrant server answers on the configured host:port."""
    try:
        from qdrant_client import QdrantClient  # noqa: F401
    except Exception:
        return False
    host = os.environ.get("NEXUS_QDRANT_HOST", "localhost")
    try:
        port = int(os.environ.get("NEXUS_QDRANT_PORT", "6333"))
    except ValueError:
        port = 6333
    import socket
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


_requires_qdrant = pytest.mark.skipif(
    not _qdrant_reachable(),
    reason="requires a live Qdrant on localhost:6333 (CI/fresh containers have none)",
)

_requires_voyage = pytest.mark.skipif(
    not os.environ.get("VOYAGE_API_KEY"),
    reason="requires VOYAGE_API_KEY for the production 1024-dim embedder "
           "(intentionally absent on CI/fresh containers)",
)

_requires_openclaw_dist = pytest.mark.skipif(
    not (Path(__file__).resolve().parent.parent / "plugins" / "openclaw" / "dist" / "index.js").exists(),
    reason="requires the built openclaw plugin bundle (plugins/openclaw/dist/index.js)",
)
