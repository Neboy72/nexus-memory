# Messlatte: der Abruf mit vielen Fragen statt einer

Stand 17.11.2026, Nexus 0.22.56. Aufruf:

    env -u PYTHONPATH .venv/bin/python benchmarks/messlatte.py

Die Fragen werden EINMAL gezogen und in `messlatte-faelle.json` festgehalten (lokal, nicht im
Repo — sie enthalten Woertliches aus dem Bestand). Danach laeuft jeder Vergleich auf denselben
Fragen, und ein Eingriff ist paarweise messbar. Ohne diesen Schritt war kein Vorher/Nachher
moeglich: die Zahl der Kennungs-Faelle schwankte zwischen 8 und 36.

## Ergebnis (128 feste Fragen)

| Art | Faelle | Platz 1 | Top 5 | nicht dabei |
|---|---|---|---|---|
| Zitat (Text woertlich) | 40 | 38 (95 %) | 38 | 0 |
| Stichwort (2-4 Woerter) | 40 | 31 (78 %) | 35 | 4 |
| Paraphrase (umformuliert) | 40 | 16 (40 %) | 27 | 6 |
| Kennung (Adresse, Hex, Domain) | 8 | 1 (13 %) | 4 | 2 |
| **gesamt** | **128** | **86 (67 %)** | **104 (81 %)** | **12** |

## Was 0.22.56 gebracht hat (dieselben 128 Fragen, mit und ohne Aenderung)

| Art | Platz 1 vorher | Platz 1 nachher | Top 5 vorher | Top 5 nachher |
|---|---|---|---|---|
| Zitat | 37 | 38 | 38 | 38 |
| Stichwort | 29 | 31 | 31 | 36 |
| Paraphrase | 13 | 16 | 22 | 29 |
| Kennung | 1 | 1 | 1 | 6 |
| **gesamt** | **80** | **86** | **92** | **109** |

Nicht gefunden: 20 vorher, 11 nachher. Keine Kategorie schlechter.
(Der Nachher-Lauf nach dem Release ergab 104 statt 109 in den Top 5 — die Differenz ist
Nutzungsdrift zwischen zwei Laeufen, kein Rueckschritt: jeder Abruf zaehlt als Zugriff und
veraendert die Rangfolge leicht.)

## Was die Zahlen sagen

**Zitate sind geloest.** 95 Prozent auf Platz 1, kein Fall ohne Treffer. Das ist die
Abdeckungsregel aus 0.22.55, und sie haelt ueber vierzig verschiedene Eintraege.

**Stichwoerter sind brauchbar.** Drei Viertel auf Platz 1.

**Paraphrasen sind die offene Flaeche.** Ein Viertel bis zwei Fuenftel auf Platz 1, zwei Drittel
in den Top 5. Der Befund der Stufendiagnose: BM25 hat 31 von 40 Paraphrasen auf Platz 1, der
Vektor nur 6 — die Bedeutungssuche ist hier schwaecher als der Wortlaut, und die Fusion zieht das
Ergebnis von 31 auf 18 herunter.

**Kennungen bleiben duenn.** Acht Faelle tragen eine echte Kennung; einer auf Platz 1. Zu wenig
fuer ein Urteil — hier braucht es gezielte Faelle statt Zufall.

## Zwei Verdachte, geprueft und verworfen

Beide waeren teuer geworden, wenn ich sie gebaut haette:

1. **Qwen3-Instruktions-Praefix auf der Frageseite.** Fehlt im sentence-transformers-Zweig, wird
   nur im Ollama-Zweig gesetzt. Gemessen an 25 Faellen: mit Praefix 9 schlechter, 3 besser.
   Also nicht die Ursache.
2. **Die Schluss-Sortierung im Dienst nach Nutzungswert.** Drei Varianten gemessen (Nutzung
   zuerst, Relevanz zuerst, Relevanz mit Nutzungsbonus): identische Zahlen. Also nicht die
   Ursache.

## Drei Messfehler in frueheren Laeufen

Alle drei waren in der Messung, nicht im Produkt:

1. **Zurueckgezogene Eintraege wurden mitgeprueft**, obwohl der Abruf sie absichtlich ausblendet.
   Der erste Lauf zaehlte 8 von 40 Zitaten als "nicht dabei", die unsichtbar sein SOLLEN.
2. **"Kennung" war zu weit gefasst** — jede Zahl, auch "2026" und "1.1.0". Jetzt nur Adressen mit
   Port, Hex ab acht Zeichen, Domains, Pfade.
3. **Die Stichprobe war nicht stabil**, also war kein Vorher/Nachher moeglich. Jetzt festgehalten.

## Womit weiterzumessen waere

- Paraphrasen: `benchmarks/fragen_bauen.py` laesst ein Modell Fragen bauen und weist je Frage den
  Anteil wortgleicher Woerter aus. Sechzig Stueck liegen in `paraphrase-faelle.json` (lokal).
- Kennungen: zwanzig Faelle von Hand, sonst bleibt die Zahl bedeutungslos.
- Vor und nach jedem Eingriff dieselbe Liste — sonst misst man den Zufall.

## Datenschutz

Stichprobe aus dem Suchkorpus, Punkte mit `doc_id` (gescannte Rechnungen, Arztbriefe)
ausgeschlossen. Die erzeugten Falldateien stehen in `.gitignore`, weil sie Woertliches aus dem
privaten Bestand enthalten. In Ausgaben erscheinen nur kurze Textanfragen.

## Paraphrasen mit echten Umformulierungen (17.11.2026)

Die Paraphrase-Zeile der Messlatte war bisher schwach gebaut: "was steht zu <drei Woerter aus dem
Eintrag> im bestand" wiederholt den Wortlaut und misst nichts. `benchmarks/fragen_bauen.py` laesst
deshalb ein Modell je eine Frage formulieren, die dasselbe wissen will, und weist je Frage den
Anteil wortgleicher Woerter aus. 60 Fragen, davon 51 unter 50 Prozent wortgleich.

Ergebnis ueber den Dienst, nach Ausschluss der Faelle, deren Zieleintrag der Aufrufer nicht sehen
darf (25 der 60 tragen einen privaten Eintrag — das ist kein Suchfehler, das ist eine
Zugriffsgrenze):

| Kennzahl | Wert |
|---|---|
| Faelle (fair, public-Ziel) | 35 |
| Platz 1 | 12 (34 %) |
| Top 5 | 22 (63 %) |
| nicht gefunden | 8 (23 %) |

Vor dem Ausschluss sahen dieselben Fragen wie 8 von 60 aus (13 % Platz 1, 55 % nicht gefunden).
Diese Zahl war falsch: Sie mass die Zugriffsgrenze, nicht die Suche. Deshalb prueft die Messlatte
seit diesem Lauf vorab die Payloads der Ziele und schliesst aus, was der Aufrufer nicht sehen darf
— zurueckgezogen, privat, fremder Geltungsbereich.

Damit ist die Paraphrase-Leistung ehrlich beziffert: rund ein Drittel oben, zwei Drittel in den
Top 5, ein Fuenftel gar nicht. Die Stufendiagnose sagt auch warum: Von 40 Paraphrasen hat BM25
31 auf Platz 1, der Vektor nur 6.
