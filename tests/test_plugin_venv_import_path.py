"""The plugin must reach its OWN venv — for every dependency, and as one set.

Regression cover for the four-day bug of 11.10.2026, where the agent's own
memory was unreachable while every surface looked healthy.

Two faults, both pinned here:

1. The venv was attached inside the qdrant repair branch only. On a host that
   HAS qdrant_client but NOT sentence-transformers (Hermes' own plugin
   environment), that branch never ran, so the venv was never reached. The
   embedding provider then fell fail-closed and every recall returned an empty
   list — indistinguishable from an empty memory.

2. The venv was appended with ``sys.path.append``, which does NOT process the
   venv's ``.pth`` files. The editable-installed ``nexus_memory`` package stayed
   invisible ("No module named 'nexus_memory'") even though its directory was
   on the search path.

And one ordering rule, which is the actual fix: appending is right when the
host can serve the path, because a package-manager install should win. When the
host cannot (no local embedding backend at all), taking the venv as a MIXTURE
is worse than taking it whole — the host carried huggingface_hub 1.24.0, which
is *importable but too old* for sentence-transformers 6.1.0 ("cannot import name
'ResolvedRevision'"). A sibling that is present but incompatible is the worst
case, because nothing looks broken. In that case the venv goes FIRST.

The plugin module is loaded point-blank from its file path (same trick as
``test_hermes_plugin.py``) to avoid colliding with the top-level ``nexus``
package at the repo root.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_plugin(name: str = "nexus_plugin_venvpath"):
    """Load plugins/memory/nexus/__init__.py by path, detached from any cache."""
    path = _REPO_ROOT / "plugins" / "memory" / "nexus" / "__init__.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def plugin():
    saved_path = list(sys.path)
    saved_modules = dict(sys.modules)
    try:
        yield _load_plugin()
    finally:
        sys.path[:] = saved_path
        for key in list(sys.modules):
            if key not in saved_modules:
                del sys.modules[key]


@pytest.fixture()
def fake_venv(tmp_path, monkeypatch, plugin):
    """Point the venv helpers at an empty fake venv inside tmp_path."""
    venv = tmp_path / "plugin-venv"
    site_pkgs = venv / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages"
    site_pkgs.mkdir(parents=True)
    monkeypatch.setattr(plugin, "_plugin_venv_dir", lambda: venv)
    monkeypatch.setattr(plugin, "_plugin_venv_site_packages", lambda _v: site_pkgs)
    return site_pkgs


def _force_find_spec(monkeypatch, found: bool, target: str = "sentence_transformers"):
    """Make importlib.util.find_spec report `target` as present/absent."""
    real = importlib.util.find_spec

    def _fake(name, *args, **kwargs):
        if name == target:
            return object() if found else None
        return real(name, *args, **kwargs)

    monkeypatch.setattr(importlib.util, "find_spec", _fake)


def test_venv_goes_first_when_host_cannot_embed(plugin, fake_venv, monkeypatch):
    """No local embedding backend in the host -> the venv wins, as one set.

    A mixture would import sentence-transformers from the venv while
    huggingface_hub/tokenizers/numpy still came from the host.
    """
    monkeypatch.setattr(sys, "path", list(sys.path))
    _force_find_spec(monkeypatch, found=False)
    sys.path[:] = [p for p in sys.path if p != str(fake_venv)]

    plugin._ensure_plugin_venv_on_path()

    assert sys.path[0] == str(fake_venv), "venv must win when the host has no local backend"


def test_venv_is_appended_when_host_can_embed(plugin, fake_venv, monkeypatch):
    """A managed install wins: the venv is only a last-resort repair path."""
    monkeypatch.setattr(sys, "path", list(sys.path))
    _force_find_spec(monkeypatch, found=True)
    sys.path[:] = [p for p in sys.path if p != str(fake_venv)]

    plugin._ensure_plugin_venv_on_path()

    assert str(fake_venv) in sys.path
    assert sys.path[0] != str(fake_venv), "a healthy host install must not be shadowed"


def test_idempotent_and_fail_open(plugin, fake_venv, monkeypatch, tmp_path):
    """A second call changes nothing; a missing venv raises nothing."""
    monkeypatch.setattr(sys, "path", list(sys.path))
    _force_find_spec(monkeypatch, found=False)
    sys.path[:] = [p for p in sys.path if p != str(fake_venv)]

    plugin._ensure_plugin_venv_on_path()
    first = list(sys.path)
    plugin._ensure_plugin_venv_on_path()
    assert sys.path == first, "a second call must not duplicate the entry"

    monkeypatch.setattr(plugin, "_plugin_venv_site_packages", lambda _v: None)
    plugin._ensure_plugin_venv_on_path()  # must not raise

    monkeypatch.setattr(plugin, "_plugin_venv_site_packages", lambda _v: tmp_path / "nope")
    monkeypatch.setattr(plugin, "_plugin_venv_dir", lambda: tmp_path / "nope")
    plugin._ensure_plugin_venv_on_path()  # must not raise on an unusable path


def test_uses_addsitedir_so_pth_files_are_processed(plugin):
    """The venv is attached via site.addsitedir, never a bare append.

    Only addsitedir processes the venv's ``.pth`` files; a bare append left the
    editable-installed ``nexus_memory`` package itself invisible.
    """
    source = (_REPO_ROOT / "plugins" / "memory" / "nexus" / "__init__.py").read_text(encoding="utf-8")
    assert "addsitedir" in source
    # The venv must be attached for ALL dependencies, not inside the qdrant
    # repair branch: that branch never runs when the host HAS qdrant_client.
    call_site = source.index("_ensure_plugin_venv_on_path()")
    qdrant_branch = source.index("def _try_import_qdrant")
    assert call_site < qdrant_branch, "the venv must be attached before/independently of the qdrant branch"
