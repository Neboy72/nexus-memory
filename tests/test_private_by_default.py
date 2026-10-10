"""Private by default — the promise a fresh install makes (10.10.2026).

Rule (maintainer decision, 10.10.2026): an agent the user connects themselves is
the owner of this memory and starts at the owner level; memories are stored closed
unless a level is named. The reason is behavioural, not theoretical: conversations
are stored as ``private``, so an agent registered as ``public`` would not see its
own memories and the first run would look broken.

These tests pin the defaults down so they cannot drift back silently — the same
class of failure the catalog review found twice (a documented promise that the
installed code did not keep).
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent


# ── 1. the write path ─────────────────────────────────────────────────────────

def test_mcp_remember_defaults_to_private():
    from nexus_memory import mcp_server

    sig = inspect.signature(mcp_server.MemoryStore.remember)
    assert sig.parameters["access_level"].default == mcp_server.ACCESS_PRIVATE, (
        "remember() defaults to something other than private — a fresh install "
        "would store open memories"
    )


def test_mcp_tool_schema_declares_private():
    src = (_REPO / "src" / "nexus_memory" / "mcp_server.py").read_text(encoding="utf-8")

    # The WRITE schema (remember) must offer private as its default ...
    write_block = src[src.index("Who can see this"):]
    write_block = write_block[:300]
    assert '"default": ACCESS_PRIVATE' in write_block, (
        "the remember tool schema does not declare private as its default"
    )
    # ... while every WRITE path must fall back to private as well.
    assert "access_level: str = ACCESS_PUBLIC" not in src, (
        "a write path still defaults to ACCESS_PUBLIC"
    )
    assert 'arguments.get("access_level", ACCESS_PUBLIC)' not in src, (
        "the MCP dispatch still falls back to ACCESS_PUBLIC"
    )
    # The READ filter may — and should — stay at the narrowest view.
    assert 'filter_level' in src, "the read filter disappeared"


def test_plugin_upsert_defaults_to_private():
    """The Hermes provider must not write open memories when no level is given."""
    import importlib.util
    import sys

    path = _REPO / "plugins" / "memory" / "nexus" / "__init__.py"
    spec = importlib.util.spec_from_file_location("nexus_plugin_defaults", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)

    schema_default = (
        module.REMEMBER_SCHEMA["parameters"]["properties"]["access_level"]["default"]
    )
    assert schema_default == "private", (
        "nexus_remember still advertises 'public' as its default"
    )

    src = path.read_text(encoding="utf-8")
    for stale in (
        'access_level: str = "public"',
        'args.get("access_level", "public")',
    ):
        assert stale not in src, f"a write path still defaults to public: {stale!r}"


# ── 2. the connect path ───────────────────────────────────────────────────────

def test_install_agent_defaults_to_private():
    from nexus_memory import setup as setup_mod

    sig = inspect.signature(setup_mod.install_agent)
    assert sig.parameters["trust_level"].default == "private", (
        "a freshly installed agent no longer lands on the owner level"
    )


def test_connect_paths_register_the_owner_level():
    """Both the CLI and the dashboard must register a user-connected agent as private."""
    setup_src = (_REPO / "src" / "nexus_memory" / "setup.py").read_text(encoding="utf-8")
    assert 'else "private"' in setup_src, (
        "the setup CLI no longer falls back to 'private' for a new agent"
    )
    assert 'suggested = "private"' in setup_src, (
        "the interactive setup no longer suggests the owner level"
    )

    dash_src = (_REPO / "dashboard" / "server.py").read_text(encoding="utf-8")
    block = dash_src[dash_src.index("register_agent("):]
    block = block[: block.index(")", block.index("config_dir="))]
    assert 'trust_level="private"' in block, (
        "connecting an agent from the dashboard does not register it as the owner level"
    )


# ── 3. the documentation must say what the code does ──────────────────────────

def test_handbook_and_readme_describe_the_closed_default():
    handbook = (_REPO / "dashboard" / "handbook" / "access-levels.html").read_text(
        encoding="utf-8")
    assert "saved as <b>private</b>" in handbook, (
        "the handbook still describes the old open default"
    )
    assert "saved as <b>public</b>" not in handbook, (
        "the handbook still claims memories default to public"
    )

    readme = (_REPO / "README.md").read_text(encoding="utf-8")
    assert "Default: `private`" in readme, (
        "the README does not state the closed default"
    )


@pytest.mark.parametrize("word", ["public", "trusted", "private"])
def test_all_three_levels_still_exist(word):
    """Closing the default must not remove the ability to open an entry up."""
    from nexus_memory import mcp_server

    assert word in mcp_server.ALL_ACCESS_LEVELS
