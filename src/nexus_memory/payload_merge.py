"""Ein Ueberschreiben: der vorhandene Punkt ist die Basis.

Regeln vom 24.11.2026 (Auftrag "Felder erhalten statt zuruecksetzen"):

* ``old`` ist die Basis. Damit kann kein Feld still auf einen Vorgabewert
  zurueckfallen — auch keines, das erst spaeter dazukommt.
* Ueberschrieben werden nur die Felder, die dieser Schreibvorgang bestimmt:
  ``content``/``text``, ``category``, ``agent``/``instance``, die strengere
  der beiden Zugriffsstufen, ``provenance`` und ``updated_at``.
* ``provenance`` ist die Aussage des **letzten** Schreibers und wird deshalb
  ersetzt — sie wird gelesen (``composite_confidence``,
  ``source_url``, ``health_audit``); friert man sie ein, meldet recall die
  Zuverlaessigkeit des urspruenglichen Schreibers fuer Text, den ein anderer
  geschrieben hat.
* ``provenance_origin`` wird beim ERSTEN Schreiben einmal gesetzt und danach
  nie ueberschrieben: der Ursprung bleibt auch dann sichtbar, wenn er nach
  zwanzig Vorversionen aus dem Trail faellt.
* ``created_at``, ``valid_from``, ``use_count``, ``salience``, ``state_value``,
  ``expiry_policy``, ``edges``, ``scope``, der Lebenszyklus und ``source``
  bleiben.
* Der neue Vorgang steht in ``provenance_trail`` (letzte
  ``PROVENANCE_TRAIL_LIMIT`` Vorversionen); ``provenance_trail_count`` zaehlt
  alle.

Das Modul ist die EINE Stelle fuer diese Regeln: der MCP-Server und die beiden
Plugin-Kopien rufen es, statt die Logik zu kopieren.
"""
from __future__ import annotations

from typing import Any, Dict

#: Hoechstzahl der Vorversionen im ``provenance_trail``. Jedes Ueberschreiben
#: laesst einen Eintrag zurueck; ohne Deckel waechst die Payload bei jedem
#: Wiederholungs-Schreiben. Die Gesamtzahl steht in ``provenance_trail_count``.
PROVENANCE_TRAIL_LIMIT = 20

#: Rang der Zugriffsstufen. Fehlend/unbekannt zaehlt wie ``private``.
ACCESS_ORDER = {"public": 0, "trusted": 1, "private": 2}

#: Felder des Punkts, die jeder Schreibvorgang bestimmt (Inhalt + Einordnung).
WRITE_FIELDS = ("id", "category", "agent", "instance")

#: Strukturierte Inhaltsfelder eines ENTITY-Punkts. Sie beschreiben denselben
#: ``content`` wie der neue Text; blieben sie alt, widerspraechen sie ihm
#: (etwa eine neue IP in ``entity_attributes`` neben altem ``content``).
ENTITY_CONTENT_FIELDS = (
    "entity_type", "entity_name", "entity_attributes",
    "entity_evidence", "search_terms",
)


def valid_access_level(level: Any) -> bool:
    """True nur fuer eine bekannte, nicht-leere Stufe."""
    return isinstance(level, str) and level in ACCESS_ORDER


def stricter_access_level(old_level: Any, new_level: Any) -> str:
    """Die strengere der beiden Zugriffsstufen gewinnt.

    Fehlend/unbekannt zaehlt wie ``private`` (fail-closed) — ein
    Ueberschreiben darf einen Punkt nie weiter oeffnen, als er war.
    """
    old_name = old_level if valid_access_level(old_level) else "private"
    new_name = new_level if valid_access_level(new_level) else "private"
    if ACCESS_ORDER[old_name] >= ACCESS_ORDER[new_name]:
        return old_name
    return new_name


def set_provenance_origin(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Den Ursprung einmal festhalten — beim ersten Schreiben.

    Spaeter setzt ihn niemand mehr: ``provenance`` folgt jeweils dem letzten
    Schreiber, ``provenance_origin`` bleibt der erste.
    """
    if "provenance_origin" not in payload and isinstance(payload.get("provenance"), dict):
        payload["provenance_origin"] = dict(payload["provenance"])
    return payload


def _append_trail(merged: Dict[str, Any], old: Dict[str, Any],
                  trail_entry: Dict[str, Any]) -> None:
    """Den neuen Vorgang anhaengen, gedeckelt, mit Gesamtzahl."""
    trail = list(old.get("provenance_trail") or [])
    previous_count = int(old.get("provenance_trail_count") or len(trail) or 0)
    trail.append(trail_entry)
    merged["provenance_trail"] = trail[-PROVENANCE_TRAIL_LIMIT:]
    merged["provenance_trail_count"] = previous_count + 1


def merge_overwritten_payload(written: Dict[str, Any], old: Dict[str, Any], *,
                              now_iso: str,
                              trail_entry: Dict[str, Any]) -> Dict[str, Any]:
    """Payload fuer ein UEBERSCHREIBEN eines ERINNERUNGS-Punkts.

    ``written`` ist die Payload, die der Schreibvorgang gebaut hat; ``old`` der
    vorhandene Punkt. Zurueck kommt ``old`` als Basis mit den Feldern dieses
    Schreibvorgangs. ``trail_entry`` beschreibt den neuen Vorgang und landet im
    ``provenance_trail``.
    """
    merged = dict(old)

    if "content" in written:
        merged["content"] = written["content"]
        # Beide Textfelder tragen denselben Inhalt: ``upsert_named`` spiegelt
        # nur, wenn EINES fehlt — hier liegen beide vor.
        if "text" in merged:
            merged["text"] = written["content"]
    for key in WRITE_FIELDS:
        if key in written:
            merged[key] = written[key]

    merged["access_level"] = stricter_access_level(
        old.get("access_level"), written.get("access_level"))

    # Die Aussage des letzten Schreibers gewinnt; der Ursprung bleibt daneben.
    if "provenance" in written:
        merged["provenance"] = dict(written["provenance"] or {})
    merged["provenance_origin"] = (
        old.get("provenance_origin")
        or old.get("provenance")
        or written.get("provenance")
    )

    merged["updated_at"] = now_iso
    _append_trail(merged, old, trail_entry)
    return merged


def merge_overwritten_entity_payload(written: Dict[str, Any],
                                     old: Dict[str, Any], *,
                                     now_iso: str,
                                     trail_entry: Dict[str, Any]) -> Dict[str, Any]:
    """Wie :func:`merge_overwritten_payload`, plus die Inhaltsfelder einer Entity.

    ``entity_type``/``entity_name``/``entity_attributes``/``entity_evidence``
    und ``search_terms`` beschreiben denselben ``content`` und werden deshalb
    mitgezogen. Ein **leerer** ``search_terms``-Wert ist keine Aktualisierung,
    sondern ein Verlust (``search_variants`` kann scheitern und liefert dann
    "") — dann bleibt der alte stehen.
    """
    merged = merge_overwritten_payload(
        written, old, now_iso=now_iso, trail_entry=trail_entry)
    for key in ENTITY_CONTENT_FIELDS:
        if key in written:
            merged[key] = written[key]
    if not merged.get("search_terms") and old.get("search_terms"):
        merged["search_terms"] = old["search_terms"]
    return merged


def finalize_written_payload(written: Dict[str, Any], old: Dict[str, Any], *,
                             now_iso: str,
                             trail_entry: Dict[str, Any]) -> Dict[str, Any]:
    """Der eine Einstiegspunkt fuer jeden Schreibvorgang einer Erinnerung.

    Ohne ``old`` (neuer Punkt) bleibt ``written`` wie gebaut und bekommt nur
    seinen ``provenance_origin``; mit ``old`` wird zusammengefuehrt.
    """
    if not old:
        return set_provenance_origin(dict(written))
    return merge_overwritten_payload(
        written, old, now_iso=now_iso, trail_entry=trail_entry)


def finalize_written_entity_payload(written: Dict[str, Any],
                                    old: Dict[str, Any], *,
                                    now_iso: str,
                                    trail_entry: Dict[str, Any]) -> Dict[str, Any]:
    """Dasselbe fuer einen ENTITY-Punkt (siehe
    :func:`merge_overwritten_entity_payload`)."""
    if not old:
        return set_provenance_origin(dict(written))
    return merge_overwritten_entity_payload(
        written, old, now_iso=now_iso, trail_entry=trail_entry)
