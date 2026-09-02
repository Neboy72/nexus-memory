"""Memory Dynamics — gehirn-inspirierte Ranking-Dynamik für Nexus Memory.

Konzept (Nebo/Kiosha 03.09.2026):
- F1 Reinforcement: recall erhöht use_count + last_accessed (via Qdrant set_payload)
- F2 Salience: float 0.0-1.0, hohe Werte sind immun gegen Decay
- F3 Decay: zustandslos berechnetes Ranking-Gewicht, nie Löschung

Design-Prinzipien:
- Zustandslos im Retrieval: Decay wird berechnet, nicht gespeichert (kein Cron)
- Backward-compatible: fehlende Felder = Defaults (use_count=0, salience=0.5)
- Nie zerstören: Decay senkt nur Ranking-Gewicht, Daten bleiben intakt
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Dict, Optional

# Defaults für alte Memories ohne Felder
DEFAULT_USE_COUNT = 0
DEFAULT_SALIENCE = 0.5

# Decay-Konstanten
DECAY_PER_MONTH = 0.05      # 5% Gewichtsverlust pro Monat Nichtnutzung
DECAY_FLOOR = 0.3           # nie tiefer als 30% (vergessen != gelöscht)
SALIENCE_IMMUNE = 0.8       # ab hier immun gegen Decay
REINFORCE_LOG_CAP = 3.0     # log-Verstärkung gedeckelt bei e^3 ≈ 20x Nutzung

# Kategorie-Gruppen für Salience-Defaults (Store-Pfade MCP + Hermes-Plugin)
SALIENT_CATEGORIES = ("rule", "procedure")
EPHEMERAL_CATEGORIES = ("temp", "session")


def parse_ts(value: Any) -> datetime | None:
    """Robustes ISO-Timestamp-Parsing (Qdrant-Payloads variieren)."""
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        s = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def months_since(value: Any, now: datetime | None = None) -> float:
    """Monate seit `value`. Monat = 30 Tage (bewusste Näherung, konstant
    statt kalendarisch — 365 Tage ergeben 12.17 Monate, der Decay ist
    dadurch minimal schneller; für Ranking-Zwecke ausreichend präzise)."""
    dt = parse_ts(value)
    if dt is None:
        return 0.0
    now = now or datetime.now(timezone.utc)
    return max(0.0, (now - dt).total_seconds() / (30 * 24 * 3600))


def decay_factor(payload: Dict[str, Any], now: datetime | None = None) -> float:
    """Ranking-Gewicht durch Verfall: 1.0 (frisch) → 0.3 (vergessen).
    Verlauf ist LINEAR (5 %-Punkte pro Monat, Floor nach 14 Monaten exakt erreicht) —
    bewusst vorhersagbar gewählt, kein exponentieller Verlauf.
    Immun wenn salience >= SALIENCE_IMMUNE."""
    salience = payload.get("salience", DEFAULT_SALIENCE)
    try:
        salience = min(1.0, max(0.0, float(salience)))
    except (TypeError, ValueError):
        salience = DEFAULT_SALIENCE
    if salience >= SALIENCE_IMMUNE:
        return 1.0
    m = months_since(payload.get("last_accessed") or payload.get("created_at"), now)
    return max(DECAY_FLOOR, 1.0 - DECAY_PER_MONTH * m)


def reinforcement_factor(payload: Dict[str, Any]) -> float:
    """Ranking-Boost durch Nutzung: 1.0 (ungenuzt) → bis (1+3) = 4.0."""
    try:
        use = max(0, int(payload.get("use_count", DEFAULT_USE_COUNT)))
    except (TypeError, ValueError):
        use = DEFAULT_USE_COUNT
    return 1.0 + min(REINFORCE_LOG_CAP, 0.0 if use == 0 else math.log(1 + use))


def effective_score(base_score: float, payload: Dict[str, Any],
                    now: datetime | None = None) -> float:
    """Finaler Ranking-Score = base * reinforcement * decay."""
    return base_score * reinforcement_factor(payload) * decay_factor(payload, now)


def access_update_payload(payload: Dict[str, Any],
                          now: datetime | str | None = None) -> Dict[str, Any]:
    """Payload-Update nach einem recall-Treffer (F1).

    Robust gegenüber String-Timestamps (inkl. Z-Suffix, Review-Fix):
    parse_ts normalisiert; ungültige Eingabe fällt auf jetzt() zurück.
    """
    now = parse_ts(now) or datetime.now(timezone.utc)
    try:
        use = max(0, int(payload.get("use_count", DEFAULT_USE_COUNT))) + 1
    except (TypeError, ValueError):
        use = DEFAULT_USE_COUNT + 1
    # v0.15 (Review-Fix, CRITICAL): access_count parallel mitpflegen —
    # SICA/Trust-Konsumenten lesen diesen Zähler; MCP- und Plugin-Pfad
    # schreiben jetzt beide Felder konsistent.
    try:
        acc = max(0, int(payload.get("access_count", DEFAULT_USE_COUNT))) + 1
    except (TypeError, ValueError):
        acc = DEFAULT_USE_COUNT + 1
    return {"use_count": use, "access_count": acc, "last_accessed": now.isoformat()}


def is_salient(payload: Dict[str, Any]) -> bool:
    try:
        return float(payload.get("salience", DEFAULT_SALIENCE)) >= SALIENCE_IMMUNE
    except (TypeError, ValueError):
        return False


def default_salience(category: str = "fact") -> float:
    """Kategorie-Default-Salience: Regeln/Prozeduren hoch (decay-immun),
    temp/session niedrig, alles andere mittel."""
    if category in SALIENT_CATEGORIES:
        return 0.8
    if category in EPHEMERAL_CATEGORIES:
        return 0.1
    return DEFAULT_SALIENCE


def normalize_salience(value: Any, category: str = "fact") -> float:
    """Salience auf [0.0, 1.0] klemmen; None/ungültig → Kategorie-Default."""
    if value is None:
        return default_salience(category)
    try:
        return min(1.0, max(0.0, float(value)))
    except (TypeError, ValueError):
        return default_salience(category)