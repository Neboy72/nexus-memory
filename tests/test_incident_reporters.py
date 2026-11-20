"""Both harness plugins report failed tool calls to the neutral feed.

Das Träumen hängt seit 0.22.61 an keinem Harness mehr: es liest
``~/.nexus-memory/events/ereignisse.jsonl``, gefüllt über ``POST /events``.
Ein Plugin, das nichts meldet, sieht von außen genauso aus wie eines, das
funktioniert — deshalb hält dieser Test die Meldung in beiden Quellbäumen
fest. Er liest Text, startet nichts und braucht keinen Dienst.
"""
import importlib.util
import json
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DSH = REPO / "plugins" / "dsh"
OPENCLAW = REPO / "plugins" / "openclaw"
HERMES_PLUGIN = REPO / "plugins" / "memory" / "nexus"
CLAUDE_CODE = REPO / "plugins" / "claude-code"


def _lade(pfad: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, pfad)
    modul = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modul)
    return modul


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_dsh_plugin_reports_to_events() -> None:
    quelle = _text(DSH / "index.js")
    assert "/events" in quelle, "der DSH-Plugin nennt den Endpunkt"
    assert "reportIncidents" in quelle, "es gibt einen Schalter"
    assert "reportIncident(" in quelle, "und einen Aufruf"
    assert "incidentFromToolResult" in quelle, "die Umwandlung ist eine eigene Funktion"


def test_dsh_plugin_only_reports_platform_failures() -> None:
    """Kein Raten im Plugin: nur isError oder ein error-Objekt zählen."""
    quelle = _text(DSH / "index.js")
    assert "isError !== true" in quelle
    assert "error?.reason" in quelle


def test_dsh_plugin_reads_the_harness_exit_marker() -> None:
    """Ein Shell-Befehl mit exit 1 ist ein Fehlschlag, kein Erfolg.

    Der Harness haengt `[exit code: N]` an das Werkzeug-Ergebnis; das ist sein
    Urteil, nicht unsere Lesart des Textes. Ohne diese Regel meldete der
    DSH-Plugin nur Werkzeugfehler und keine einzige gescheiterte Kommandozeile.
    """
    quelle = _text(DSH / "index.js")
    assert "nonZeroExit" in quelle
    assert "[exit code:" in quelle
    assert "|null)" in quelle, "null heisst ohne Code und wird nicht geraten"


def test_dsh_plugin_has_a_cap_and_a_switch() -> None:
    quelle = _text(DSH / "index.js")
    assert "reportPerSessionMax" in quelle
    assert "reportIncidents !== false" in quelle, "Standard ist an"


def test_openclaw_plugin_reports_to_events() -> None:
    lib = _text(OPENCLAW / "lib" / "incidents.ts")
    index = _text(OPENCLAW / "index.ts")
    assert "/events" in lib
    assert "incidentFromToolCall" in lib
    assert "buildIncidentHandler" in index, "der Hook wird registriert"
    assert 'api.on("after_tool_call"' in index


def test_openclaw_manifest_allows_the_switch() -> None:
    """`additionalProperties: false` — ein fehlender Schlüssel wäre ein Fehler."""
    import json

    manifest = json.loads(_text(OPENCLAW / "openclaw.plugin.json"))
    eigenschaften = manifest.get("configSchema", {}).get("properties", {})
    assert "reportIncidents" in eigenschaften
    assert "reportIncidents" in (manifest.get("uiHints") or {})


def test_both_plugins_carry_their_own_offline_tests() -> None:
    assert (DSH / "test-report-incidents.mjs").exists()
    assert (OPENCLAW / "_test_incidents.mjs").exists()


# ── 0.22.64: auch Claude Code und Hermes melden ────────────────────

def test_hermes_plugin_reports_at_session_end() -> None:
    quelle = _text(HERMES_PLUGIN / "__init__.py")
    assert "_report_incidents(messages)" in quelle
    assert "incident_report" in quelle
    assert (HERMES_PLUGIN / "incident_report.py").exists()


def test_hermes_incidents_only_from_marked_failures() -> None:
    modul = _lade(HERMES_PLUGIN / "incident_report.py", "hermes_incident_report")
    messages = [
        {"role": "assistant", "tool_calls": [
            {"id": "c1", "function": {"name": "terminal"}},
            {"id": "c2", "function": {"name": "read_file"}},
        ]},
        {"role": "tool", "tool_call_id": "c1", "content": json.dumps(
            {"output": "Traceback ... ModuleNotFoundError: No module named 'requests'",
             "exit_code": 1, "error": None, "hint": "use the venv interpreter"})},
        {"role": "tool", "tool_call_id": "c2", "content": json.dumps(
            {"content": "ok", "exit_code": 0})},
    ]
    vorfaelle = modul.incidents_from_messages(messages, session="s1")
    assert len(vorfaelle) == 1
    assert vorfaelle[0]["tool"] == "terminal"
    assert vorfaelle[0]["ok"] is False
    assert "requests" in vorfaelle[0]["error"]
    assert vorfaelle[0]["hint"] == "use the venv interpreter"
    assert vorfaelle[0]["session"] == "s1"


def test_hermes_switch_off_sends_nothing(monkeypatch) -> None:
    modul = _lade(HERMES_PLUGIN / "incident_report.py", "hermes_incident_report_off")
    monkeypatch.setenv("NEXUS_REPORT_INCIDENTS", "0")
    aufrufe = []
    assert modul.report_incidents([{"tool": "x", "ok": False, "error": "boom"}]) == 0
    assert aufrufe == []


def test_claude_code_hook_is_registered() -> None:
    hooks = json.loads(_text(CLAUDE_CODE / "hooks" / "nexus-hooks.json"))
    befehle = [h.get("command", "") for gruppe in hooks["hooks"]["Stop"]
               for h in gruppe.get("hooks", [])]
    assert any("report_incidents.py" in befehl for befehl in befehle)


def test_claude_code_reads_the_harness_error_flag(tmp_path) -> None:
    modul = _lade(CLAUDE_CODE / "scripts" / "report_incidents.py", "cc_incidents")
    transcript = tmp_path / "session.jsonl"
    zeilen = [
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls nope"}},
            {"type": "tool_use", "id": "t2", "name": "Read", "input": {}},
        ]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t1", "is_error": True,
             "content": "Exit code 1\nls: nope: No such file or directory"},
            {"type": "tool_result", "tool_use_id": "t2", "content": "datei-inhalt"},
        ]}},
    ]
    transcript.write_text("\n".join(json.dumps(z) for z in zeilen) + "\n", encoding="utf-8")
    vorfaelle = modul.incidents_from_transcript(str(transcript), session="s2")
    assert len(vorfaelle) == 1, "nur der als Fehler markierte Aufruf"
    assert vorfaelle[0]["tool"] == "Bash"
    assert "No such file" in vorfaelle[0]["error"]
    assert vorfaelle[0]["session"] == "s2"


def _mini_dienst():
    """Ein winziger HTTP-Dienst, der /events mitschreibt (kein Netz nach außen)."""
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    gesehen = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 - stdlib name
            laenge = int(self.headers.get("Content-Length") or 0)
            roh = self.rfile.read(laenge)
            gesehen.append({
                "pfad": self.path,
                "schluessel": self.headers.get("x-nexus-service-key"),
                "koerper": json.loads(roh or b"[]"),
            })
            antwort = json.dumps({"stored": len(gesehen[-1]["koerper"]),
                                  "received": len(gesehen[-1]["koerper"])}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(antwort)))
            self.end_headers()
            self.wfile.write(antwort)

        def log_message(self, *args):  # still
            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, gesehen


def test_hermes_posts_to_events(monkeypatch) -> None:
    modul = _lade(HERMES_PLUGIN / "incident_report.py", "hermes_incident_report_http")
    server, gesehen = _mini_dienst()
    try:
        port = server.server_address[1]
        gesendet = modul.report_incidents(
            [{"tool": "terminal", "ok": False, "error": "boom", "hint": "", "session": "s"}],
            base=f"http://127.0.0.1:{port}", key="test-key")
    finally:
        server.shutdown()
    assert gesendet == 1
    assert gesehen[0]["pfad"] == "/events"
    assert gesehen[0]["schluessel"] == "test-key"
    assert gesehen[0]["koerper"][0]["tool"] == "terminal"


def test_claude_code_posts_to_events(monkeypatch) -> None:
    modul = _lade(CLAUDE_CODE / "scripts" / "report_incidents.py", "cc_incidents_http")
    server, gesehen = _mini_dienst()
    try:
        port = server.server_address[1]
        gesendet = modul.report(
            [{"tool": "Bash", "ok": False, "error": "exit 1", "hint": "", "session": "s"}],
            base=f"http://127.0.0.1:{port}", key="cc-key")
    finally:
        server.shutdown()
    assert gesendet == 1
    assert gesehen[0]["pfad"] == "/events"
    assert gesehen[0]["schluessel"] == "cc-key"
