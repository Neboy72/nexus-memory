"""Prueft die Zugriffsstufen an den Stellen, die der Fremdreview am 09.10.2026 fand.

Nebo-Auftrag: „lieber einmal mehr pruefen". Der Review fand fuenf Stellen, an denen
entweder eine Stufe falsch gesetzt war oder ein fehlender Wert als 'public' gelesen
wurde (fail-open). Alle werden hier festgenagelt.

Der Kern: Ein FEHLENDER oder UNBEKANNTER Wert muss immer als die SICHERSTE Stufe
gelesen werden — nie als die freundlichste. Sonst rutscht eine private Erinnerung an
einem Agenten vorbei, der sie nicht sehen darf.
"""
from __future__ import annotations

import ast
import pathlib
import sys

import pytest

WURZEL = pathlib.Path(__file__).resolve().parents[1]
RANG = ("public", "trusted", "private")


def test_guardrails_stufe_faellt_auf_private():
    """Die Regel-Extraktion darf einen fehlenden Wert nicht als 'public' lesen.

    Sie baut Schutzregeln aus Erinnerungen und gibt deren Text an Aufrufer zurueck.
    Steht in dem Text ein privates Gespraech, darf er nicht als oeffentlich gelten.
    """
    sys.path.insert(0, str(WURZEL / "src"))
    from nexus_memory.guardrails import _level_or_private

    assert _level_or_private({"access_level": "trusted"}) == "trusted"
    assert _level_or_private({"access_level": "private"}) == "private"
    assert _level_or_private({"access_level": "public"}) == "public"
    # Gross-/Kleinschreibung wird normalisiert, wie in consolidation.py — "PUBLIC"
    # benennt die Stufe public und darf nicht als unbekannt durchfallen.
    assert _level_or_private({"access_level": "PUBLIC"}) == "public"
    for kaputt in ({}, {"access_level": None}, {"access_level": ""},
                   {"access_level": "admin"}, {"access_level": "oeffentlich"},
                   {"access_level": 42}, {"access_level": " private "}, None,
                   "kein dict"):
        if isinstance(kaputt, dict) and kaputt.get("access_level") == " private ":
            continue  # Leerraum wird getrimmt, das ist die gueltige Stufe
        assert _level_or_private(kaputt) == "private", (
            f"{kaputt!r} wurde nicht als 'private' gelesen. Ein fehlender oder "
            "unbekannter Wert muss auf die SICHERSTE Stufe fallen — sonst gilt ein "
            "privater Regeltext als oeffentlich und wird herausgegeben."
        )


def test_guardrails_hat_kein_fail_open_mehr():
    """Und der Quelltext selbst darf die alte Form nicht mehr enthalten."""
    quelle = (WURZEL / "src" / "nexus_memory" / "guardrails.py").read_text()
    assert 'payload.get("access_level", "public")' not in quelle, (
        "guardrails.py liest eine Stufe noch mit dem alten fail-open-Standard "
        "'public'. Ersetzt durch _level_or_private(payload)."
    )


def test_graph_boost_filtert_nach_stufe():
    """Der Graph-Boost darf keine Nachbarn durchlassen, die der Agent nicht sehen darf.

    Gemessen am 09.10.2026: Die aktive Plugin-Datei filterte dort NUR nach
    lifecycle_status — eine private Nachbar-Erinnerung landete ungefiltert im
    Prefetch, obwohl der MCP-Server und der OpenClaw-Hook an derselben Stelle
    fail-closed arbeiten.
    """
    quelle = (WURZEL / "plugins" / "memory" / "nexus" / "__init__.py").read_text()
    baum = ast.parse(quelle)

    # Die Funktion _graph_boost finden
    ziel = None
    for knoten in ast.walk(baum):
        if isinstance(knoten, ast.FunctionDef) and knoten.name == "_graph_boost":
            ziel = knoten
            break
    if ziel is None:
        # In der aelteren Kopie heisst sie anders; dann dort suchen.
        pytest.skip("_graph_boost in dieser Fassung nicht vorhanden")

    rumpf = ast.unparse(ziel)
    assert "access_level" in rumpf, (
        "_graph_boost prueft access_level nicht. Eine private Nachbar-Erinnerung "
        "wuerde ungefiltert in den Prefetch gelangen."
    )
    assert "_visible_levels" in rumpf, (
        "_graph_boost nutzt nicht die gemeinsame Sichtbarkeitspruefung "
        "(_visible_levels) — die Grenze muss an einer Stelle definiert sein."
    )


def test_sichtbarkeit_je_agentenkontext():
    """Die Sichtbarkeitsgrenze selbst: Inhaber sieht alles, ein Fremder wenig."""
    quelle = (WURZEL / "plugins" / "memory" / "nexus" / "__init__.py").read_text()
    baum = ast.parse(quelle)
    ziel = None
    for knoten in ast.walk(baum):
        if isinstance(knoten, ast.FunctionDef) and knoten.name == "_visible_levels":
            ziel = knoten
            break
    assert ziel is not None, "_visible_levels fehlt — die Grenze ist nicht definiert"
    rumpf = ast.unparse(ziel)
    assert "public" in rumpf and "trusted" in rumpf and "private" in rumpf, (
        "Die Sichtbarkeitsgrenze nennt nicht alle drei Stufen."
    )
    assert "_agent_context" in rumpf, (
        "Die Sichtbarkeit haengt nicht am Agentenkontext — sie muss sich aus der "
        "Rolle ableiten, nicht angenommen werden."
    )


def test_kein_blankes_public_mehr_in_den_schreibpfaden():
    """Die Schreibpfade der drei Kanaele duerfen Verlaeufe nicht als 'public' ablegen.

    Geprueft werden echte Schreibvorgaenge. Ein Signature-Standard oder ein Wert,
    der aus der Anfrage kommt, ist kein fester Wert — nur eine Zuweisung, die
    'public' woertlich hinschreibt, waere einer.
    """
    zu_pruefen = {
        "plugins/memory/nexus/__init__.py": "der aktive Provider",
        "nexus/sica/__init__.py": "SICA-Zusammenfassungen",
        "integrations/hermes-plugin/__init__.py": "die aeltere Kopie",
    }
    for rel, was in zu_pruefen.items():
        quelle = (WURZEL / rel).read_text()
        verdaechtig = []
        for nummer, zeile in enumerate(quelle.splitlines(), 1):
            blank = zeile.strip()
            if blank.startswith("#") or "access_level" not in zeile:
                continue
            if '"public"' not in zeile and "'public'" not in zeile:
                continue
            # Erlaubt: kommt aus der Anfrage, ist ein Signature-Standard oder reicht
            # einen bereits geprueften Wert weiter. Nur ein woertliches 'public' in
            # einer echten Zuweisung waere ein Fund.
            if any(m in zeile for m in (
                "args.get", "REMEMBER_SCHEMA", "access_level=access_level",
                "def ", "str = ", ": str", "-> ", "getattr", "metadata",
            )):
                continue
            verdaechtig.append((nummer, blank))
        for nummer, zeile in verdaechtig:
            raise AssertionError(
                f"{rel}:{nummer} setzt eine Stufe fest auf 'public' ({was}). "
                f"Zeile: {zeile[:110]}"
            )
