"""Raum-Deckel fuer SCHREIBVorgaenge (13.11.2026, verschaerft 24.11.2026).

Die Regel in einem Satz: kommt ein Lesevorgang aus einem GETEILTEN Raum, ist die
hoechste erlaubte Stufe ``public``. Fuer einen SCHREIBvorgang gilt seit
24.11.2026: verlangt er in einem nachweislich geteilten Raum mehr als
``public``, wird er ABGELEHNT — die Stufe wird nicht mehr still gesenkt. Ist der
Raum nicht geteilt (Einzelchat, eigener Kanal ohne Fremde, Cron, CLI, Subagent),
bleibt alles exakt wie vorher.

Warum die Verschaerfung: bis 0.22.68 schrieb der Store-Weg die gesenkte Stufe
und antwortete trotzdem "Stored: …". Der Aufrufer bekam damit einen
oeffentlicheren Eintrag, als er verlangt hatte, und erfuhr es nicht — ClawHub
las das als Absenkung der Vertraulichkeit. Ein Schreibvorgang, der offener
landet als verlangt, ist eine Offenlegung, die niemand freigegeben hat; deshalb
jetzt Absage mit ``isError: true`` statt stiller Aenderung.

Der Unterschied zwischen Lesen und Schreiben: die Leseregel ist fail-closed
(kein frischer Marker -> ``public``), weil Gezeigtes nicht zurueckholbar ist.
Ein Schreibvorgang lehnt nur bei ``shared === true`` ab — ein unbekannter Raum
darf weder einen privaten Eintrag oeffentlich machen noch ihn grundlos
zurueckweisen.

Geprueft wird (statische Quellpruefung, wie in ``test_wave26_fixes.py``; kein
Qdrant, kein Netz, kein Embedder):

1. ``plugins/openclaw/hooks/pre-tool-gate.ts`` — die Vorab-Erinnerung sucht mit
   der fuer den RAUM aufgeloesten Stufe, nicht mehr mit ``cfg.accessLevel``.
   Ihre Treffer landen in ``blockReason``, also im Klartext.
2. ``plugins/openclaw/tools/store.ts`` lehnt ab, statt zu senken;
   ``hooks/capture.ts`` deckelt weiter die KONFIGURIERTE Stufe (dort gibt es
   keine angeforderte Stufe, also auch nichts abzulehnen).
3. ``plugins/memory/nexus/__init__.py`` — ``nexus_remember`` lehnt eine zu hohe
   Stufe im geteilten Raum ab (gleiche Kennung und Felder wie der TS-Weg), und
   ``sync_turn`` schreibt in einem geteilten Raum gar kein Sitzungsprotokoll
   mehr, statt es auf ``public`` zu senken.
4. Der Lesedeckel und der Capture-Deckel werfen nicht und lehnen nicht ab; beide
   Schreibwege lehnen ab, bevor etwas eingebettet oder geschrieben wird.

Die Tests pruefen Quelltext, weil alle Stellen TS-Hooks bzw. ein Hermes-Plugin
sind: ein echter Aufruf braeuchte Gateway, Qdrant und Embedder. Das Verhalten
des Store-Wegs prueft ``plugins/openclaw/test-store-room-rejection.mjs`` mit
einem echten Aufruf.
"""
from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

PRE_TOOL_GATE = "plugins/openclaw/hooks/pre-tool-gate.ts"
RECALL = "plugins/openclaw/hooks/recall.ts"
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
        block = _slice(src, "// ── Level of THIS turn", "// ── 3. PLAN-ZWANG",
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
        # Die eine Leseregel aus lib/turn-context.ts mit ALLEN fuenf Argumenten:
        # Gruppen-Turn, Raumurteil, Konfiguration, Raum-Freigabe, Besitzer.
        # Faellt eines davon weg, faellt still eine Schutzbedingung aus.
        level_call = _slice(block, "cachedLevel = resolveReadLevel(", "return cachedLevel",
                            rel=PRE_TOOL_GATE)
        for arg in ("isGroupTurn,",
                    "isRoomShared(roomKey),",
                    "cfg.accessLevel,",
                    "isRoomApproved(cfg.privateRooms, roomKey),",
                    "hasKnownOwner(cfg, resolveOwnerStateDir()),"):
            assert arg in level_call, (
                f"die Stufe muss ueber resolveReadLevel(...) aus "
                f"lib/turn-context.ts aufgeloest werden — Argument fehlt: {arg!r}"
            )
        assert "preActionRecall(embedder, qdrantClient, effectiveAccessLevel, recallQuery)" in block, (
            "die aufgeloeste Stufe muss an preActionRecall durchgereicht werden."
        )

    def test_recall_resolves_the_level_with_all_five_arguments(self):
        """Das Gegenstueck in ``hooks/recall.ts``.

        ``pre-tool-gate.ts`` beruft sich ausdruecklich auf dieselbe Regel wie
        ``recall.ts`` — dann muss auch ``recall.ts`` sie mit allen fuenf
        Argumenten aufrufen. Fehlt ``roomApproved`` oder ``ownerKnown``,
        greifen die Defaults der Regel (``false`` bzw. ``true``) und damit
        still eine Schutzbedingung weniger: ein Gruppenraum ohne Freigabe und
        ein unbekannter Besitzer deckeln dann nicht mehr zuverlaessig auf
        ``public``.
        """
        src = _read(RECALL)
        block = _slice(src, "const effectiveAccessLevel = resolveReadLevel(",
                       "// Turn marker for the TOOLS", rel=RECALL)

        # Die eine Leseregel aus lib/turn-context.ts mit ALLEN fuenf Argumenten:
        # Gruppen-Turn, Raumurteil, Konfiguration, Raum-Freigabe, Besitzer.
        # Die Argumentliste wird an den Kommas gelesen — ein fehlendes oder
        # verrutschtes Argument faellt damit auf, und die Meldung nennt das
        # wirklich fehlende, nicht das letzte ohne Komma.
        call = _slice(block, "resolveReadLevel(", ")", rel=RECALL)
        args = [a.strip() for a in call[len("resolveReadLevel("):].split(",") if a.strip()]
        expected = ["groupCapActive",
                    "roomShared",
                    "cfg.accessLevel",
                    "roomApproved",
                    "ownerKnown"]
        missing = [name for name in expected if name not in args]
        assert args == expected, (
            f"recall.ts muss die Stufe ueber resolveReadLevel(...) aus "
            f"lib/turn-context.ts mit allen fuenf Argumenten in dieser "
            f"Reihenfolge aufloesen — gefunden: {args}. "
            + (f"Es fehlt: {', '.join(missing)}. " if missing else "")
            + "Beide letzten Argumente sind Schutzbedingungen: ohne "
              "'roomApproved' greift die Freigabe eines Gruppenraums nicht "
              "(Default false), ohne 'ownerKnown' gilt ein unbekannter "
              "Besitzer als bekannt (Default true). Dann gelten ein "
              "Gruppenraum ohne Freigabe und ein unbekannter Besitzer nicht "
              "mehr als 'public', und private Auszuege werden in einem "
              "geteilten Raum lesbar."
        )


class TestWritePathRejectsInASharedRoom:
    """2. store.ts lehnt ab; capture.ts deckelt weiter (ohne Anfrage)."""

    def test_the_store_tool_rejects_a_requested_level_above_public(self):
        src = _read(STORE)

        assert "const requestedLevel = (params.access_level ?? cfg.accessLevel) as string" in src, (
            "store.ts muss die angeforderte Stufe weiterhin aus dem Aufruf lesen "
            "(dann aber pruefen, nicht ungeprueft schreiben)."
        )

        rule = _slice(src, "// Write-side room rule", "// Scope normalization", rel=STORE)
        assert "toolCallWriteRoomShared(toolCallId)" in rule, (
            "store.ts hat keinen ctx: das Raumurteil kommt aus der Markierung in "
            "lib/turn-context.ts — ueber toolCallWriteRoomShared, gebunden an die "
            "call-id dieses Aufrufs."
        )
        assert "toolAccessLevel(" not in rule, (
            "store.ts darf fuer Schreibvorgaenge nicht toolAccessLevel nutzen: das "
            "liefert ohne frischen Marker 'public' und wuerde einen privaten "
            "Eintrag oeffentlich schreiben, sobald der Turn aelter als die TTL "
            "ist (langer Arbeitsturn, Cron, Werkzeugkette)."
        )
        # Die Absage, nicht die Senkung: isError traegt den Fehlervertrag der
        # Datei, und die Stufe wird unveraendert weitergereicht.
        assert "isError: true" in rule, (
            "der Schreibweg muss in einem geteilten Raum ablehnen (isError: true), "
            "nicht still eine niedrigere Stufe speichern."
        )
        assert "const accessLevel = requestedLevel" in rule, (
            "die angeforderte Stufe darf nicht mehr auf ein Raum-Level "
            "heruntergezogen werden; abgelehnt wird ganz, sonst gilt die Anfrage."
        )
        assert "indexOf(requestedLevel) > levelOrder.indexOf(roomLevel)" not in src, (
            "die stille Absenkung (min(angefordert, Raum)) muss weg sein."
        )
        assert rule.index("toolCallWriteRoomShared(toolCallId)") < rule.index("const accessLevel ="), (
            "das Raumurteil muss vor der Stufe feststehen."
        )

        # Geschrieben wird die angeforderte Stufe — nur wenn der Raum sie zulaesst.
        payload = _slice(src, "const payload = {", "await writer.upsert", rel=STORE)
        assert "access_level: accessLevel," in payload, (
            "der Payload muss die geprüfte Stufe tragen."
        )
        assert "access_level: requestedLevel" not in src, (
            "die ungeprueft angeforderte Stufe darf nicht in den Payload."
        )
        # Die Absage kommt VOR Einbettung und Schreibvorgang: nichts wird
        # vorbereitet, was dann doch nicht gespeichert wird.
        assert src.index("shared-room-write-rejected") < src.index("await embedder.embed("), (
            "die Absage muss vor der Einbettung stehen (kein Schreibvorgang, "
            "nicht einmal Vorarbeit)."
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

    def test_only_a_proven_shared_room_rejects(self):
        """Die Absage haengt an ``roomShared === true`` — nicht am Zweifel."""
        for rel, marker in ((STORE, "toolCallWriteRoomShared(toolCallId)"),
                            (CAPTURE, "roomShared === true")):
            src = _read(rel)
            assert marker in src, f"{rel}: Raumurteil nicht gefunden"

        # capture.ts: ausdruecklich nur bei beobachtetem Fremden.
        assert 'access_level: roomShared === true ? "public" : cfg.accessLevel,' in _read(CAPTURE)
        # store.ts: toolCallWriteRoomShared ist die eine Schreibregel aus
        # lib/turn-context.ts (nur ein nachweislich geteilter Raum lehnt ab).
        turn = _read("plugins/openclaw/lib/turn-context.ts")
        assert "return turn !== null && turn.shared === true" in turn

    def test_a_write_without_a_fresh_marker_is_never_rejected(self):
        """Ohne Marker oder mit altem Marker bleibt der Aufruf unberuehrt.

        Die Gegenprobe zur Leseregel: ``toolAccessLevel`` ist bei fehlendem
        Marker absichtlich fail-closed ("public"). Fuer einen SCHREIBVORGANG
        waere das eine Verschlechterung — der Eintrag des Nutzers wuerde
        oeffentlich, sobald der Turn die TTL ueberschreitet. Ausserdem darf
        Unbekanntes keinen Schreibvorgang zurueckweisen. ``writeRoomShared``
        meldet deshalb nur bei ``shared === true`` einen geteilten Raum und
        veraendert sonst nichts.
        """
        turn = _read("plugins/openclaw/lib/turn-context.ts")
        body = _slice(turn, "export function writeRoomShared(", "/** Test seam", rel="lib/turn-context.ts")

        # (a) kein Marker bzw. alter Marker -> getTurnContext liefert null
        #     -> kein geteilter Raum, also keine Absage.
        assert "const turn = getTurnContext(sessionKey, now)" in body, (
            "writeRoomShared muss denselben Marker lesen wie die Leseregel."
        )
        assert "return turn !== null && turn.shared === true" in body, (
            "ohne (frischen) Marker darf writeRoomShared keinen geteilten Raum "
            "melden — sonst wuerde ein privater Eintrag grundlos abgelehnt."
        )
        assert '"public"' not in body, (
            "writeRoomShared darf keine Stufe zurueckgeben oder setzen; es "
            "beantwortet nur die Frage 'ist der Raum geteilt?'."
        )
        # (b) getTurnContext verwirft alte Marker — damit greift (a) auch fuer
        #     einen Turn, der laenger als die TTL zurueckliegt.
        getter = _slice(turn, "const marker = markers.get(key)", "return marker",
                        rel="lib/turn-context.ts")
        assert "if (!(now - marker.at < TURN_MARKER_TTL_MS)) {" in getter, (
            "ein Marker aelter als TURN_MARKER_TTL_MS muss als fehlend gelten."
        )
        assert "markers.delete(key)" in getter and "return null" in getter, (
            "der abgelaufene Marker muss verworfen werden und als fehlend gelten."
        )
        # (c) unbekanntes Raumurteil (null) meldet ebenfalls keinen geteilten
        #     Raum (shared === true ist die einzige wahre Bedingung).
        assert "turn.shared === true" in body, (
            "nur ein nachweislich geteilter Raum darf ablehnen — Unbekanntes "
            "darf einen privaten Eintrag nicht verhindern."
        )


class TestHermesPluginRejectsInASharedRoom:
    """3. Hermes-Plugin: nexus_remember und sync_turn lehnen im geteilten Raum ab.

    Beide Schreibwege muessen dieselbe Antwort geben wie der OpenClaw-Weg
    (``plugins/openclaw/tools/store.ts``), sonst bekommen Hermes-Nutzer weiter
    die stille Absenkung, die ClawHub beanstandet.
    """

    def test_nexus_remember_rejects_in_a_shared_room(self):
        src = _read(HERMES)
        branch = _slice(src, 'elif tool_name == "nexus_remember":',
                        'elif tool_name == "nexus_forget":', rel=HERMES)

        assert "self._chat_is_shared() is True and _level_above_public(" in branch, (
            "die Absage muss hinter `_chat_is_shared() is True` UND einer "
            "Pruefung der verlangten Stufe stehen — ein Raum, der nicht geteilt "
            "ist, bleibt unveraendert."
        )
        assert "result = _shared_room_rejection(_requested_level)" in branch, (
            "im geteilten Raum muss nexus_remember die Absage zurueckgeben, "
            "nicht eine gesenkte Stufe schreiben."
        )
        assert "access_level=_requested_level" in branch, (
            "sonst (kein geteilter Raum) geht die verlangte Stufe unveraendert "
            "an _upsert."
        )
        assert '_write_level = "public"' not in branch, (
            "die stille Absenkung auf public muss aus nexus_remember weg sein."
        )
        # Abgelehnt heisst: kein Schreibvorgang und keine zweite Ablage. Die
        # Anreicherung darf nur im Schreibzweig stehen, nicht danach fuer beide.
        write_branch = branch[branch.index("else:"):]
        before_else = branch[:branch.index("else:")]
        assert "_upsert(" in write_branch, "der Schreibvorgang gehoert in den else-Zweig."
        assert "_upsert(" not in before_else, (
            "im Absage-Zweig darf _upsert nicht vorkommen — sonst wird trotz "
            "Absage geschrieben."
        )
        assert "_enqueue_entity_extraction(" in write_branch, (
            "die Anreicherung darf nur nach einem Schreibvorgang laufen; nach "
            "einer Absage wuerde sie eine zweite Ablage erzeugen."
        )

    def test_sync_turn_does_not_write_in_a_shared_room(self):
        src = _read(HERMES)
        body = _slice(src, "def sync_turn(", "def _write_loop(", rel=HERMES)

        assert "if self._chat_is_shared() is True:" in body, (
            "der geteilte Raum muss vor dem Einreihen des Sitzungsprotokolls "
            "erkannt werden; sonst wird es mit gesenkter Stufe geschrieben."
        )
        assert '"access_level": "private",' in body, (
            "Turn-Eintraege tragen ausserhalb eines geteilten Raums weiter "
            "private."
        )
        assert '_turn_level = "public"' not in body, (
            "die stille Absenkung des Sitzungsprotokolls auf public muss weg sein."
        )
        assert '"access_level": _turn_level' not in body
        assert "shared-room-write-rejected" in body, (
            "die Absage muss dieselbe Kennung nennen wie der TS-Weg."
        )

    def test_both_harnesses_say_the_same_thing(self):
        """Beide Schreibwege: gleiche Kennung, gleiche Felder, keine Senkung."""
        store = _read(STORE)
        hermes = _read(HERMES)

        for name, src in (("store.ts", store), ("__init__.py", hermes)):
            assert "shared-room-write-rejected" in src, (
                f"{name}: die Kennung fehlt — beide Harnesse muessen dieselbe nennen."
            )
            assert (
                "reason: shared_room" in src
                or '"reason": "shared_room"' in src
            ), f"{name}: das Feld reason: shared_room fehlt."
            assert "max_level" in src and "public" in src, (
                f"{name}: die Obergrenze muss in der Antwort stehen."
            )
            assert "requested_level" in src, (
                f"{name}: die angeforderte Stufe muss in der Antwort stehen."
            )

        # Und keine der beiden Stellen senkt mehr still.
        for name, marker in (("store.ts", "levelOrder.indexOf(requestedLevel) > levelOrder.indexOf(roomLevel)"),
                             ("__init__.py", '_write_level = "public"'),
                             ("__init__.py", '_turn_level = "public"')):
            assert marker not in (store if name == "store.ts" else hermes), (
                f"{name}: stille Absenkung gefunden: {marker!r}"
            )

    def test_shared_room_rejection_names_room_ceiling_and_way_out(self):
        src = _read(HERMES)
        helper = _slice(src, "def _shared_room_rejection(", "def _log_movement(", rel=HERMES)

        assert "shared room" in helper, "die Absage muss den Raum als Grund nennen."
        assert 'max_level: public' in helper, "die Obergrenze muss im Text stehen."
        assert "nothing was written" in helper, "die Absage muss sagen, dass nichts gespeichert wurde."
        assert "write it in your own chat" in helper, "der Ausweg muss genannt sein."
        assert '"status": "rejected"' in helper and '"is_error": True' in helper, (
            "die Absage traegt den Fehlervertrag dieses Plugins."
        )


class TestReadCapsAndCaptureDoNotReject:
    """4. Lesedeckel und Capture-Deckel senken nur; beide Schreibwege lehnen ab."""

    def test_the_read_cap_has_no_block_or_throw(self):
        src = _read(PRE_TOOL_GATE)
        cap = _slice(src, "// Room-capped read level", "recallContext = await preActionRecall",
                     rel=PRE_TOOL_GATE)

        assert "throw" not in cap
        assert "block: true" not in cap
        assert "isError" not in cap

    def test_the_store_rejects_but_the_capture_cap_does_not(self):
        store_rule = _slice(_read(STORE), "// Write-side room rule", "// Scope normalization",
                            rel=STORE)
        capture_cap = _slice(_read(CAPTURE), "// Write-side room cap", "lifecycle_status:",
                             rel=CAPTURE)

        # Der Store-Weg lehnt ab — mit dem Fehlervertrag der Datei, vor jeder
        # Vorarbeit.
        assert "isError: true" in store_rule, "store.ts muss ablehnen, nicht senken."
        assert "throw" not in store_rule, "die Absage ist eine Antwort, kein Wurf."

        # Der Capture-Deckel schreibt die KONFIGURIERTE Stufe gedeckelt; dort
        # gibt es keine angeforderte Stufe, also auch nichts abzulehnen.
        for name, cap in (("capture.ts", capture_cap),):
            assert "throw" not in cap, f"{name}: der Deckel darf nicht werfen."
            assert "isError" not in cap, f"{name}: der Deckel lehnt nichts ab."
            assert "return {" not in cap, f"{name}: der Deckel ist keine Fehlerantwort."

    def test_the_hermes_rejection_is_an_answer_not_an_exception(self):
        """Die Absage im Hermes-Weg ist ein Ergebnis, kein Wurf."""
        src = _read(HERMES)
        helper = _slice(src, "def _shared_room_rejection(", "def _log_movement(", rel=HERMES)

        assert "raise" not in helper, "die Absage darf nicht werfen."
        assert "return {" in helper, "die Absage ist eine Ergebnisantwort."
