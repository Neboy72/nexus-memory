"""Wachen fuer Katalog-Auflage 135493 — Plugin ohne Pfad-Chirurgie.

Vier Wachen, jede war ohne den Umbau rot (Rot-Beweis im Bericht):

1. Kein Voranstellen mehr: keine `sys.path`-Operation fuer die Plugin-venv im
   Quelltext, und der Import laesst die Wirts-Umgebung unveraendert.
2. Alter Wirt, neue Umgebung (der vier-Tage-Fall): der Wirt kann das
   Einbettungspaket nicht bedienen -> kein Pfad-Eingriff, der Ausfall wird LAUT
   gemeldet, und die Lese-Antwort ist NICHT leer.
3. Dienst antwortet: der Vektor kommt vom Dienst (Modell/Dimension aus der
   Antwort) und NICHT aus einem Import im Wirtsprozess.
4. Dienst fehlt: das Plugin laedt, `embedder_status()` sagt es, der
   Reparaturbefehl nennt den Dienst, und die Lese-Antwort ist nicht leer.

Das Plugin wird — wie in `tests/test_hermes_plugin.py` — point-blank von seinem
Dateipfad geladen, um nicht mit dem Top-Level-Paket `nexus` am Repo-Root zu
kollidieren. `NEXUS_DATA_DIR` zeigt auf ein tmp-Verzeichnis, damit kein echter
Dienst gestartet und keine echte venv beruehrt wird.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SRC = str(_REPO_ROOT / "src")
_PLUGIN_PATH = _REPO_ROOT / "plugins" / "memory" / "nexus" / "__init__.py"

if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

_DEAD_URL = "http://127.0.0.1:1"  # nothing listens on port 1


def _load_plugin(name: str = "nexus_plugin_embed_guards"):
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


@pytest.fixture()
def data_dir(tmp_path, monkeypatch):
    """A private data dir with NO plugin-venv, so nothing real is ever started."""
    monkeypatch.setenv("NEXUS_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("NEXUS_SERVE_PORT", raising=False)
    monkeypatch.delenv("NEXUS_EMBEDDING_PROVIDER", raising=False)
    monkeypatch.delenv("NEXUS_EMBEDDING_MODEL", raising=False)
    return tmp_path


# ── 1: kein Voranstellen, kein Pfad-Eingriff beim Import ────────────────────

def test_no_prepend_and_host_path_unchanged(data_dir, monkeypatch):
    """Nirgends `insert(0, <plugin-venv>)`; der Import laesst sys.path in Ruhe."""
    source = _PLUGIN_PATH.read_text(encoding="utf-8")
    prepend_needle = "insert(" + "0,"  # split so this guard itself stays grep-clean
    assert prepend_needle not in source, (
        "the plugin must not prepend anything to sys.path (catalog rule 135493)")
    assert "addsitedir" not in source, (
        "the embedding path must not process the private venv's .pth files "
        "inside the host process")

    # A host that CAN import the local embedding package previously still got
    # the plugin venv attached. Now the import must not touch sys.path at all.
    fake_venv = data_dir / "plugin-venv"
    site = (fake_venv / "lib"
            / f"python{sys.version_info.major}.{sys.version_info.minor}"
            / "site-packages")
    site.mkdir(parents=True)
    before = list(sys.path)

    _load_plugin()

    assert sys.path == before, "importing the plugin must not change sys.path"
    assert str(site) not in sys.path and str(fake_venv) not in sys.path
    assert sys.path[0] == before[0], "sys.path[0] (the host's own) must not move"


# ── 2: alter Wirt, neue Umgebung — Ausfall LAUT, Antwort nicht leer ─────────

def test_old_host_is_loud_not_empty(data_dir, monkeypatch):
    """Kann der Wirt nicht einbetten, meldet das Plugin den Ausfall sichtbar."""
    monkeypatch.setenv("NEXUS_EMBED_SERVICE_URL", _DEAD_URL)
    plugin = _load_plugin("nexus_plugin_old_host")

    provider = plugin.NexusMemoryProvider()
    # The four-day case: the host cannot serve an embedding provider.
    provider._embedder = plugin._Embedder()
    provider._qdrant = object()  # non-None, but never reached (embedder is down)
    provider._session_id = "old-host-session"

    assert provider._embedder.uses_service is True
    assert provider._embedder_available() is False

    out = provider._recall("alter wirt, neue umgebung")
    assert isinstance(out, list) and out, "a read answer must never be an empty list"
    note = out[0]["text"]
    assert note.startswith("⚠ local embedder unavailable:")
    assert "fix:" in note


# ── 3: Dienst antwortet — Vektor vom Dienst, kein Import im Wirt ────────────

class _FakeEmbedService(BaseHTTPRequestHandler):
    vector = [0.5, 0.25, 0.125, 0.0625]
    model = "fake-service-model"

    def log_message(self, *a):  # silence the test output
        pass

    def do_GET(self):
        if self.path == "/healthz":
            body = json.dumps({"status": "ok", "qdrant": True}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path != "/embed":
            self.send_response(404)
            self.end_headers()
            return
        length = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(length) or b"{}")
        assert payload.get("text"), "the plugin must send a non-empty text"
        body = json.dumps({
            "embedding": list(self.vector),
            "model": self.model,
            "dim": len(self.vector),
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture()
def fake_service():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeEmbedService)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def test_vector_comes_from_an_answering_service(data_dir, monkeypatch, fake_service):
    """Mit laufendem /embed kommt der Vektor vom Dienst — nicht aus dem Wirt."""
    monkeypatch.setenv("NEXUS_EMBED_SERVICE_URL", fake_service)
    plugin = _load_plugin("nexus_plugin_service_ok")

    # Prove the vector is NOT from a host-side import.
    for name in ("sentence_transformers", "torch", "transformers"):
        monkeypatch.delitem(sys.modules, name, raising=False)

    embedder = plugin._Embedder()
    assert embedder.uses_service is True
    vector = embedder.embed("hallo dienst")

    assert vector == list(_FakeEmbedService.vector)
    assert embedder.dim == len(_FakeEmbedService.vector)
    assert embedder.model_name == _FakeEmbedService.model
    assert "sentence_transformers" not in sys.modules, (
        "the vector came from the host, not the service")


# ── 4: Dienst fehlt — Status sagt es, Reparaturbefehl, keine leere Liste ────

def test_missing_service_reports_status_and_repair(data_dir, monkeypatch):
    """Ohne Dienst: Plugin laedt, Status + Reparaturbefehl, keine leere Liste."""
    monkeypatch.setenv("NEXUS_EMBED_SERVICE_URL", _DEAD_URL)
    plugin = _load_plugin("nexus_plugin_service_missing")

    status = plugin.embedder_status()
    assert set(status) == {"status", "source", "detail", "repair", "endpoint"}
    assert status["status"] == "unavailable"
    assert status["source"] == "none"
    assert status["endpoint"] == _DEAD_URL
    assert status["detail"]

    repair = status["repair"]
    assert "plugin-venv" in repair
    # Den EINSTIEG prueft Wache 5 (er muss wirklich existieren); hier genuegt:
    # es ist ein Startbefehl fuer den Dienst, nicht fuer den Wirt.
    assert " serve" in repair, f"Startbefehl fuer den Dienst erwartet: {repair}"
    # Never the host interpreter, never a pip install into the host.
    assert sys.executable not in repair

    # The plugin still loads and answers reads with the visible note.
    provider = plugin.NexusMemoryProvider()
    provider._embedder = plugin._Embedder()
    provider._qdrant = object()
    provider._session_id = "missing-service-session"
    out = provider._recall("q")
    assert isinstance(out, list) and out, "no empty list when the service is gone"
    assert out[0]["text"].startswith("⚠ local embedder unavailable:")


# ── 5: der Reparaturbefehl zeigt auf einen Einstieg, den es WIRKLICH gibt ───

def test_repair_command_names_a_real_entry_point(data_dir, monkeypatch):
    """Fund 12.10.2026 beim Nachpruefen (nicht vom Bau gemeldet): der Befehl
    nannte ``-m nexus_memory.serve`` — dieses Modul gibt es im Baum nicht; die
    CLI ist ``nexus_memory.mcp_server:cli`` mit dem Unterbefehl ``serve``
    (pyproject: ``nexus-memory``). Ein kopierbarer Hinweis, der scheitert, ist
    schlimmer als kein Hinweis: der Nutzer haelt sich daran und liest
    "No module named ...". (Der Dienststart selbst haette damit ebenfalls nie
    funktioniert — er waere still gestorben.)

    Geprueft wird der genannte EINSTIEG, nicht der venv-Pfad (die venv ist im
    Test fake): (a) der Unterbefehl ``serve`` steht drin, (b) jedes per ``-m``
    genannte Modul existiert wirklich, (c) nie ``sys.executable``, nie
    ``pip install`` in den Wirt.
    """
    import re as _re
    mod = _load_plugin()
    cmd = mod._embedder_repair_command()
    assert " serve" in cmd, f"der Dienst braucht den Unterbefehl 'serve': {cmd}"
    assert "pip install" not in cmd, f"kein pip install in den Wirt: {cmd}"
    assert sys.executable not in cmd, f"nie der Wirt-Interpreter: {cmd}"
    for name in _re.findall(r"-m\s+([A-Za-z_][\w.]*)", cmd):
        assert importlib.util.find_spec(name) is not None, (
            f"Modul '{name}' gibt es nicht — dieser Hinweis wuerde scheitern: {cmd}")
