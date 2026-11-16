"""Der Inhalt eines Punktes — eine Regel, ein Ort.

``content`` ist das fuehrende Feld der Sammlung. ``text`` bleibt lesbar, weil
628 Punkte (15.11.2026 gemessen) ihren Inhalt nur dort trugen und an jeder
Stelle durchfielen, die ausschliesslich ``content`` las. Liegen beide Felder
vor, gewinnt ``content``.

Kein Import aus ``nexus_memory``: diese Hooks werden mit dem ``python3`` des
Editors gestartet, und das Paket ist dort nicht zwingend installiert. Die
Datei hat deshalb keine Abhaengigkeiten.
"""
from __future__ import annotations

from typing import Any


def point_text(payload: Any) -> str:
    """``content``, sonst ``text``, sonst ``""`` — immer ein ``str``."""
    pl = payload if isinstance(payload, dict) else {}
    value = pl.get("content") or pl.get("text") or ""
    return value if isinstance(value, str) else str(value)
