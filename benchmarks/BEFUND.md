# Messlatte: der Abruf mit vielen Fragen statt einer

Stand 17.11.2026, Nexus 0.22.55. Aufruf:

    env -u PYTHONPATH .venv/bin/python benchmarks/messlatte.py

Das Skript zieht eine Zufallsstichprobe aus dem Suchkorpus, baut daraus vier Arten von Fragen
und prueft ueber den laufenden Dienst, auf welchem Platz der Eintrag zurueckkommt, aus dem die
Frage stammt. Ergebnis als JSON unter `/tmp/messlatte-ergebnis.json`.

## Ergebnis (131 Fragen, 120 zufaellige Eintraege)

| Art | Faelle | Platz 1 | Top 5 | nicht dabei |
|---|---|---|---|---|
| Zitat (Text woertlich) | 40 | 37 (92 %) | 39 | 1 |
| Stichwort (2-4 Woerter) | 40 | 30 (75 %) | 34 | 5 |
| Paraphrase (umformuliert) | 40 | 10 (25 %) | 26 | 7 |
| Kennung (Adresse, Hex, Domain) | 11 | 3 (27 %) | 3 | 5 |
| **gesamt** | **131** | **80 (61 %)** | **102 (78 %)** | **18** |

## Was die Zahlen sagen

**Zitate sind geloest.** 92 Prozent auf Platz 1, nur ein Fall ganz ohne Treffer. Das ist die
Abdeckungsregel aus 0.22.55, und sie haelt ueber vierzig verschiedene Eintraege — nicht nur
ueber den Fall, der sie ausgeloest hat.

**Stichwoerter sind brauchbar.** Drei Viertel auf Platz 1. Die fuenf Fehlschlaege sind kurze
Wortgruppen wie "gibt monitor eine", wo der Eintrag viele aehnliche Konkurrenten hat.

**Paraphrasen sind die offene Flaeche.** Nur ein Viertel auf Platz 1, aber zwei Drittel in den
Top 5. Das ist die semantische Seite: Wer sich vage erinnert und anders formuliert, findet den
Eintrag meist irgendwo in den ersten fuenf, aber selten zuerst. Das ist der naechste Hebel — und
er ist groesser als alles, was in 0.22.50 bis 0.22.55 gebaut wurde.

**Kennungen sind duenn besetzt.** Nur elf Faelle in der Stichprobe tragen eine echte Kennung
(Adresse mit Port, Hex-Gruppe, Domain, Pfad). Drei davon auf Platz 1. Die Zahl ist zu klein fuer
ein Urteil; fuer eine belastbare Aussage braucht es gezielte Faelle statt Zufall.

## Zwei Messfehler, die der erste Lauf hatte

Beide waren in der Messung, nicht im Produkt, und beide sind behoben:

1. **Zurueckgezogene Eintraege wurden mitgeprueft.** Der Abruf blendet sie absichtlich aus. Der
   erste Lauf zaehlte 8 von 40 Zitaten als "nicht dabei", die in Wahrheit absichtlich unsichtbar
   sind. Das Skript schliesst sie jetzt ueber die Liste des Retrievers aus.
2. **"Kennung" war zu weit gefasst.** Der erste Lauf zog jede Zahl, auch "2026" und "1.1.0" — das
   sind Jahre und Versionsnummern, die in tausenden Eintraegen stehen. Jetzt gelten nur Adressen
   mit Port, Hex-Gruppen ab acht Zeichen, Domainnamen und Pfade.

## Datenschutz

Die Stichprobe kommt aus dem Suchkorpus, nicht direkt aus der Sammlung. Punkte mit `doc_id`
(gescannte Rechnungen, Arztbriefe) sind ausgeschlossen, und in Ausgaben erscheinen nur kurze
Textanfragen, keine Dokumentinhalte.

## Was als Naechstes zu messen waere

- Paraphrasen gezielt: 60 bis 100 umformulierte Fragen zu bekannten Eintraegen, bevor dort etwas
  gebaut wird. Sonst misst man den Zufall.
- Kennungen gezielt: zwanzig Faelle mit Adresse, Hex und Domain, von Hand ausgewaehlt.
- Die Messung vor und nach jedem Eingriff wiederholen — mit demselben Startwert, damit die
  Stichprobe dieselbe bleibt.
