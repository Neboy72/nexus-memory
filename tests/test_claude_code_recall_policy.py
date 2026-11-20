"""Die gespiegelte Auswahlregel im Claude-Code-Hook bleibt deckungsgleich.

Der Hook (``plugins/claude-code/scripts/auto_recall.py``) sucht DIREKT in
Qdrant und laeuft unter dem System-Python der App — er kann das Paket
``nexus_memory`` nicht importieren. Die Regel steht dort deshalb ein zweites
Mal. Diese Doppelung ist nur zulaessig, solange sie geprueft wird: derselbe
Fall, dasselbe Ergebnis wie in ``src/nexus_memory/recall_policy.py``.

Faellt hier etwas auseinander, ist einer der beiden Wege falsch — und der
Test sagt, welcher Fall betroffen ist.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from nexus_memory import recall_policy  # noqa: E402

_HOOK = Path(__file__).resolve().parents[1] / "plugins" / "claude-code" / "scripts" / "auto_recall.py"


def _hook():
    spec = importlib.util.spec_from_file_location("auto_recall_unter_test", _HOOK)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


#: Dieselbe Falltabelle wird im Node-Test fuer OpenClaw benutzt.
FAELLE = [
    # (payload, erwartete Kategorie)
    ({"category": "fact"}, "fact"),
    ({"category": "rule"}, "rule"),
    ({"category": "preference"}, "preference"),
    ({"category": "entity"}, "entity"),
    ({"category": "session"}, "session"),
    ({"category": "FACT "}, "fact"),
    ({}, "fact"),
    ({"category": None}, "fact"),
    ({"type": "turn"}, "session"),
    ({"type": "turn", "category": "fact"}, "session"),
    ({"type": "memory", "category": "rule"}, "rule"),
    ({"type": "TURN", "category": "rule"}, "session"),
]


def test_kategorie_ist_in_beiden_fassungen_gleich():
    hook = _hook()
    for payload, erwartet in FAELLE:
        assert recall_policy.effective_category(payload) == erwartet, payload
        assert hook.effective_category(payload) == erwartet, payload


def test_auswahl_ist_in_beiden_fassungen_gleich():
    """Kategorien plus Schwelle, ueber eine Reihe von Punktzahlen."""
    hook = _hook()
    kategorien = ("fact", "rule", "preference")
    for payload, _ in FAELLE:
        for score in (0.0, 0.2, 0.3499, 0.35, 0.8, 1.0):
            kanonisch = recall_policy.passes(
                {"category": payload.get("category"),
                 "type": payload.get("type"),
                 "vector_score": score},
                categories=kategorien,
                min_score=recall_policy.DEFAULT_RECALL_MIN_SCORE,
            )
            gespiegelt = hook.passes_categories(payload, kategorien) and (
                score >= recall_policy.DEFAULT_RECALL_MIN_SCORE)
            assert kanonisch is gespiegelt, (payload, score)


def test_hook_voreinstellungen_sind_die_des_auftrags():
    hook = _hook()
    assert hook.MAX_RESULTS == recall_policy.DEFAULT_RECALL_LIMIT
    assert hook.CATEGORIES == recall_policy.DEFAULT_RECALL_CATEGORIES
    assert hook.MIN_SCORE == recall_policy.DEFAULT_RECALL_MIN_SCORE
    assert hook.MAX_CONTEXT_CHARS == recall_policy.DEFAULT_RECALL_BUDGET_CHARS
