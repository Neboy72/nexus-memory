# IDEE: Eingebaute Mini-KI für Konsolidierung (Nexus-brain)

Stand: 09.09.2026 — Status: IDEENSAMMLUNG, NICHT gebaut. Wird mit Nebo gemeinsam entschieden.

## Die Vision (Nebos Bild)
Nexus Memory bringt sein eigenes kleines Gehirn mit. Sortieren/fassen funktioniert
KOMPLETT autark — egal ob User Cloud-Keys hat oder nicht. Kein "Mitfahren" bei
fremden Modellen nötig für die Grundfunktion.

## Was die Konsolidierung technisch braucht
- Dubletten erkennen (2 Erinnerungen = gleicher Inhalt?)
- Kurzfassung schreiben (mehrere Zeilen → 1 Zeile)
- Konflikt-Urteil (neue Info ersetzt alte? ergänzt sie?)
→ kurze Texte, klare Aufgabe = EINFACHSTE Kategorie von KI-Aufgaben

## Option A: Bündeln (ggf. mit installer)
+ 100% autark ab Installation, 0 $ Kosten, 0 Datenabfluss, Fables Opt-in-Kritik
  erledigt sich von selbst (nichts geht raus)
+ Einheitliches Erlebnis für ALLE Nutzer ("es funktioniert einfach")
- 2-4 GB Download beim Setup (Installationsgröße wächst deutlich)
- Qualität: schwächer als GPT-6/Fable-Klasse bei Haarspaltereien
  (Folge: konservativer = lässt gelegentlich eine Erinnerung unsortiert,
   KEIN Datenverlust)
- Wartung: wir binden ein konkretes Modell (z.B. Qwen2.5-3B / Llama-3.2-3B /
  Phi-3-mini), das altern mit der Zeit → Update-Pflicht
- RAM: braucht 2-4 GB im Hintergrund wenn Daemon läuft (8GB-Rechner eng,
  16GB okay — MAC MINI M4 16GB = okay, Windows 32GB = okay)

## Option B: Nutzer-Ollama mitfahren (IST HEUTE SCHON SO — fuel_chain Stufe 1)
+ 0 MB extra, 0 $, privat
- Voraussetzung: User muss Ollama HABEN (nicht alle haben das)
- Nebos Punkt: "Wer KI nutzt, hat schon eine" — teils wahr (Claude-Desktop-
  Nutzer ohne Ollama sind real)

## Option C: Hybrid-Stufen (Empfehlung zur Diskussion)
Stufe 1: eingebautes Mini-Modell (Option A) = DEFAULT, autark
Stufe 2: wenn User-Ollama da → das nutzen (oft besser als Mini)
Stufe 3: wenn User Cloud-Keys da + Dashboard-Schalter AN → Cloud-Boost (Cap 5 $)
→ Jeder Nutzer hat GARANTIE auf Sortierung (Stufe 1), Boost optional.

## Offene Fragen fürs Gespräch mit Nebo
1. Ist 2-4 GB Installationsgröße akzeptabel für unser Zielpublikum?
2. Bündeln wir ins Installationspaket (pip size!) oder Nachladen bei Erststart?
3. Welches Modell (Lizenz! Qwen/Llama/Phi haben unterschiedliche)?
4. Mac Mini M4 16GB als Referenz: Mini-Modell + Qdrant + Agent gleichzeitig = RAM-Check
5. Re-Embedding-Story analog (bge-m3 "in Minuten gratis") — gleiche UX fürs Brain?

## Verwandt (schon gebaut)
- fuel_chain.py: Stufenlogik Ollama→OpenRouter→OpenAI/Nous existiert bereits
- NEXUS_FUEL_BUDGET_USD = 5,00 $/Monat Standard (File-Lock, fail-closed)
- Dashboard-Toggles existieren (MCP-Toggle-Muster vom Agent-Karten-UI)
