"""Wer darf welches Werkzeug — und wer darf fremde Eintraege anfassen.

Warum (15.11.2026): Bis hierher durfte jeder Aufrufer jedes Werkzeug. Ein
Harness, der nur lesen und schreiben soll, konnte damit ein Backup fahren, die
Wiederherstellung starten oder einen Guardrail-Override mit Audit-Spur
schreiben. Drei Stufen ersetzen das „darf alles":

``know``
    Lesen und schreiben: ``recall``, ``remember``, ``health``,
    ``guardrail_check``, ``fact_history``, ``report`` — und ``forget``/
    ``update``, aber nur an EIGENEN Eintraegen.

``work``
    Wie ``know``, zusaetzlich die Graph-Werkzeuge, ``list_reports``/
    ``resolve_report`` und ``forget``/``update`` an fremden Eintraegen. Das ist
    die Stufe eines arbeitenden Agenten: er raeumt auch auf, was ein anderer
    Kollege falsch hinterlassen hat, und entscheidet ueber Meldungen.

``admin``
    Wie ``work``, zusaetzlich Verwaltung: Webhooks (``subscribe``,
    ``unsubscribe``, ``list_subscriptions``), ``backup``/``restore``,
    ``guardrail_override`` und die Kostenauskunft.

Zwei Dinge muessen zusammen gelten, sonst ist die Tabelle eine Empfehlung:

1. ``tools/list`` liefert nur die erlaubten Werkzeuge (der Aufrufer sieht
   nicht, was er nicht darf).
2. Der Aufruf wird serverseitig abgewiesen, auch wenn der Name bekannt ist —
   ein selbstgebauter Client fragt ``tools/list`` nicht.

Eigentum (``is_owner``): ``know`` braucht einen Eintrag, dessen Feld ``agent``
die Kennung des Aufrufers traegt. FEHLT das Feld (Altbestand), gilt der
Eintrag als NICHT eigen — eine Ausnahme fuer Altdaten wuerde die Regel wertlos
machen, weil jedes Aufraeumen zuerst die aeltesten Eintraege trifft. ``work``
und ``admin`` duerfen fremde Eintraege anfassen: ein arbeitender Agent raeumt
auch auf, was ein Kollege falsch hinterlassen hat (Auftrag, Punkt 2 Tabelle).
"""
from __future__ import annotations

from typing import Any, Optional

from nexus_memory.agent_detect import (
    AGENT_ROLES,
    DEFAULT_AGENT_ROLE,
    ROLE_ADMIN,
    ROLE_KNOW,
    ROLE_WORK,
)

#: Mindestrolle je Werkzeug. Vollstaendig absichtlich: ein Werkzeug, das hier
#: NICHT steht, ist nur fuer ``admin`` erlaubt — ein neues Werkzeug ist damit
#: automatisch zu, bis jemand es bewusst eintraegt.
TOOL_MIN_ROLE = {
    # know: lesen, schreiben, nachsehen, pruefen
    "recall": ROLE_KNOW,
    "remember": ROLE_KNOW,
    "health": ROLE_KNOW,
    "guardrail_check": ROLE_KNOW,
    "fact_history": ROLE_KNOW,
    # Melden statt Ueberschreiben (15.11.2026): wer lesen darf, darf seinen
    # Zweifel an einem Eintrag anhaengen — er aendert ihn dabei nicht.
    "report": ROLE_KNOW,
    # forget/update sind fuer alle drei Stufen offen; WELCHE Eintraege,
    # entscheidet is_owner() — nicht diese Tabelle.
    "forget": ROLE_KNOW,
    "update": ROLE_KNOW,
    # work: Graph und fremde Eintraege
    "graph_traverse": ROLE_WORK,
    "find_entities": ROLE_WORK,
    "get_subgraph": ROLE_WORK,
    "get_related": ROLE_WORK,
    # Ueber Meldungen entscheiden ist Arbeit am Bestand, keine Verwaltung.
    "list_reports": ROLE_WORK,
    "resolve_report": ROLE_WORK,
    # admin: Verwaltung
    "subscribe": ROLE_ADMIN,
    "unsubscribe": ROLE_ADMIN,
    "list_subscriptions": ROLE_ADMIN,
    "backup": ROLE_ADMIN,
    "restore": ROLE_ADMIN,
    "guardrail_override": ROLE_ADMIN,
    "cost_routing_stats": ROLE_ADMIN,
    "cost_routing_explain": ROLE_ADMIN,
}

#: Rang der Stufen — ``work`` schliesst ``know`` ein, ``admin`` beide.
_RANK = {ROLE_KNOW: 0, ROLE_WORK: 1, ROLE_ADMIN: 2}


def normalize_role(value: Any) -> str:
    """Eine bekannte Rolle, sonst die schwaechste (fail-closed)."""
    return value if value in AGENT_ROLES else DEFAULT_AGENT_ROLE


def tool_allowed(role: str, tool: str) -> bool:
    """Darf ``role`` das Werkzeug ``tool`` aufrufen?"""
    needed = TOOL_MIN_ROLE.get(tool)
    if needed is None:
        # Unbekanntes Werkzeug: nur die oberste Stufe. Neue Werkzeuge sind
        # damit zu, bis jemand sie bewusst freigibt.
        return normalize_role(role) == ROLE_ADMIN
    return _RANK[normalize_role(role)] >= _RANK[needed]


def is_owner(payload: Optional[dict], agent_id: str, role: str) -> bool:
    """Darf ``agent_id`` diesen Eintrag aendern oder loeschen?

    ``work`` und ``admin`` immer. ``know`` nur, wenn ``payload["agent"]`` genau
    die Kennung des Aufrufers traegt. Ein FEHLENDES oder leeres Feld ist nicht
    eigen: Altbestand ohne Absender gehoert niemandem, den man hier fragen
    koennte.
    """
    if normalize_role(role) != ROLE_KNOW:
        return True
    if not isinstance(payload, dict):
        return False
    owner = payload.get("agent")
    if not isinstance(owner, str):
        return False
    owner = owner.strip()
    return bool(owner) and owner == (agent_id or "").strip()
