# Nexus Memory — natives Plugin für den DeepSeek Harness

Auto-Recall und Auto-Capture gegen eine **bestehende** Nexus-Memory-Installation.
Das Plugin ist ein Client: es bringt keinen eigenen Store, keine eigenen
Embeddings und keinen zweiten Qdrant mit. Es hängt den Harness an dieselbe
Collection `nexus`, die Hermes, OpenClaw und der MCP-Server schon benutzen.

- Übergabe an den Agenten, der die Nexus-Installation betreut: **[HANDOVER.md](HANDOVER.md)**
- Vertrag, Endpunkte, Abnahmetests: dort, Abschnitte 1 und 5

## Was es tut

**Auto-Recall.** Vor dem ersten Schritt eines Turns geht die letzte menschliche
Nachricht an den Dienst (`recall`, `filter_level` = `accessLevel`). Die besten
Treffer werden als eine dauerhafte, quellenattribuierte User-Nachricht an den
Schritt gehängt:

```
Nexus Memory — 3 relevant entries from earlier sessions:
- [fact · 2026-11-02] …
Use these as background context when relevant; prefer newer evidence when entries disagree.
```

Die Nachricht trägt `source.kind = "nexus-memory"` und steht damit sichtbar im
Session-Log (forkbar, komprimierbar, nachvollziehbar) — keine stille
Prompt-Manipulation.

**Auto-Capture.** Am Ende eines Turns werden die menschliche Nachricht und die
letzte Assistentenantwort als eine episodische Erinnerung
(`category: session`) über `POST /write` zurückgeschrieben — durch dasselbe
Write-Gate wie alle anderen Clients. Subagent-Turns und sehr kurze Turns werden
übersprungen.

Beides ist **fail-open**: Recall hat ein hartes Zeitlimit (Default 4 s), Capture
läuft nach `turn/end` als fire-and-forget. Fällt der Dienst aus, läuft der Turn
normal weiter und im Log steht eine Warnung.

## Transporte

| `transport` | Weg | Voraussetzung |
|---|---|---|
| `http` (Default) | MCP-over-HTTP `/mcp` + `/write` | ein laufender `nexus-memory serve` |
| `stdio` | `nexus_bridge.py` importiert `nexus_memory` | Python mit installiertem Paket |

`http` ist der Normalbetrieb: kein Python auf der Harness-Seite, dieselbe
Hybrid-Suche (BM25 + Vektor + RRF, Graph-Boost, Verification) wie für alle
anderen Agenten. `stdio` ist der Rückfallweg für Maschinen ohne Dienst; er
benutzt `MemoryStore.remember()` / `.recall()` direkt.

## Konfiguration

Alles Optionale steht in `cordis.patch.yml` im Bundle — der einzige Ort, den man
auf einem anderen Rechner anpassen muss.

| Feld | Default | Bedeutung |
|---|---|---|
| `transport` | `http` | `http` oder `stdio` |
| `baseUrl` | `http://127.0.0.1:9122` | Adresse des Serve-Daemons |
| `serviceKey` | — | Installationsschlüssel direkt setzen (überschreibt `keyFile`) |
| `keyFile` | `<dataDir>/service_auth.key` bzw. `~/.nexus-memory/service_auth.key` | Installationsschlüssel: öffnet **nur** `POST /enroll` |
| `agentHarness` | `dsh` | Harness-Name beim Enroll und Namensgeber des eigenen Schlüssels; erlaubt sind `[a-z0-9._-]` (1–64 Zeichen) |
| `agentLabel` | `DeepSeek Harness` | Anzeigename, den das Plugin beim Enroll einträgt |
| `agentKeyFile` | `<dataDir>/agent_keys/<agentHarness>.key` | eigener Schlüssel des Harness; holt und schreibt das Plugin beim ersten Start selbst |
| `accessLevel` | `private` | Sichtbarkeit beim Lesen **und** Ablage-Level beim Schreiben |
| `captureCategory` | `session` | Kategorie der Auto-Captures |
| `recall` / `capture` | `true` | die beiden Hälften einzeln abschaltbar |
| `recallLimit` | `5` | maximale Treffer pro Turn |
| `recallBudgetChars` | `1600` | Zeichenbudget des injizierten Blocks |
| `recallTimeoutMs` | `4000` | danach ohne Erinnerung weiter |
| `recallMinChars` | `30` | keine Einblendung, wenn die letzte menschliche Nachricht kürzer ist |
| `recallMinWords` | `4` | keine Einblendung, wenn sie weniger Wörter hat |
| `captureMinChars` / `captureMaxChars` | `12` / `1600` | Capture-Grenzen |
| `captureSubagents` | `false` | Turns von Subagenten mitcapturen |
| `reportIncidents` | `true` | Gescheiterte Werkzeugaufrufe an `POST /events` melden, damit das nächtliche Lernen sie sieht (nur HTTP-Transport) |
| `reportMaxChars` | `2000` | Grenze des Fehlertextes je Meldung |
| `reportPerSessionMax` | `50` | Höchstzahl der Meldungen je Sitzung |

Zwei Installationen am selben Dienst brauchen **keine** verschiedenen Namen: ein
Eintrag, der schon einen eigenen Schlüssel hat, wird nie übernommen — die zweite
Installation bekommt automatisch einen eigenen Eintrag mit eigener Kennung, auch
wenn beide `dsh` heißen. Ein eigener Name (`agentHarness`) ist nur noch eine
freie Zugabe, wenn man sie im Dashboard auseinanderhalten will.

Nur für `stdio`: `python`, `bridge`, `pythonPath`, `nexusHome`, `dataDir`,
`qdrantHost`, `qdrantPort`, `qdrantExe`, `qdrantStorage`, `ollamaExe`,
`embeddingProvider`, `agentId`, `logDir`.

## Installation

Ein Befehl richtet alles ein, auf macOS, Linux und Windows gleich:

```bash
node scripts/install_dsh_plugin.mjs                  # Profil "desktop", Harness "dsh"
node scripts/install_dsh_plugin.mjs --harness dsh-win --base-url http://127.0.0.1:9122
```

Das Skript installiert zuerst die Pakete des Plugins (`npm install --omit=dev` im
Ordner, den das Profil registriert) — ohne `@deepseek-ai/dsh-llm` und
`@deepseek-ai/schemastery` bricht der Harness beim Laden mit
`ERR_MODULE_NOT_FOUND` ab, und das Plugin sieht trotzdem installiert aus.
Voraussetzung ist Node 22+ und npm.

Dann prüft es den Dienst, holt beim ersten Mal einen eigenen Schlüssel
(`POST /enroll`), legt ihn unter `<datenverzeichnis>/agent_keys/<harness>.key` ab
und schreibt `NEXUS_AGENT_KEY` in `<profil>/.env`. Gilt der vorhandene Schlüssel
noch, wird nichts ersetzt (`--force-enroll` erzwingt es).

Danach den Harness neu starten — Bundle-Listen werden nur beim Start gelesen.

Die Werkzeuge kommen mit dem Plugin, ohne Zusatzschritt: `recall`, `remember`,
`forget`, `update`, `fact_history`, `report`, `health`, `guardrail_check`
(Rolle `know`). Im Harness heißen sie `mcp__nexus__recall` und so weiter. Fehlt
`NEXUS_AGENT_KEY`, startet der Harness trotzdem — dann nur ohne Werkzeuge.

Von Hand, ohne Skript:

```powershell
dsh plugin --profile desktop add <absoluter-pfad>/nexus-memory
# Harness neu starten — Bundle-Listen werden nur beim Start gelesen
```

Prüfen, ob der Dienst erreichbar ist und der eigene Schlüssel passt:

```bash
curl -s http://127.0.0.1:9122/healthz
KEY=$(cat ~/.nexus-memory/agent_keys/dsh.key)   # Installationsschlüssel öffnet keine Daten
curl -s -X POST http://127.0.0.1:9122/write -H 'content-type: application/json' \
  -H "x-nexus-service-key: $KEY" \
  -d '{"content":"Probe: DSH schreibt über den Serve-Daemon.","category":"session","source":"dsh-check","source_type":"session"}'
```

Den `stdio`-Pfad kann man ohne Harness testen:

```powershell
python plugins/nexus-memory/smoke-test.py
```

## Grenzen und bewusste Entscheidungen

- **Kein LLM-Extraktor.** Capture speichert den Turn als Episode; das
  Destillieren in Fakten übernimmt der Consolidation-Daemon der Installation.
- **Episodisch heißt redundant.** Jeder Turn landet im Store und unterliegt der
  `session`-Retention (`SICA_RETENTION_SESSION`, Default 7 Tage).
- **Recall kostet Zeit.** Ein Hybrid-Recall dauert je nach Store-Größe einige
  hundert Millisekunden bis ~2 s. Deshalb das harte Limit: lieber ohne
  Erinnerung antworten als den Turn hängen lassen.
- **Ein Access-Level für beide Richtungen.** `private` schreibt privat und sieht
  privat; ein anderer Agent mit `public` sieht diese Einträge nicht.
- **Zwei Schlüssel, zwei Aufgaben.** Der Installationsschlüssel
  (`service_auth.key`) öffnet nur noch `POST /enroll` und nur von Loopback.
  Lesen und Schreiben gehen ausschließlich mit dem eigenen Schlüssel in
  `<dataDir>/agent_keys/dsh.key`; den holt und schreibt das Plugin beim ersten
  Start selbst. Ein Agent kann sich mit seinem eigenen Schlüssel nicht erneut
  einschreiben.

## Deinstallation

```powershell
dsh plugin --profile desktop remove @local/dsh-nexus-memory
```

Danach den Eintrag aus `dsh.profile.bundles` entfernen und neu starten. Am Store
ändert das nichts — die Memories bleiben, wo sie sind.
