"""Raum-Deckel fuer SCHREIBVorgaenge (13.11.2026).

Die Regel in einem Satz: kommt ein Lese- ODER Schreibvorgang aus einem
GETEILTEN Raum, ist die hoechste erlaubte Stufe ``public``. Ist der Raum nicht
geteilt (Einzelchat, eigener Kanal ohne Fremde, Cron, CLI, Subagent), bleibt
alles exakt wie vorher. Es wird NIE ein Schreibvorgang abgelehnt — nur die Stufe
gedeckelt.

Der Unterschied zwischen Lesen und Schreiben: die Leseregel ist fail-closed
(kein frischer Marker -> ``public``), weil Gezeigtes nicht zurueckholbar ist.
Ein Schreibvorgang deckelt nur bei ``shared === true`` — ein unbekannter Raum
darf einen privaten Eintrag nicht oeffentlich machen, sonst veraendert der
Deckel die Ablage des Nutzers statt ihn zu schuetzen.

Vier Stellen, vier Pruefungen (statische Quellpruefung, wie in
``test_wave26_fixes.py``; kein Qdrant, kein Netz, kein Embedder):

1. ``plugins/openclaw/hooks/pre-tool-gate.ts`` — die Vorab-Erinnerung sucht mit
   der fuer den RAUM aufgeloesten Stufe, nicht mehr mit ``cfg.accessLevel``.
   Ihre Treffer landen in ``blockReason``, also im Klartext.
2. ``plugins/openclaw/tools/store.ts`` und ``hooks/capture.ts`` — die zu
   speichernde Stufe ist durch die Raumstufe gedeckelt.
3. ``plugins/memory/nexus/__init__.py`` — ``nexus_remember`` und ``sync_turn``
   deckeln auf ``public``, wenn ``_chat_is_shared() is True``.
4. Keine dieser Stellen wirft oder lehnt ab: der Deckel-Zweig senkt nur eine
   Stufe (kein ``throw``/``block``/``isError``/``raise``).

Die Tests pruefen Quelltext, weil alle vier Stellen TS-Hooks bzw. ein
Hermes-Plugin sind: ein echter Aufruf braeuchte Gateway, Qdrant und Embedder.
"""
from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

PRE_TOOL_GATE = "plugins/openclaw/hooks/pre-tool-gate.ts"
STORE = "plugins/openclaw/tools/store.ts"
CAPTURE = "plugins/openclaw/hooks/capture.ts"
HERMES = "plugins/memory/nexus/__init__.py"


def _read(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8")


def _slice(src: str, start: str, end: str, *, rel: str = "") -> str:
    """Der Abschnitt von (einschliesslich) ``start`` bis (ausschliesslich) ``end``.

    Schlaegt mit einer lesbaren Meldung fehl, wenn eine der Marken fehlt — dann
    hat jemand die Stelle umbenannt und die Pruefung zeigt ins Leere.
    """
    begin = src.find(start)
    assert begin != -1, f"{rel}: Marke nicht gefunden: {start!r}"
    stop = src.find(end, begin)
    assert stop != -1, f"{rel}: Endmarke nicht gefunden: {end!r} (nach {start!r})"
    return src[begin:stop]


class TestReadPathUsesTheRoomLevel:
    """1. pre-tool-gate.ts: die Vorab-Erinnerung liest mit der Raumstufe."""

    def test_the_search_call_has_no_configured_level_any_more(self):
        src = _read(PRE_TOOL_GATE)
        body = _slice(src, "async function preActionRecall", "// ── Main Hook Handler",
                      rel=PRE_TOOL_GATE)

        assert "qdrantClient.search(queryVector, NEXUS_QUERY_LIMIT, accessLevel)" in body, (
            "preActionRecall muss die uebergebene (raum-aufgeloeste) Stufe an "
            "qdrantClient.search geben — Treffer landen in blockReason, also in "
            "Text, den das Modell wiedergibt."
        )
        assert "cfg.accessLevel" not in body, (
            "preActionRecall darf cfg.accessLevel nicht mehr lesen: in einem "
            "geteilten Raum wuerden so private Auszuege in blockReason landen."
        )

    def test_the_level_is_resolved_with_the_same_rule_as_recall(self):
        src = _read(PRE_TOOL_GATE)
        block = _slice(src, "// Room-capped read level", "// ── 3. PLAN-ZWANG",
                       rel=PRE_TOOL_GATE)

        # Dieselben Helfer und dieselbe Regel wie hooks/recall.ts — kein
        # zweiter Rechenweg, keine neue Funktion mit gleicher Aufgabe.
        assert "roomKeyOf({" in block, (
            "der Raumname muss ueber roomKeyOf(...) aus lib/room-state.ts kommen "
            "(gleiche Schluessel wie in hooks/recall.ts)."
        )
        assert "isRoomShared(" in block, (
            "das Raumurteil muss isRoomShared(...) aus lib/room-state.ts sein."
        )
        assert "resolveReadLevel(isGroupTurn, roomShared, cfg.accessLevel)" in block, (
            "die Stufe muss ueber resolveReadLevel(isGroupTurn, roomShared, "
            "cfg.accessLevel) aus lib/turn-context.ts aufgeloest werden."
        )
        assert "preActionRecall(embedder, qdrantClient, effectiveAccessLevel, recallQuery)" in block, (
            "die aufgeloeste Stufe muss an preActionRecall durchgereicht werden."
        )


class TestWritePathsAreCapped:
    """2. store.ts und capture.ts: die Raumstufe ist die Obergrenze."""

    def test_the_store_tool_caps_the_requested_level(self):
        src = _read(STORE)

        assert "const requestedLevel = (params.access_level ?? cfg.accessLevel) as string" in src, (
            "store.ts muss die angeforderte Stufe weiterhin aus dem Aufruf lesen "
            "(dann aber deckeln, nicht ungeprueft schreiben)."
        )

        cap = _slice(src, "// Write-side room cap", "// Scope normalization", rel=STORE)
        assert "writeAccessLevel(requestedLevel)" in cap, (
            "store.ts hat keinen ctx: die Raumstufe kommt aus der Markierung in "
            "lib/turn-context.ts — aber ueber writeAccessLevel, nicht ueber die "
            "Leseregel."
        )
        assert "toolAccessLevel(" not in cap, (
            "store.ts darf fuer Schreibvorgaenge nicht toolAccessLevel nutzen: das "
            "liefert ohne frischen Marker 'public' und wuerde einen privaten "
            "Eintrag oeffentlich schreiben, sobald der Turn aelter als die TTL "
            "ist (langer Arbeitsturn, Cron, Werkzeugkette)."
        )
        # Die Obergrenze: die angeforderte Stufe wird auf die Raumstufe
        # heruntergezogen, nie darueber gehoben.
        assert "levelOrder.indexOf(requestedLevel) > levelOrder.indexOf(roomLevel)" in cap, (
            "der Deckel muss min(angefordert, Raumstufe) sein — hoechstens die "
            "Raumstufe, nie mehr."
        )
        assert cap.index("writeAccessLevel(requestedLevel)") < cap.index("const accessLevel ="), (
            "die Raumstufe muss VOR dem Schreiben berechnet werden."
        )

        # Geschrieben wird die gedeckelte Stufe — nicht die angeforderte.
        payload = _slice(src, "const payload = {", "await qdrantClient.upsert", rel=STORE)
        assert "access_level: accessLevel," in payload, (
            "der Payload muss die gedeckelte Stufe tragen."
        )
        assert "access_level: requestedLevel" not in src, (
            "die ungeprueft angeforderte Stufe darf nicht mehr in den Payload."
        )

    def test_capture_caps_the_configured_level_in_a_shared_room(self):
        src = _read(CAPTURE)
        cap = _slice(src, "// Write-side room cap", "lifecycle_status:", rel=CAPTURE)

        assert "isRoomShared(" in cap and "roomKeyOf({" in cap, (
            "capture.ts hat ctx: die Raumstufe muss wie in hooks/recall.ts ueber "
            "roomKeyOf(...) + isRoomShared(...) bestimmt werden."
        )
        assert 'access_level: roomShared === true ? "public" : cfg.accessLevel,' in cap, (
            "in einem geteilten Raum ist public die Obergrenze; sonst bleibt "
            "cfg.accessLevel exakt wie heute."
        )
        assert cap.index("const roomShared = isRoomShared(") < cap.index("access_level:"), (
            "die Raumstufe muss VOR dem Payload berechnet werden."
        )

    def test_only_a_shared_room_caps(self):
        """Der Deckel haengt an ``roomShared === true`` — nicht am Zweifel."""
        for rel, marker in ((STORE, "writeAccessLevel"),
                            (CAPTURE, "roomShared === true")):
            src = _read(rel)
            assert marker in src, f"{rel}: Deckel nicht gefunden"

        # capture.ts: ausdruecklich nur bei beobachtetem Fremden.
        assert 'access_level: roomShared === true ? "public" : cfg.accessLevel,' in _read(CAPTURE)
        # store.ts: writeAccessLevel ist die eine Schreibregel aus
        # lib/turn-context.ts (nur ein nachweislich geteilter Raum -> public).
        turn = _read("plugins/openclaw/lib/turn-context.ts")
        assert "if (turn.shared !== true) return requested" in turn
        assert 'return "public"' in turn

    def test_write_level_does_not_cap_without_a_fresh_marker(self):
        """Ohne Marker oder mit altem Marker bleibt die angeforderte Stufe.

        Die Gegenprobe zur Leseregel: ``toolAccessLevel`` ist bei fehlendem
        Marker absichtlich fail-closed ("public"). Fuer einen SCHREIBVORGANG
        waere das eine Verschlechterung — der Eintrag des Nutzers wuerde
        oeffentlich, sobald der Turn die TTL ueberschreitet. ``writeAccessLevel``
        darf deshalb nur bei ``shared === true`` deckeln.
        """
        turn = _read("plugins/openclaw/lib/turn-context.ts")
        body = _slice(turn, "export function writeAccessLevel(", "/** Test seam", rel="lib/turn-context.ts")

        # (a) kein Marker bzw. alter Marker -> getTurnContext liefert null
        #     -> die angeforderte Stufe kommt unveraendert zurueck.
        assert "const turn = getTurnContext(now)" in body, (
            "writeAccessLevel muss denselben Marker lesen wie die Leseregel."
        )
        assert "if (turn === null) return requested" in body, (
            "ohne (frischen) Marker darf writeAccessLevel NICHT deckeln."
        )
        # (b) getTurnContext verwirft alte Marker — damit greift (a) auch fuer
        #     einen Turn, der laenger als die TTL zurueckliegt.
        assert "if (!(now - marker.at < TURN_MARKER_TTL_MS)) return null" in turn, (
            "ein Marker aelter als TURN_MARKER_TTL_MS muss als fehlend gelten."
        )
        # (c) unbekanntes Raumurteil (null) deckelt ebenfalls nicht.
        assert "if (turn.shared !== true) return requested" in body, (
            "nur ein nachweislich geteilter Raum darf deckeln — Unbekanntes "
            "darf einen privaten Eintrag nicht oeffentlich machen."
        )
        # (d) und der Deckel selbst ist genau "public".
        assert 'return "public"' in body, (
            "bei shared === true ist public die Obergrenze."
        )


class TestHermesPluginCapsWrites:
    """3. Hermes-Plugin: nexus_remember und sync_turn deckeln auf public."""

    def test_nexus_remember_is_capped_in_a_shared_room(self):
        src = _read(HERMES)
        branch = _slice(src, 'elif tool_name == "nexus_remember":',
                        'elif tool_name == "nexus_forget":', rel=HERMES)

        assert 'self._chat_is_shared() is True' in branch, (
            "der Deckel muss hinter `_chat_is_shared() is True` stehen — ein "
            "Raum, der nicht geteilt ist, bleibt unveraendert."
        )
        assert '_write_level = "public"' in branch, (
            "in einem geteilten Raum ist public die Obergrenze."
        )
        assert "access_level=_write_level" in branch, (
            "die gedeckelte Stufe muss an _upsert gehen."
        )
        assert 'access_level=args.get("access_level", "private")' not in branch, (
            "die angeforderte Stufe darf nicht mehr ungedeckelt durchgereicht werden."
        )

    def test_sync_turn_is_capped_in_a_shared_room(self):
        src = _read(HERMES)
        body = _slice(src, "def sync_turn(", "def _write_loop(", rel=HERMES)

        assert '_turn_level = "public" if self._chat_is_shared() is True else "private"' in body, (
            "Turn-Eintraege muessen in einem geteilten Raum public sein und sonst "
            "private wie bisher."
        )
        assert '"access_level": _turn_level,' in body, (
            "der Warteschlangen-Eintrag muss die gedeckelte Stufe tragen."
        )
        assert '"access_level": "private",' not in body, (
            "der feste private-Wert in sync_turn ist der ungedeckelte alte Pfad."
        )


class TestNothingIsRejected:
    """4. Der Deckel senkt nur eine Stufe — er wirft nicht und lehnt nicht ab."""

    def test_the_read_cap_has_no_block_or_throw(self):
        src = _read(PRE_TOOL_GATE)
        cap = _slice(src, "// Room-capped read level", "recallContext = await preActionRecall",
                     rel=PRE_TOOL_GATE)

        assert "throw" not in cap
        assert "block: true" not in cap
        assert "isError" not in cap

    def test_the_write_caps_have_no_rejection(self):
        store_cap = _slice(_read(STORE), "// Write-side room cap", "// Scope normalization",
                           rel=STORE)
        capture_cap = _slice(_read(CAPTURE), "// Write-side room cap", "lifecycle_status:",
                             rel=CAPTURE)

        for name, cap in (("store.ts", store_cap), ("capture.ts", capture_cap)):
            assert "throw" not in cap, f"{name}: der Deckel darf nicht werfen."
            assert "isError" not in cap, f"{name}: der Deckel darf nichts ablehnen."
            assert "return {" not in cap, f"{name}: der Deckel ist keine Fehlerantwort."

    def test_the_hermes_caps_have_no_raise_or_error(self):
        src = _read(HERMES)
        remember = _slice(src, 'elif tool_name == "nexus_remember":',
                          'elif tool_name == "nexus_forget":', rel=HERMES)
        sync = _slice(src, "def sync_turn(", "def _write_loop(", rel=HERMES)
        sync_cap = _slice(sync, "Room cap (13.11.2026)", "_turn_level = ", rel=HERMES)

        for name, chunk in (("nexus_remember", remember), ("sync_turn", sync_cap)):
            assert "raise" not in chunk, f"{name}: der Deckel darf nicht werfen."
            assert '"status": "error"' not in chunk, f"{name}: der Deckel lehnt nichts ab."
            assert "return {" not in chunk, f"{name}: der Deckel ist keine Fehlerantwort."
