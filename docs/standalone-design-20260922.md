# Design-Blatt: Full Standalone Independence (v0.21-Kandidat)

Stand: 22.09.2026, 20:10 Uhr · Autor: Kiosha · Status: WARTET AUF NEBOS BLICK
Baseline: ae37787 · OpenClaw 26/26 grün · Pytest 1945 passed / 3 skipped / 0 failed

---

## Klartext für Nebo (dieser Abschnitt zählt)

**Was ändert sich für dich: NICHTS.** Der Butler (Auto-Injection) arbeitet genau wie heute.
Nexus bekommt zusätzlich ein eigenes Leben: Er läuft künftig als eigener Dienst im Haus,
startet beim Einschalten von selbst und lebt, auch wenn gerade kein Bot wach ist.

**Die zwei obersten Gesetze (Nebo, 22.09.):**

1. **Beim Plugin ändert sich nichts.** Alles bleibt wie bisher. Es wird höchstens besser.
   Wird auch nur EINE Sache schlechter: Projekt Abbruch. Kein Workaround.
2. **Der alte Install-Weg bleibt funktionieren.** Wer einem Bot sagt "installiere Nexus",
   bekommt denselben funktionierenden Weg wie heute, ohne Standalone-Infrastruktur.

---

## Technischer Plan

### Was heute ist (Fakten aus dem Code)

- Die Plugins (Hermes, OpenClaw) reden DIREKT mit Qdrant (localhost:6333), nicht über den
  MCP-Server. Ihr Kernwert Auto-Injection hängt an Qdrant, nicht am Server.
- Der MCP-Server (`nexus-memory`, 15 Werkzeuge) ist der stdio-Weg für Claude Code, Cursor
  und jeden MCP-Client.
- Qdrant läuft als Docker-Container. Embeddings: Voyage-4 (Fuel über Keys in .env).

### Baustein A: `nexus serve` (HTTP-Dienst)

- Neuer Modus im bestehenden Server: `nexus-memory serve` startet MCP über Streamable HTTP
  statt nur stdio. Port **9122** (geprüft frei; 9119 bis 9121 sind Hermes).
- stdio bleibt unverändert als zweiter Weg. Nichts wird ersetzt.
- Health-Endpoint auf 9122/healthz: meldet Qdrant-Erreichbarkeit, Embedder-Status, Version.

### Baustein B: launchd-Unit (Dienst im Haus)

- `ai.nexus.serve.plist` in ~/Library/LaunchAgents/ (Muster wie ai.hermes.*).
- RunAtLoad + KeepAlive: startet beim Boot, kommt nach Crash selbst zurück.
- Log nach ~/nexus-memory/logs/serve.log (Rotation, max 5 MB).

### Baustein C: Eigener Fuel-Chain (der fetteste Teil)

- Der Consolidation-Daemon läuft künftig aus dem Dienst selbst, nicht mehr aus einem
  Agenten heraus. Multi-Fuel (Ollama → OpenRouter → OpenAI-kompatibel) bleibt logisch
  identisch, nur der Startort ändert sich.
- Der Dienst liest dieselbe .env wie heute. Keine neuen Keys, keine neue Config-Quelle.

### Baustein D: Plugin-Vertrag (kein Umbau!)

- Hermes- und OpenClaw-Plugin bleiben, WIE SIE SIND: direkter Qdrant-Zugriff, unverändert.
- Ihre Konfiguration bekommt höchstens EIN neues optionales Feld (health-check-Anzeige),
  Standard aus. Vom Verhalten her: 1 zu 1, bewiesen durch die Test-Baseline.

### Was NICHT gebaut wird

- Kein Transport-Zwang: MCP-stdio-Install (der Alt-Weg) bleibt erstklassig dokumentiert
  und getestet.
- Keine Qdrant-Migration, keine Collection-Änderung, kein Schema-Bruch.
- Kein Auth-Bau (das ist Roadmap #2, kommt separat danach).

### Beweis-Kette (Abnahme, in dieser Reihenfolge)

1. Plugin-Regression: volle Suite (1945) + OpenClaw (26) grün NACH dem Umbau, plus
   Live-Gegenprobe: Auto-Injection in einem echten Hermes-Turn und OpenClaw-Turn, vorher/
   nachher verglichen. EINE Verschlechterung = Abbruch (Gesetz 1).
2. Alt-Weg: frische Testumgebung, Bot-Sag-Install nach AGENTS.md, funktioniert ohne
   den Dienst. (Gesetz 2)
3. Kill-Test: Dienst stoppen → KeepAlive holt ihn zurück → Health meldet wieder grün.
4. Nebo-Abnahme: "Läuft Nexus, ohne dass Hermes ihn anfasst?" (Ohr-Test)

### Rollback

- Der Dienst ist ein ZUSATZ. Aus = launchd-Unit entladen, alles ist wie heute.
- Vor jedem Schritt: git-Tag + Qdrant-Backup (heilig, wie immer).

### Wer baut

- Claude Code Workers (DeepSeek V4.1 Flash, localhost:11434) bauen Bausteine A bis C
  je mit Quellen-Pflicht (decision-gate bleibt aktiv). Kiosha reviewt jeden Baustein,
  Replika-Container-Gegenprobe vor jedem Push.

### Zeit (Schätzung, keine Zusage)

- A: 1 bis 2 Abende · B: 1 Abend · C: 1 bis 2 Abende · Beweis-Kette: 1 bis 2 Abende.
- Risiko-Meldung kommt aus Baustein C, sobald bekannt, nicht am Ende.