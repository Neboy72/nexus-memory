"""Fail-open im Schreibweg: ohne Raum gilt ``private``, mit unklarem Raum nicht.

ClawHub-Befund 25.11.2026: „possible fail-open memory authorization flaw … when
room observation is missing". Bis 0.22.70 wurde eine zu hohe Stufe nur dann
abgelehnt, wenn der Raum NACHWEISLICH geteilt war; ein Raum mit fehlendem oder
veraltetem Urteil liess alles durch.

Die Regel seither:

* kein Raumkontext (reiner MCP-Aufrufer: DS Bot, Hermes, Claude Code, DSH) →
  ``private`` bleibt erlaubt, der Aufruf wird nicht abgewiesen;
* Raumkontext vorhanden, Urteil ``True`` (geteilt) → Absage ``shared_room``;
* Raumkontext vorhanden, Urteil ``None`` (nicht belegbar) → Absage
  ``room_unknown``;
* Raumkontext vorhanden, Urteil ``False`` (eigener Raum) → der Aufruf steht.

Der Test ruft den ECHTEN ``handle_tool_call``-Zweig mit Attrappen fuer Ablage und
Anreicherung; kein Qdrant, kein Netz, kein Modell.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PLUGIN = REPO / "plugins" / "memory" / "nexus" / "__init__.py"


def _modul():
    spec = importlib.util.spec_from_file_location("hermes_nexus_plugin_failopen", PLUGIN)
    modul = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modul)
    return modul


def _aufruf(shared, signals, level="private"):
    """Ein nexus_remember-Aufruf mit Attrappen; liefert (Antwort, ob geschrieben)."""
    modul = _modul()
    obj = modul.NexusMemoryProvider.__new__(modul.NexusMemoryProvider)
    obj._chat_is_shared = lambda: shared
    obj._turn_signals = signals
    geschrieben = []

    def upsert(**kwargs):
        geschrieben.append(kwargs)
        return {"status": "stored"}

    obj._upsert = upsert
    obj._enqueue_entity_extraction = lambda *a, **k: None
    antwort = json.loads(obj.handle_tool_call(
        "nexus_remember", {"text": "Der Verteiler steht im Flur.", "access_level": level}))
    return antwort, geschrieben


def test_ohne_raum_bleibt_private_erlaubt():
    """Der reine MCP-Aufrufer darf NICHT abgewiesen werden."""
    antwort, geschrieben = _aufruf(None, {})
    assert len(geschrieben) == 1, "ohne Raum muss geschrieben werden"
    assert geschrieben[0]["access_level"] == "private"
    assert antwort.get("status") != "rejected"


def test_ohne_raum_ohne_signale_aber_mit_eigenem_raum():
    antwort, geschrieben = _aufruf(False, {})
    assert len(geschrieben) == 1
    assert antwort.get("status") != "rejected"


def test_unklarer_raum_wird_abgelehnt():
    """Kontext da, Urteil None: genau der Fail-open-Fall."""
    antwort, geschrieben = _aufruf(None, {"chat_id": "chat-1"})
    assert not geschrieben, "bei unklarem Raum darf nichts geschrieben werden"
    assert antwort.get("status") == "rejected"
    assert antwort.get("reason") == "room_unknown"
    assert antwort.get("code") == "shared-room-write-rejected"
    assert antwort.get("max_level") == "public"
    assert "cannot be determined" in antwort.get("error", "")


def test_geteilter_raum_wird_abgelehnt():
    antwort, geschrieben = _aufruf(True, {"chat_id": "chat-1"})
    assert not geschrieben
    assert antwort.get("reason") == "shared_room"
    assert antwort.get("code") == "shared-room-write-rejected"


def test_eigener_raum_geht_durch():
    antwort, geschrieben = _aufruf(False, {"chat_id": "chat-1", "author_id": "owner"})
    assert len(geschrieben) == 1
    assert geschrieben[0]["access_level"] == "private"
    assert antwort.get("status") != "rejected"


def test_public_geht_auch_im_unklaren_raum_durch():
    antwort, geschrieben = _aufruf(None, {"chat_id": "chat-1"}, level="public")
    assert len(geschrieben) == 1
    assert geschrieben[0]["access_level"] == "public"
    assert antwort.get("status") != "rejected"
