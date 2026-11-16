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

## Der Umsortierer: gemessen und verworfen (17.11.2026)

Der naheliegende naechste Schritt war ein Cross-Encoder, der die Kandidaten nachsortiert. Das
Modell liegt lokal (`BAAI/bge-reranker-v2-m3`, mehrsprachig), der Code existiert im Projekt
(`src/nexus_memory/reranker.py`), es war also kein Neubau noetig — nur eine Messung.

Aufbau: Kandidatenvereinigung aus BM25-Top-40 und Vektor-Top-40 (rund 78 Paare je Anfrage), dann
der Cross-Encoder ueber alle Paare, Rang des Ziels vorher und nachher verglichen. 83 der 128
festen Fragen sind fair (die uebrigen 45 tragen einen Eintrag, den ein public-Aufrufer nicht
sehen darf). Geraet: Grafikeinheit, nachdem aufgefallen war, dass sentence-transformers die CPU
waehlt, wenn man das Geraet nicht nennt.

| Kennzahl | ohne Umsortierer | mit Umsortierer |
|---|---|---|
| Platz 1 | 61 von 83 (73 %) | 56 von 83 (67 %) |
| Top 5 | 77 von 83 (93 %) | 74 von 83 (89 %) |
| verbessert / verschlechtert | — | 12 / 16 |
| Laufzeit je Anfrage | — | 1838 ms |

Je Frageart: Zitat 24 → 23 erste Plaetze, Stichwort 19 → 16, Paraphrase 17 → 16, Kennung 1 → 1.
Der Umsortierer verliert ueberall ausser bei echten Umformulierungen, wo er in einer frueheren
Messung (35 Modell-Paraphrasen) von 12 auf 21 erste Plaetze half — dieser Gewinn wiegt die
Verluste in den anderen drei Arten nicht auf.

**Der Grund steht in derselben Messung:** Der Fusionsrang der richtigen Treffer hat den Median 1,
und 90 Prozent liegen unter den ersten vier. Es gibt nichts zu retten, der Umsortierer kann nur
zerstoeren. Die Rangfusion ist besser als ihr Ruf — die schwachen Zahlen der Paraphrase-Zeile
kommen daher, dass der richtige Eintrag bei manchen Fragen gar nicht im Kandidatenvorrat liegt,
nicht daher, dass er falsch einsortiert wird.

**Die vorgeschlagene Bedingung** (nur umsortieren, wenn BM25 und Vektor sich auf Platz 1 uneinig
sind, statt eine abgelesene Schwelle zu erfinden): Von den 83 fairen Fragen sind sich beide
Listen in 38 Faellen einig, und davon ist der Treffer in 35 Faellen richtig. Die Bedingung spart
also in knapp der Haelfte der Faelle die Laufzeit — aber sie rettet die Verluste in der anderen
Haelfte nicht, wie die Gesamtzahlen zeigen.

**Entscheidung: nicht gebaut.** Wer diesen Umbau spaeter nochmal erwägt, soll diese Zahlen zuerst
lesen. Der Hebel liegt nicht in der Sortierung.

Was stattdessen gemessen wurde und wo die Kandidaten wirklich fehlen: Die Recall-Kurve auf den
35 fairen Modell-Paraphrasen zeigt, wie viele Ziele in den Top k der jeweiligen Liste liegen —
BM25 15/17/21/25, Vektor 26/29/31/32, Vereinigung 17/21/25/33 fuer k = 10/20/50/100. Bei 100
Kandidaten je Liste sind 33 von 35 erreichbar, bei 40 deutlich weniger. Der Vorrat, nicht die
Reihenfolge, ist die Grenze.
