# LitM-Bench: Bericht (13.09.2026)

Autor: Kiosha · Plan: ~/.hermes/plans/memory-adaption-litm.md · Spec: benches/litm/SPEC.md

## Frage
Fand das Modell die wichtige Info seltener, wenn sie in der Blockmitte stand
(Lost in the Middle)? Lohnt der Umbau des Erinnerungs-Blocks?

## Zahlen (Messung A bis C komplett)

Hauptmessung, 45 Sessions x 8 Eintraege, glm-5.3-flash:cloud:
- base: 45/45 Treffer, anfang 15/15, mitte 15/15, ende 15/15, litm_gap = 0,0
- b1 (Umsortierung): ebenfalls 45/45
- b2 (Vorschau-Layer): ebenfalls 45/45

Staffel-Runs mit groesseren Bloecken (Distraktoren dichter):
- 16 Eintraege (1262 Zeichen): base 100% an allen Positionen
- 24 Eintraege (1861 Zeichen): base 15/15 anfang, 15/15 mitte, 15/15 ende;
  b1 13/15 anfang (2 Fehltreffer NUR in b1)
- 32 Eintraege (2460 Zeichen, reale Blockgroesse): base 15/15 an allen
  Positionen; b1 14/15 anfang

Cross-Check kimi-k3:cloud (15 Fragen Stichprobe, 24 Eintraege): alle Zellen 5/5.

Token: b2 spart nur mit vorhandenen Zusammenfassungen (8-Item-Fixtures:
662 vs 757 Zeichen). Staffel-Fixtures haben keine Zusammenfassungen, dort ist
b2 identisch mit base (ehrlich vermerkt).

## Schluss (nach Spec-Kriterien)

- litm_gap(BASE) = 0,0. Das Erfolgskriterium "hit(mitte) +10 Punkte" ist
  gegen 100% unerreichbar und unnoetig.
- Ursache: GLM-5.3-Flash denkt vor jeder Antwort (Reasoning-Schritt) und liest
  den Block dabei vollstaendig. Kimi-K3 bestaetigt das Muster.
- Entscheidung: Produktion-Umbau B1/B2 NICHT noetig. Plugin unangetastet.

## Aufwand + Kosten

Rund 750 Cloud-Calls gesamt (Hauptmessung 135, Staffeln 270, Cross-Check 30,
Rest Selbsttest/Debug). Cent-Bereich, 2-EUR-Deckel eingehalten. Mac-Mini-Last:
Laufzeiten p95 unter 2 Sekunden je Call, checkpoint-basiert.

## Offen
- Cron-Sortierung (21 aktive, Loeschliste als Vorschlag) aus dem alten Handoff.
