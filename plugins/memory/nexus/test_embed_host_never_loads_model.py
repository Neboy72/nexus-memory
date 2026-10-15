"""Auf Apple Silicon laedt der Wirtsprozess kein Einbettungsmodell mehr.

Gemessen am 15.10.2026 (Gateway-Log): dort loest sentence-transformers von
selbst auf ``mps`` auf, und PyTorch-MPS bricht bei zwei gleichzeitigen
Einbettungen im selben Prozess den GANZEN Prozess ab (Metal-Assertion). Neun
harte Abbrueche in fuenf Tagen, jedes Mal mitten im Agenten, der sein eigenes
Gedaechtnis schrieb — weil das Plugin das Modell im Wirt lud, obwohl der Dienst
auf ``POST /embed`` bereitstand.

Ausserhalb von Apple Silicon bleibt der Wirt wie zuvor der naheliegende Weg;
``NEXUS_EMBED_IN_HOST=1`` erzwingt ihn ueberall.

Dieser Test laedt das Plugin point-blank von seinem Dateipfad (wie
``test_embed_service_guards.py``), legt ein Fake-Einbetter-Modul in
``sys.modules`` und prueft, ob der Wirt es ueberhaupt anfasst.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import types
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SRC = _REPO_ROOT / "src"
_PLUGIN_PATH = _REPO_ROOT / "plugins" / "memory" / "nexus" / "__init__.py"

if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def _load_plugin(name: str = "nexus_plugin_host_model_guard"):
    """Load plugins/memory/nexus/__init__.py by path, detached from any cache."""
    spec = importlib.util.spec_from_file_location(name, _PLUGIN_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    saved_path = list(sys.path)
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.path[:] = saved_path
    return mod


def _uname(sysname: str, machine: str):
    return types.SimpleNamespace(
        sysname=sysname, machine=machine, release="", version="", nodename=""
    )


@pytest.fixture()
def fake_embedder(monkeypatch, tmp_path):
    """A fake nexus_memory.embeddings whose provider records that it was built."""
    built: dict = {}

    class _FakeProvider:
        def __init__(self, *args, **kwargs):
            built["provider_built"] = True
            self.model_name = "fake/model"
            self.dim = 8

    package = types.ModuleType("nexus_memory")
    package.__path__ = []  # type: ignore[attr-defined]
    embeddings = types.ModuleType("nexus_memory.embeddings")
    embeddings.EmbeddingProvider = _FakeProvider  # type: ignore[attr-defined]
    package.embeddings = embeddings  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "nexus_memory", package)
    monkeypatch.setitem(sys.modules, "nexus_memory.embeddings", embeddings)

    monkeypatch.setenv("NEXUS_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("NEXUS_EMBED_SERVICE_URL", raising=False)
    monkeypatch.delenv("NEXUS_EMBED_IN_HOST", raising=False)
    return built


def test_apple_silicon_host_does_not_load_a_model(monkeypatch, fake_embedder):
    """Apple Silicon: der Wirt baut KEINEN Einbetter — der Vektor kommt vom Dienst."""
    monkeypatch.setattr(os, "uname", lambda: _uname("Darwin", "arm64"))
    mod = _load_plugin()
    monkeypatch.setattr(mod, "_ensure_embed_service", lambda: (False, None))

    embedder = mod._Embedder()

    assert not fake_embedder.get("provider_built"), (
        "the plugin built the embedding provider inside the host process on Apple "
        "Silicon — that is the load that killed the gateway (MPS abort); the vector "
        "must come from the local service instead"
    )
    assert embedder._impl is None


def test_explicit_opt_in_still_builds_in_host(monkeypatch, fake_embedder):
    """NEXUS_EMBED_IN_HOST=1 bleibt der ausdrueckliche Hebel, auch auf Apple Silicon."""
    monkeypatch.setattr(os, "uname", lambda: _uname("Darwin", "arm64"))
    monkeypatch.setenv("NEXUS_EMBED_IN_HOST", "1")
    mod = _load_plugin()
    monkeypatch.setattr(mod, "_ensure_embed_service", lambda: (False, None))

    embedder = mod._Embedder()

    assert fake_embedder.get("provider_built"), (
        "NEXUS_EMBED_IN_HOST=1 must still allow the host to serve its own provider"
    )
    assert embedder._impl is not None


def test_other_platforms_keep_the_host_path(monkeypatch, fake_embedder):
    """Ausserhalb von Apple Silicon bleibt der Wirt der naheliegende Weg (keine Regression)."""
    monkeypatch.setattr(os, "uname", lambda: _uname("Linux", "x86_64"))
    mod = _load_plugin()
    monkeypatch.setattr(mod, "_ensure_embed_service", lambda: (False, None))

    embedder = mod._Embedder()

    assert fake_embedder.get("provider_built"), (
        "on platforms without MPS the host provider stays the default path"
    )
    assert embedder._impl is not None
