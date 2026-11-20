"""Fehlgeschlagene Werkzeugaufrufe an Nexus melden (0.22.64).

Das nächtliche Lernen in Nexus hängt an keinem Harness: Agenten melden ihre
Fehlschläge an ``POST /events``, Nexus zählt die Wiederkehr und entscheidet,
was eine Lehre wird. Dieser Baustein ist die Hermes-Seite davon.

Warum nicht Nexus die Sitzungsdatenbank lesen lassen: das war bis 0.22.60 der
Fall und koppelte das Gedächtnis an Hermes. Hier ist die Richtung umgedreht,
der Harness meldet über sich selbst, in seinem eigenen Prozess, am Ende der
Sitzung.

Regeln (dieselben wie in den beiden anderen Plugins):

- Nur was der Harness selbst als Fehlschlag ausweist: ``exit_code != 0`` oder
  ein gesetzter ``error``-Text im Werkzeug-Ergebnis. Kein Raten aus dem Text.
- Gesendet werden Werkzeugname, Fehlertext (gekappt), Hinweis der Umgebung und
  die Sitzungskennung. Kein Gesprächsinhalt.
- Best effort: ein Melder, der einen Turn aufhält oder eine Sitzung kippt, ist
  schlechter als einer, der einmal nichts meldet.

Schalter: ``NEXUS_REPORT_INCIDENTS=0`` schaltet das Melden ab (Standard an).
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

MAX_FEHLERTEXT = 2000
MAX_MELDUNGEN = 50
TIMEOUT_S = 4.0


def _env_an(name: str, standard: bool = True) -> bool:
    wert = os.environ.get(name)
    if wert is None:
        return standard
    return str(wert).strip().lower() not in ("0", "false", "no", "off", "")


def _kuerzen(text: Any, grenze: int) -> str:
    return " ".join(str(text or "").split())[:grenze]


def _werkzeug_ergebnis(content: Any) -> Dict[str, Any]:
    """(fehler, text, hint) — dieselbe Regel wie im Nexus-Schreibpfad."""
    if isinstance(content, (bytes, bytearray)):
        content = content.decode("utf-8", "replace")
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except Exception:
            return {"fehler": False, "text": "", "hint": ""}
    if isinstance(content, list):
        for eintrag in content:
            ergebnis = _werkzeug_ergebnis(json.dumps(eintrag))
            if ergebnis["fehler"]:
                return ergebnis
        return {"fehler": False, "text": "", "hint": ""}
    if not isinstance(content, dict):
        return {"fehler": False, "text": "", "hint": ""}
    hint = str(content.get("hint") or "").strip()
    exit_code = content.get("exit_code")
    fehler = content.get("error")
    if isinstance(exit_code, int) and exit_code != 0:
        return {"fehler": True,
                "text": str(content.get("output") or fehler or ""),
                "hint": hint}
    if isinstance(fehler, str) and fehler.strip():
        return {"fehler": True, "text": str(fehler), "hint": hint}
    if str(content.get("status") or "").lower() == "error":
        return {"fehler": True, "text": str(content.get("output") or ""),
                "hint": hint}
    return {"fehler": False, "text": "", "hint": hint}


def _werkzeug_namen(messages: Iterable[Dict[str, Any]]) -> Dict[str, str]:
    """Kennung → Werkzeugname aus den Aufrufen der Assistenten-Nachrichten."""
    namen: Dict[str, str] = {}
    for nachricht in messages:
        aufrufe = nachricht.get("tool_calls") or []
        if isinstance(aufrufe, str):
            try:
                aufrufe = json.loads(aufrufe)
            except Exception:
                aufrufe = []
        for aufruf in aufrufe if isinstance(aufrufe, list) else []:
            if not isinstance(aufruf, dict):
                continue
            kennung = aufruf.get("id")
            funktion = aufruf.get("function") or {}
            name = funktion.get("name") if isinstance(funktion, dict) else None
            if kennung and name:
                namen[str(kennung)] = str(name)
    return namen


def incidents_from_messages(messages: Iterable[Dict[str, Any]],
                            session: str = "",
                            limit: int = MAX_MELDUNGEN) -> List[Dict[str, Any]]:
    """Baut die Meldezeilen aus einer Sitzung. Reine Funktion, kein Netz."""
    nachrichten = list(messages or [])
    namen = _werkzeug_namen(nachrichten)
    vorfaelle: List[Dict[str, Any]] = []
    for nachricht in nachrichten:
        if not isinstance(nachricht, dict):
            continue
        if str(nachricht.get("role") or "") != "tool":
            continue
        ergebnis = _werkzeug_ergebnis(nachricht.get("content"))
        if not ergebnis["fehler"]:
            continue
        werkzeug = (nachricht.get("name")
                    or nachricht.get("tool_name")
                    or namen.get(str(nachricht.get("tool_call_id") or ""), ""))
        text = _kuerzen(ergebnis["text"], MAX_FEHLERTEXT)
        if not werkzeug or not text:
            continue
        vorfaelle.append({
            "tool": str(werkzeug)[:64],
            "ok": False,
            "error": text,
            "hint": _kuerzen(ergebnis["hint"], 600),
            "session": _kuerzen(session, 128),
        })
        if len(vorfaelle) >= limit:
            break
    return vorfaelle


def _dienst_basis() -> str:
    return (os.environ.get("NEXUS_SERVICE_URL")
            or os.environ.get("NEXUS_SERVE_URL")
            or "http://127.0.0.1:9122").rstrip("/")


def _dienst_schluessel() -> str:
    """Der eigene Agentenschlüssel; ohne ihn wird nicht gemeldet."""
    for kandidat in ("~/.nexus-memory/agent_keys/hermes.key",
                     "~/.nexus-memory/service_auth.key"):
        try:
            wert = Path(kandidat).expanduser().read_text(encoding="utf-8").strip()
            if wert:
                return wert
        except Exception:
            continue
    return ""


def report_incidents(vorfaelle: List[Dict[str, Any]], *,
                     base: Optional[str] = None,
                     key: Optional[str] = None,
                     timeout: float = TIMEOUT_S) -> int:
    """Meldet die Vorfälle. Gibt die Zahl der angenommenen Zeilen zurück.

    Wirft nie: das Sitzungsende darf an einer nicht erreichbaren Meldestelle
    nicht scheitern.
    """
    if not vorfaelle or not _env_an("NEXUS_REPORT_INCIDENTS"):
        return 0
    ziel = (base or _dienst_basis()).rstrip("/")
    schluessel = key if key is not None else _dienst_schluessel()
    if not schluessel:
        return 0
    gesendet = 0
    for start in range(0, len(vorfaelle), 100):
        paket = vorfaelle[start:start + 100]
        daten = json.dumps(paket, ensure_ascii=False).encode("utf-8")
        anfrage = urllib.request.Request(
            f"{ziel}/events", data=daten, method="POST",
            headers={"Content-Type": "application/json",
                     "x-nexus-service-key": schluessel})
        try:
            with urllib.request.urlopen(anfrage, timeout=timeout) as antwort:
                koerper = json.loads(antwort.read().decode("utf-8") or "{}")
            gesendet += int(koerper.get("stored") or 0)
        except Exception:
            # Eine Meldestelle, die schweigt, ist kein Sitzungsfehler.
            break
    return gesendet
