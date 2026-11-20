"""Both harness plugins report failed tool calls to the neutral feed.

Das Träumen hängt seit 0.22.61 an keinem Harness mehr: es liest
``~/.nexus-memory/events/ereignisse.jsonl``, gefüllt über ``POST /events``.
Ein Plugin, das nichts meldet, sieht von außen genauso aus wie eines, das
funktioniert — deshalb hält dieser Test die Meldung in beiden Quellbäumen
fest. Er liest Text, startet nichts und braucht keinen Dienst.
"""
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DSH = REPO / "plugins" / "dsh"
OPENCLAW = REPO / "plugins" / "openclaw"


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
