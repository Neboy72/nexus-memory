# Bench-Spec: Lost-in-the-Middle (LitM) Adaption

Stand: 13.09.2026 · Autor: Kiosha · Status: Bau läuft (GO erteilt)
Repo: ~/nexus-memory-test (Test-Repo, KEIN Produktions-Code)
Plan: ~/.hermes/plans/memory-adaption-litm.md

## 1. Was gemessen wird

Der Auto-Recall-Block (Prefetch) injiziert Memories als Textblock in den Prompt.
Steht die entscheidende Info in der Block-MITTE, überlesen Modelle sie öfter
("Lost in the Middle"). Dieser Bench misst, wie oft das passiert, und prüft ob
eine Umsortierung des Blocks die Trefferquote erhöht.

## 2. Bench-Design

### 2.1 Testdaten (synthetisch, KEINE echten Daten)

- 45 Sessions, je 8 Fake-Memories (= 360 Injektions-Blöcke)
- Jede Session hat genau 1 "Needle" (gezielte Fakten-Zeile mit eindeutiger ID
  wie N-0042), platziert nach Rotation: Anfang / Mitte / Ende
  (15x je Position, rotiert über Sessions)
- Needles stammen aus 3 Themenbereichen (je 15): Homeinfra, Voice, Projekte —
  gleiche Verteilung über Positionen
- Distraktor-Memories: plausibel klingende Fake-Fakten (Namensschema
  DIST-<n>), thematisch nah aber ohne die Needle-Info
- Fixtures-File: benches/litm/fixtures.jsonl (ein Datensatz pro Session)

### 2.2 Prompt-Format

Jede Frage wird ALLEIN mit dem Injektions-Block gestellt (kein weiterer
System-Kontext), exakt im Plugin-Format:

    [category] score=0.87: Text ...

Block-Kopf wie im Plugin: "Nexus Memory active. Relevant memories are
automatically injected." — danach die Items, Budget 2400 Zeichen
(NEXUS_PREFETCH_CHARS-Default der Produktion).

### 2.3 Modelle (Kette, NIE Trainings-Tier)

- Hauptmessung: glm-5.3-flash:cloud (Produktions-Main, beide Varianten)
- Cross-Check: kimi-k3:cloud (verifier-Modell, nur Stichprobe ~15 Fragen)
- Kosten: ~450 Cloud-Calls Hauptmessung + ~15 Cross-Check, Cent-Bereich,
  Budget-Bremse 2,00 EUR gesamt, Checkpoint-Resume nach jedem Chunk von 10

### 2.4 Metriken

| Metrik | Definition |
|--------|-----------|
| hit | Needle-ID korrekt in Antwort genannt |
| position_hit | Hit-Rate je Platzierung (Anfang/Mitte/Ende) |
| litm_gap | hit(Anfang) minus hit(Mitte) — der Schaden |
| tokens | gemessene Block-Größe je Variante |
| latency | p50/p95 je Variante (Call-Dauer) |

## 3. Varianten

| ID | Name | Anordnung | Änderung |
|----|------|-----------|----------|
| BASE | Baseline | Score-Ordnung wie Produktion (Top-Score zuerst, Graph-Items hinten) | keine |
| B1 | Edge-Placement | beste 2 Items an Block-ANFANG + Block-ENDE, Rest Mitte, Graph-Items bleiben hinten | Item-Reihenfolge im fertigen Block |
| B2 | Summary-Layer | 1-Zeilen-Zusammenfassung je Item als Vorschau, Volltext nur Top-2 | Item-Inhalt |

B1 = reine Umsortierung im _do_prefetch-Output (nur Reihenfolge, keine
Code-Logik im Plugin geändert — Varianten werden im Bench-Harness umsortiert).
B2 = Zusammenfassungen kommen aus den Fixtures (vorgefertigt, keine LLM-Kosten
im Bench), Volltext-Kürzung wie Produktion.

## 4. Ablauf

1. **A — Bench bauen:** fixtures.jsonl erzeugen (haus-betrieb), harness
   (benches/litm/run_bench.py, Claude Code), Baseline-FIXTURES gegenprüfen
2. **B — Messung:** BASE messen (checkpoint alle 10 Fragen), B1 messen,
   wenn litm_gap(BASE) >= 5 Punkte: auch B2 messen; sonst B2 entfallen
3. **C — Auswertung:** results.json + bericht.md, verifier prüft Zahlen
   (Kimi K3), Kiosha fasst für Nebo zusammen

## 5. Erfolgskriterien (HART, für Produktion)

- hit(Mitte) B1 vs BASE: mindestens +10 Prozentpunkte
- hit(gesamt) B1: nicht schlechter als BASE (Toleranz -1 Punkt)
- tokens: B1 gleich BASE (Umsortierung ändert nichts), B2 kleiner
- latency p95: höchstens +10 Prozent
- Unter +5 Punkten = ehrlich als "nicht Produktion reif" melden
  (Lektion Reranking-Bench: +3,2 war zu schwach)

## 6. Rollen

1. haus-betrieb: fixtures.jsonl (45x8 Fake-Memories + Needles + Fragen)
2. Claude Code (coder, DeepSeek-Kette): run_bench.py + Varianten + Checkpoint
3. Kiosha: Messung starten, überwachen, auswerten
4. verifier (Kimi K3): Zahlen gegenprüfen
5. Kiosha: Bericht an Nebo + GO-Frage Produktion

## 7. Kehrregeln

- Nur Fake-Daten (Namensschema N-/DIST-), keine Familien-, Finanz-,
  Nutzerdaten
- NIE Trainings-Tier-Modelle; nur glm-5.3-flash + kimi-k3
- Mac-Mini-Last: Bench-Läufe checkpoint-basiert, bei Nebos Arbeit sofort stoppen
- Produktions-Code (plugins/) wird NUR nach neuer GO-Frage angefasst