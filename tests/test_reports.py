"""Melden statt Ueberschreiben (15.11.2026).

Ein Agent, der einen fremden Eintrag fuer falsch haelt, meldet ihn — er
ueberschreibt ihn nicht. Die Meldung haengt am Eintrag (Payload-Feld
``reports``), jeder Leser sieht sie als Warnzeile, entschieden wird beim
Verwalter (``accept``/``reject``). Der Inhalt des Punktes bleibt in jedem
Schritt unberuehrt.

Rollen: ``know`` darf melden und lesen; ``list_reports``/``resolve_report``
sind Arbeit am Bestand und verlangen ``work``.
"""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import nexus_memory.mcp_server as mcp
from nexus_memory import service_log

ZIEL = "3f8f5c2e-6a1b-4c7d-9e2f-1a2b3c4d5e6f"
INHALT = "Der Verteiler steht im Flur"
GRUND = "Der Verteiler steht nicht im Flur, sondern im Keller."


# ===========================================================================
# Hilfen
# ===========================================================================

class _FakeQdrant:
    """Nur die Qdrant-Flaeche, die report/resolve/recall anfassen."""

    def __init__(self) -> None:
        self.payloads: dict[str, dict] = {}

    def retrieve(self, ids=None, **_kwargs):
        return [
            SimpleNamespace(id=i, payload=copy.deepcopy(self.payloads[i]))
            for i in (ids or []) if i in self.payloads
        ]

    def set_payload(self, payload=None, points=None, **_kwargs):
        ids = getattr(points, "points", points) or []
        if isinstance(ids, str):
            ids = [ids]
        for pid in ids:
            self.payloads.setdefault(str(pid), {}).update(
                copy.deepcopy(payload or {}))
        return SimpleNamespace(status="completed")

    def query_points(self, query=None, limit=10, **_kwargs):
        points = [
            SimpleNamespace(id=pid, payload=copy.deepcopy(pl), score=0.9)
            for pid, pl in self.payloads.items()
        ]
        return SimpleNamespace(points=points[:limit])

    def scroll(self, **_kwargs):
        points = [
            SimpleNamespace(id=pid, payload=copy.deepcopy(pl))
            for pid, pl in self.payloads.items()
        ]
        return points, None


@pytest.fixture
def store(monkeypatch):
    """Ein ``MemoryStore`` mit Attrappen — kein Netz, kein echter Bestand."""
    memory_store = object.__new__(mcp.MemoryStore)
    memory_store.vector_name = None
    memory_store._hybrid_retriever = None
    memory_store._skill_graph = None
    memory_store._scope_centroids = None
    memory_store._update_check_result = None
    memory_store._update_nudged_at = 0.0
    memory_store.client = _FakeQdrant()

    async def _embed(_text, is_query=True):
        return [0.1] * 4

    memory_store._embed = _embed
    monkeypatch.setattr(mcp, "get_store", lambda: memory_store)
    monkeypatch.setattr(mcp, "_index_written_point", lambda _payload: None)

    async def _no_dispatch(*_args, **_kwargs):
        return None

    monkeypatch.setattr(mcp, "dispatch_event", _no_dispatch)
    return memory_store


def _body(out) -> dict:
    return json.loads(out[0].text)


def _put(store, mid: str = ZIEL, **extra) -> None:
    payload = {
        "id": mid,
        "content": INHALT,
        "category": "fact",
        "access_level": "public",
        "agent": "hermes",
        "scope": "default",
    }
    payload.update(extra)
    store.client.payloads[mid] = payload


def _open_report(by: str = "probe-bot", reason: str = GRUND) -> dict:
    return {"by": by, "at": "2026-11-15T10:00:00+00:00",
            "reason": reason, "status": "open"}


def _log_entries() -> list[dict]:
    lines: list[str] = []
    for path in sorted(service_log.log_dir().glob("service-*.jsonl")):
        lines.extend(path.read_text(encoding="utf-8").splitlines())
    return [json.loads(line) for line in lines if line.strip()]


# ===========================================================================
# 1. know darf melden — der Inhalt bleibt
# ===========================================================================

async def test_know_meldet_und_inhalt_bleibt(store, caller_as):
    caller_as("probe-bot", "know")
    _put(store)

    body = _body(await mcp.handle_call_tool(
        "report", {"memory_id": ZIEL, "reason": GRUND}))

    assert body["status"] == "reported"
    assert ZIEL in body["message"]

    payload = store.client.payloads[ZIEL]
    assert payload["content"] == INHALT, "der Inhalt darf nicht angefasst werden"
    assert len(payload["reports"]) == 1
    report = payload["reports"][0]
    assert report["by"] == "probe-bot"
    assert report["reason"] == GRUND
    assert report["status"] == "open"
    assert report["at"]

    entry = _log_entries()[-1]
    assert entry["action"] == "report"
    assert entry["result"] == "ok"
    assert entry["target_id"] == ZIEL
    assert GRUND not in json.dumps(entry), "der Meldetext gehoert nicht ins Log"


# ===========================================================================
# 2. Fehlender oder zu kurzer Grund wird abgewiesen
# ===========================================================================

@pytest.mark.parametrize("reason", ["", "   ", "zu kurz"])
async def test_kurzer_grund_wird_abgewiesen(store, caller_as, reason):
    caller_as("probe-bot", "know")
    _put(store)

    body = _body(await mcp.handle_call_tool(
        "report", {"memory_id": ZIEL, "reason": reason}))

    assert body["status"] == "error"
    assert "10" in body["error"]
    assert "reports" not in store.client.payloads[ZIEL]
    assert _log_entries()[-1]["result"] == "refused"


# ===========================================================================
# 3. Zweite Meldung desselben Agenten ersetzt die erste
# ===========================================================================

async def test_zweite_meldung_ersetzt_die_erste(store, caller_as):
    caller_as("probe-bot", "know")
    _put(store)
    zweiter_grund = "Der Verteiler wurde letzte Woche in den Keller versetzt."

    await mcp.handle_call_tool("report", {"memory_id": ZIEL, "reason": GRUND})
    await mcp.handle_call_tool(
        "report", {"memory_id": ZIEL, "reason": zweiter_grund})

    reports = store.client.payloads[ZIEL]["reports"]
    assert len(reports) == 1, "nicht zwei Meldungen desselben Kopfes"
    assert reports[0]["reason"] == zweiter_grund
    assert reports[0]["status"] == "open"


# ===========================================================================
# 4. recall zeigt die Warnzeile mit Kennung und Grund
# ===========================================================================

async def test_recall_zeigt_die_warnzeile(store, caller_as):
    caller_as("probe-bot", "know")
    _put(store)
    await mcp.handle_call_tool("report", {"memory_id": ZIEL, "reason": GRUND})

    body = _body(await mcp.handle_call_tool(
        "recall", {"query": "Verteiler", "filter_level": "public"}))

    assert body["count"] == 1
    text = body["results"][0]["text"]
    assert INHALT in text, "der Inhalt bleibt lesbar"
    assert f"⚠ disputed by probe-bot: {GRUND}" in text


# ===========================================================================
# 5. know darf list_reports/resolve_report NICHT aufrufen
# ===========================================================================

@pytest.mark.parametrize("tool,arguments", [
    ("list_reports", {}),
    ("resolve_report", {"memory_id": ZIEL, "by": "probe-bot",
                        "decision": "reject"}),
])
async def test_know_darf_nicht_entscheiden(store, caller_as, tool, arguments):
    caller_as("probe-bot", "know")

    body = _body(await mcp.handle_call_tool(tool, arguments))

    assert body["status"] == "denied"
    assert body["error"] == f"not permitted: {tool}"
    entry = _log_entries()[-1]
    assert entry["action"] == "denied"
    assert entry["reason"] == f"not permitted: {tool}"


# ===========================================================================
# 6. work darf beides; reject setzt den Status, der Eintrag bleibt
# ===========================================================================

async def test_work_listet_und_verwirft(store, caller_as):
    caller_as("openclaw", "work")
    _put(store, reports=[_open_report()])

    listed = _body(await mcp.handle_call_tool("list_reports", {}))
    assert listed["count"] == 1
    item = listed["reports"][0]
    assert item["memory_id"] == ZIEL
    assert item["by"] == "probe-bot"
    assert item["reason"] == GRUND
    assert INHALT in item["excerpt"]

    body = _body(await mcp.handle_call_tool("resolve_report", {
        "memory_id": ZIEL, "by": "probe-bot", "decision": "reject",
        "note": "Der Verteiler steht wirklich im Flur.",
    }))

    assert body["status"] == "resolved"
    assert body["decision"] == "reject"

    payload = store.client.payloads[ZIEL]
    assert payload["content"] == INHALT
    assert payload["reports"][0]["status"] == "rejected"
    assert payload["reports"][0]["resolved_by"] == "openclaw"
    assert payload["reports"][0]["resolved_at"]
    assert "disputed_accepted" not in payload

    after = _body(await mcp.handle_call_tool("list_reports", {}))
    assert after["count"] == 0

    entry = _log_entries()[-1]
    assert entry["action"] == "resolve_report"
    assert entry["target_id"] == ZIEL
    assert entry["reason"] == "reject"


# ===========================================================================
# 7. accept markiert den Eintrag, loescht ihn nicht
# ===========================================================================

async def test_accept_markiert_und_loescht_nicht(store, caller_as):
    caller_as("openclaw", "work")
    _put(store, reports=[_open_report()])

    body = _body(await mcp.handle_call_tool("resolve_report", {
        "memory_id": ZIEL, "by": "probe-bot", "decision": "accept"}))

    assert body["status"] == "resolved"
    payload = store.client.payloads[ZIEL]
    assert ZIEL in store.client.payloads, "accept loescht nichts"
    assert payload["content"] == INHALT, "accept schreibt den Inhalt nicht um"
    assert payload["disputed_accepted"] is True
    assert payload["dispute_decision"]["by"] == "probe-bot"
    assert payload["dispute_decision"]["decision"] == "accept"
    assert payload["reports"][0]["status"] == "accepted"


# ===========================================================================
# 8. Nach reject erscheint keine offene Warnung mehr
# ===========================================================================

async def test_nach_reject_keine_warnung_mehr(store, caller_as):
    caller_as("openclaw", "work")
    _put(store, reports=[_open_report()])

    await mcp.handle_call_tool("resolve_report", {
        "memory_id": ZIEL, "by": "probe-bot", "decision": "reject"})

    body = _body(await mcp.handle_call_tool(
        "recall", {"query": "Verteiler", "filter_level": "public"}))

    assert body["count"] == 1
    assert "⚠ disputed" not in body["results"][0]["text"]
    assert INHALT in body["results"][0]["text"]
