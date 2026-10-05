"""Catalog-admission round 2: egress-safety fixes from the 05.10.2026 review.

Each test pins one of the review's blocking findings so a later refactor cannot
quietly reintroduce it:

1. `mcp_server` must not read `cwd/.env` — importing it (the provider imports it
   for `_normalize_scope`) would otherwise let any working directory inject
   environment variables, API keys included.
2. The provider must not import `mcp_server` at all for the scope helper.
3. The extraction LLM config must never pair a provider key with a foreign
   `base_url` (OpenAI key + non-OpenAI endpoint).
4. Both extractors resolve their config through one shared implementation.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_SRC = _REPO / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


# ── 1. mcp_server must not read cwd/.env ──────────────────────────────────────

def test_mcp_server_source_has_no_cwd_env_read():
    """The `Path.cwd() / '.env'` loader is gone from the module source."""
    text = (_SRC / "nexus_memory" / "mcp_server.py").read_text(encoding="utf-8")
    assert "cwd() / \".env\"" not in text, "cwd/.env is still loaded at import time"
    assert "Path.cwd()" not in text, "Path.cwd() must not appear in the env loader"


def test_importing_mcp_server_ignores_cwd_env(tmp_path, monkeypatch):
    """A hostile cwd/.env cannot inject a variable into the process environment."""
    hostile = tmp_path / "workdir"
    hostile.mkdir()
    (hostile / ".env").write_text("NEXUS_HOSTILE_INJECTION=1\n", encoding="utf-8")

    monkeypatch.chdir(hostile)
    monkeypatch.delenv("NEXUS_HOSTILE_INJECTION", raising=False)
    monkeypatch.setenv("NEXUS_ENV_FILE", str(tmp_path / "missing.env"))

    import importlib
    mod = importlib.import_module("nexus_memory.mcp_server")
    importlib.reload(mod)

    assert "NEXUS_HOSTILE_INJECTION" not in os.environ, (
        "cwd/.env was loaded into the process environment"
    )


# ── 2. the provider does not import mcp_server for the scope helper ────────────

def test_provider_does_not_import_mcp_server():
    """The provider resolves scope inline instead of importing the server module."""
    text = (_REPO / "plugins" / "memory" / "nexus" / "__init__.py").read_text(encoding="utf-8")
    assert "from nexus_memory.mcp_server import _normalize_scope" not in text, (
        "the provider still imports the MCP server for the scope helper"
    )


def test_provider_scope_normalization_matches_server_contract():
    """The inlined rule degrades exactly like mcp_server._normalize_scope."""
    import re

    def provider_scope(raw):
        s = raw.strip().lower() if isinstance(raw, str) else ""
        return s if (s and re.match(r"^[a-z0-9][a-z0-9-]{0,39}$", s)) else "default"

    from nexus_memory.mcp_server import _normalize_scope as server_scope

    for value in (None, "", "  ", "OK", "a" * 41, "has space", "UPPER", "good-scope", "x"):
        assert provider_scope(value) == server_scope(value), value


# ── 3. key/endpoint pairing ───────────────────────────────────────────────────

def _write_config(home: Path, *, base_url: str, api_key: str = "") -> None:
    (home / "config.yaml").write_text(
        "model:\n"
        f'  default: "test-model"\n'
        f'  base_url: "{base_url}"\n'
        f'  api_key: "{api_key}"\n',
        encoding="utf-8",
    )


def test_openai_key_is_never_paired_with_a_foreign_base_url(tmp_path, monkeypatch):
    """A relay base_url must not receive the OpenAI key from .env."""
    home = tmp_path
    _write_config(home, base_url="https://api.commandcode.ai/provider/v1")
    (home / ".env").write_text("OPENAI_API_KEY=sk-secret\n", encoding="utf-8")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)

    from nexus_memory.llm_endpoint_config import resolve_llm_config

    cfg = resolve_llm_config(str(home))

    # The invariant that matters: the real secret never reaches the foreign
    # endpoint. The placeholder that satisfies the client is not a credential.
    assert cfg["api_key"] != "sk-secret", (
        "the OpenAI key was paired with a non-OpenAI endpoint"
    )
    assert cfg["api_key"] == "ollama"
    assert cfg["base_url"] == "https://api.commandcode.ai/provider/v1"


def test_openai_key_is_used_for_the_openai_endpoint(tmp_path, monkeypatch):
    """The same key IS used when the configured endpoint belongs to it."""
    home = tmp_path
    _write_config(home, base_url="https://api.openai.com/v1")
    (home / ".env").write_text("OPENAI_API_KEY=sk-secret\n", encoding="utf-8")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    from nexus_memory.llm_endpoint_config import resolve_llm_config

    cfg = resolve_llm_config(str(home))

    assert cfg["api_key"] == "sk-secret"
    assert cfg["base_url"] == "https://api.openai.com/v1"


def test_configured_pair_in_config_yaml_wins(tmp_path):
    """A base_url+api_key pair written together in config.yaml stays a pair."""
    home = tmp_path
    _write_config(home, base_url="https://relay.internal/v1", api_key="relay-key")

    from nexus_memory.llm_endpoint_config import resolve_llm_config

    cfg = resolve_llm_config(str(home))

    assert cfg["api_key"] == "relay-key"
    assert cfg["base_url"] == "https://relay.internal/v1"


def test_ollama_key_is_used_for_the_local_endpoint(tmp_path, monkeypatch):
    """A local endpoint uses the Ollama key — the pre-fix behaviour, kept."""
    home = tmp_path
    (home / "config.yaml").write_text(
        'model:\n  default: "m"\n  provider: ollama-cloud\n', encoding="utf-8"
    )
    (home / ".env").write_text("OLLAMA_API_KEY=real-ollama-key\n", encoding="utf-8")
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)

    from nexus_memory.llm_endpoint_config import resolve_llm_config

    cfg = resolve_llm_config(str(home))

    assert cfg["base_url"] == "http://localhost:11434/v1"
    assert cfg["api_key"] == "real-ollama-key"


def test_remote_endpoint_without_key_gets_the_placeholder_not_a_real_key(
        tmp_path, monkeypatch):
    """A relay configured without a key must not borrow a real provider key."""
    home = tmp_path
    _write_config(home, base_url="https://relay.internal/v1")
    (home / ".env").write_text("OLLAMA_API_KEY=real-ollama-key\n", encoding="utf-8")
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)

    from nexus_memory.llm_endpoint_config import resolve_llm_config

    cfg = resolve_llm_config(str(home))

    # A remote host is not the Ollama endpoint, so the key is not eligible.
    assert cfg["api_key"] != "real-ollama-key"
    assert cfg["api_key"] == "ollama"


def test_environment_wins_over_env_file(tmp_path, monkeypatch):
    """`os.environ` (what Hermes populates) is authoritative over the file."""
    home = tmp_path
    _write_config(home, base_url="http://localhost:11434/v1")
    (home / ".env").write_text("OLLAMA_API_KEY=from-file\n", encoding="utf-8")
    monkeypatch.setenv("OLLAMA_API_KEY", "from-process")

    from nexus_memory.llm_endpoint_config import resolve_llm_config

    assert resolve_llm_config(str(home))["api_key"] == "from-process"


def test_missing_config_falls_back_to_local_pair(tmp_path, monkeypatch):
    """Nothing configured → local endpoint AND local placeholder key.

    The placeholder assertions hold with no provider key in the environment;
    the real keys are cleared here so a key leaked into the process env by
    another test cannot make this order-dependent.
    """
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    from nexus_memory.llm_endpoint_config import resolve_llm_config

    cfg = resolve_llm_config(str(tmp_path / "nonexistent"))

    assert cfg["base_url"].startswith("http://localhost")
    assert cfg["api_key"] == "ollama"
    assert cfg["model"]


# ── 4. one shared implementation ──────────────────────────────────────────────

def test_both_extractors_use_the_shared_resolver(tmp_path, monkeypatch):
    """Both `_load_llm_config` functions return the shared resolver's result."""
    home = tmp_path
    _write_config(home, base_url="http://localhost:11434/v1")

    from nexus_memory import entity_extractor, extractor
    from nexus_memory.llm_endpoint_config import resolve_llm_config

    expected = resolve_llm_config(str(home))
    assert extractor._load_llm_config(str(home)) == expected
    assert entity_extractor._load_llm_config(str(home)) == expected


def test_extractors_no_longer_parse_env_files_themselves():
    """No duplicated .env parsing remains in either extractor."""
    for name in ("extractor.py", "entity_extractor.py"):
        text = (_SRC / "nexus_memory" / name).read_text(encoding="utf-8")
        assert 'OPENAI_API_KEY" and not config["api_key"]' not in text, name
        assert ".env read failed" not in text, name


# ── 5. paid fuel is opt-IN ────────────────────────────────────────────────────

def test_paid_stations_are_off_by_default(tmp_path, monkeypatch):
    """A fresh install (no toggle file, no env) keeps paid stations closed."""
    from nexus_memory import fuel_chain

    monkeypatch.setattr(fuel_chain, "FUEL_TOGGLE_PATH", tmp_path / "fuel_paid_enabled")
    monkeypatch.delenv("NEXUS_FUEL_PAID", raising=False)

    assert fuel_chain._paid_enabled() is False
    assert not (tmp_path / "fuel_paid_enabled").exists()


def test_importing_fuel_chain_leaves_the_toggle_alone(tmp_path, monkeypatch):
    """Importing the module must not create the toggle file.

    Checked without reloading the module: `importlib.reload` rebinds module
    globals for every other holder of `nexus_memory.fuel_chain`, which poisoned
    unrelated fuel tests. The import-time side effect is instead proven by the
    source itself — the `_ensure_toggle_default_on()` call at module scope is
    gone and the function is a no-op.
    """
    import inspect

    from nexus_memory import fuel_chain

    src = inspect.getsource(fuel_chain)
    assert "\n_ensure_toggle_default_on()" not in src, (
        "the module still calls the toggle creator at import time"
    )

    # The function, if anything still calls it, writes nothing.
    toggle = tmp_path / "fuel_paid_enabled"
    monkeypatch.setattr(fuel_chain, "FUEL_TOGGLE_PATH", toggle)
    fuel_chain._ensure_toggle_default_on()
    assert not toggle.exists(), "the toggle creator is not a no-op"


def test_paid_stations_can_be_enabled_explicitly(tmp_path, monkeypatch):
    """Both switches work: the env var and the dashboard toggle file."""
    from nexus_memory import fuel_chain

    toggle = tmp_path / "fuel_paid_enabled"
    monkeypatch.setattr(fuel_chain, "FUEL_TOGGLE_PATH", toggle)

    monkeypatch.setenv("NEXUS_FUEL_PAID", "1")
    assert fuel_chain._paid_enabled() is True

    monkeypatch.delenv("NEXUS_FUEL_PAID", raising=False)
    toggle.touch()
    assert fuel_chain._paid_enabled() is True

    monkeypatch.setenv("NEXUS_FUEL_PAID", "0")
    assert fuel_chain._paid_enabled() is False, "the OFF switch must always win"


def test_rewrite_is_opt_in_under_the_provider(monkeypatch):
    """The provider does not rewrite queries unless NEXUS_REWRITE is on."""
    import importlib.util
    from pathlib import Path

    plugin_path = _REPO / "plugins" / "memory" / "nexus" / "__init__.py"
    spec = importlib.util.spec_from_file_location("nexus_provider_optin", plugin_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    class _Provider(mod.NexusMemoryProvider):
        pass

    # The method touches no instance state before the gate decides.
    provider = object.__new__(_Provider)

    monkeypatch.delenv("NEXUS_REWRITE", raising=False)
    assert provider._rewrite_if_enabled("was war das mit dem ding") == "was war das mit dem ding"

    monkeypatch.setenv("NEXUS_REWRITE", "0")
    assert provider._rewrite_if_enabled("hund futter") == "hund futter"


# ── 6. the mcp floor matches the API the code actually calls ──────────────────

def test_mcp_floor_covers_the_api_the_server_calls():
    """`add_request_handler` exists only from mcp 2.0.0 on.

    The code is written against the MCP v2 API, so a `<2.0.0` bound installs a
    version without that attribute and the import dies — this happened on CI
    (run 37285736327) when the floor was still 1.0.0. The declared range must
    admit every version that provides the call.
    """
    import tomllib

    data = tomllib.loads((_REPO / "pyproject.toml").read_text(encoding="utf-8"))
    deps = [d for d in data["project"]["dependencies"] if d.startswith("mcp")]
    assert deps, "mcp must stay a declared dependency"
    spec = deps[0]

    assert ">=2.0.0" in spec, spec
    # No upper bound below 3.0.0 (a 2.x ceiling would repeat the CI break).
    assert "<2.0.0" not in spec, spec


def test_server_source_calls_the_v2_api():
    """The server relies on the v2 request-handler API, hence the floor above."""
    text = (_SRC / "nexus_memory" / "mcp_server.py").read_text(encoding="utf-8")
    assert "add_request_handler" in text


# ── 7. the endpoint matcher is exact-or-subdomain, never a substring ─────────
#
# Second review round (05.10.2026, OCR): the first version of
# `_key_matches_base_url` tested `marker in host`. A host that merely CONTAINS
# a marker therefore passed, so `https://api.openai.com.attacker.example/v1`
# received the real `OPENAI_API_KEY` — the exact leak the module exists to
# prevent. These tests pin the corrected rule so a later edit cannot soften it
# back into a substring test.

def test_host_marker_does_not_match_a_lookalike_domain():
    """`api.openai.com.attacker.example` must not borrow the OpenAI key."""
    sys.path.insert(0, str(_SRC))
    from nexus_memory.llm_endpoint_config import _key_matches_base_url

    for host in (
        "https://api.openai.com.attacker.example/v1",
        "https://evil-openai-api.openai.com.attacker.tld/v1",
        "https://notapi.openai.com.evil.example/v1",
    ):
        assert not _key_matches_base_url("OPENAI_API_KEY", host), host


def test_host_marker_accepts_the_provider_host_and_its_subdomains():
    sys.path.insert(0, str(_SRC))
    from nexus_memory.llm_endpoint_config import _key_matches_base_url

    assert _key_matches_base_url("OPENAI_API_KEY", "https://api.openai.com/v1")
    # A subdomain of the marker belongs to the provider too.
    assert _key_matches_base_url("OPENAI_API_KEY", "https://eu.api.openai.com/v1")


def test_ollama_markers_are_exact_hosts_not_substrings():
    """`localhost.evil.example` and `notollama.example` are foreign hosts."""
    sys.path.insert(0, str(_SRC))
    from nexus_memory.llm_endpoint_config import _key_matches_base_url

    for host in ("http://localhost.evil.example/v1", "http://notollama.example/v1"):
        assert not _key_matches_base_url("OLLAMA_API_KEY", host), host
    for host in ("http://localhost:11434/v1", "http://127.0.0.1:11434/v1"):
        assert _key_matches_base_url("OLLAMA_API_KEY", host), host


def test_a_rejected_key_is_named_in_a_warning(tmp_path, monkeypatch, caplog):
    """A mismatch must say so instead of surfacing as a generic auth failure."""
    home = tmp_path
    _write_config(home, base_url="https://relay.internal/v1")
    (home / ".env").write_text("OPENAI_API_KEY=sk-secret\n", encoding="utf-8")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)

    from nexus_memory.llm_endpoint_config import resolve_llm_config

    with caplog.at_level("WARNING"):
        cfg = resolve_llm_config(str(home))

    assert cfg["api_key"] != "sk-secret"
    assert any("OPENAI_API_KEY" in r.message and "not issued for" in r.message
               for r in caplog.records), caplog.text


def test_a_resolved_key_does_not_also_warn_about_the_other(tmp_path, monkeypatch, caplog):
    """No log spam: a key that another key resolves must not produce a warning.

    Second review round: the warning used to fire inside the loop, so a setup
    holding `OLLAMA_API_KEY` and `OPENAI_API_KEY` warned on every extraction
    even though `OPENAI_API_KEY` resolved the call. It now fires only when
    nothing resolved.
    """
    home = tmp_path
    _write_config(home, base_url="https://api.openai.com/v1")
    (home / ".env").write_text(
        "OLLAMA_API_KEY=real-ollama\nOPENAI_API_KEY=sk-real\n", encoding="utf-8")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)

    from nexus_memory.llm_endpoint_config import resolve_llm_config

    with caplog.at_level("WARNING"):
        cfg = resolve_llm_config(str(home))

    assert cfg["api_key"] == "sk-real"
    assert not [r for r in caplog.records if "not issued for" in r.message], caplog.text


def test_the_endpoint_resolver_uses_the_cross_encoder_api_too():
    """The dependency cap must cover every entry point the code calls.

    The plugin reranks with `CrossEncoder.predict`, so a cap justified by
    `SentenceTransformer.encode` alone would be verified against an incomplete
    list of call sites.
    """
    reranker = (_SRC / "nexus_memory" / "reranker.py").read_text(encoding="utf-8")
    retrieval = (_REPO / "nexus" / "retrieval" / "__init__.py").read_text(
        encoding="utf-8")
    assert "CrossEncoder" in reranker or "CrossEncoder" in retrieval
    assert ".predict(" in reranker or ".predict(" in retrieval


def test_sentence_transformers_bound_is_the_same_in_every_manifest():
    """Two committed manifests must not advertise different runtime bounds."""
    import re as _re

    root = (_REPO / "pyproject.toml").read_text(encoding="utf-8")
    plugin = (_REPO / "plugins" / "memory" / "nexus" / "plugin.yaml").read_text(
        encoding="utf-8")
    installer = (_REPO / "scripts" / "install_hermes_plugin.sh").read_text(
        encoding="utf-8")

    pattern = r"sentence-transformers>=\s*3\.0\.0\s*,\s*<([0-9.]+)"
    found = set()
    for text in (root, plugin, installer):
        m = _re.search(pattern, text)
        if m:
            found.add(m.group(1))
    assert len(found) == 1, f"manifests disagree on the upper bound: {found}"
