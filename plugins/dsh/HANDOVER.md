# Übergabe: DeepSeek Harness ↔ Nexus Memory

Dieses Dokument ist für den Agenten geschrieben, der die **bestehende**
Nexus-Memory-Installation betreut (Hermes). Ziel: den DeepSeek Harness als
weiteren Client an *denselben* Store hängen — Auto-Recall vor dem Turn,
Auto-Capture nach dem Turn.

**Wichtig:** Das Plugin bringt keinen eigenen Store mit. Es ist ein Client.
Qdrant, Embeddings, Kategorien, Access-Level und Maintenance bleiben da, wo sie
schon sind.

---

## 1. Der Vertrag

Das Plugin spricht ausschließlich HTTP mit einem laufenden
`nexus-memory serve` (Standard `http://127.0.0.1:9122`). Es braucht **keinen**
Zugriff auf Qdrant, **keine** Embedding-Provider-Zugänge und **kein** Python.

Drei Endpunkte, alle loopback-only:

| Endpunkt | Zweck | Auth |
|---|---|---|
| `GET /healthz` | Lebt der Dienst? | keins |
| `POST /mcp` | MCP über Streamable HTTP — `recall` | Agentenschlüssel (`<data-dir>/agent_keys/dsh.key`) |
| `POST /write` | Schreibpfad mit Write-Gate — Capture | Agentenschlüssel (`<data-dir>/agent_keys/dsh.key`) |
| `POST /enroll` | einmalig den eigenen Schlüssel holen | Installationsschlüssel, nur von Loopback |

Zwei Schlüssel, zwei Aufgaben. Der **Installationsschlüssel** liegt in
`<data-dir>/service_auth.key` (mode 0600), wird beim ersten Start des Daemons
erzeugt und kann per `NEXUS_SERVICE_AUTH_KEY` überschrieben werden. Er öffnet
nur `POST /enroll`; auf `/embed`, `/write` und `/mcp` antwortet er mit
`401 plugin update required`. Der **Agentenschlüssel** liegt in
`<data-dir>/agent_keys/dsh.key`, wird beim ersten Start des Plugins geholt und
benennt den Agenten. `<data-dir>` ist `NEXUS_DATA_DIR` oder `~/.nexus-memory`.
Der Kopf `x-nexus-agent-id` hat auf Datenrouten keine Wirkung mehr.

### 1.1 Recall (JSON-RPC über SSE)

`POST /mcp`, Header:

```
content-type: application/json
accept: application/json, text/event-stream
x-nexus-service-key: <key>
```

Schritt 1 — Handshake:

```json
{"jsonrpc":"2.0","id":1,"method":"initialize",
 "params":{"protocolVersion":"2025-06-18","capabilities":{},
           "clientInfo":{"name":"dsh-nexus-memory","version":"0.3.0"}}}
```

Antwort: `200`, `content-type: text/event-stream`, Header `mcp-session-id`,
Body als SSE-Frame:

```
event: message

data: {"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2025-06-18","capabilities":{"tools":{}},"serverInfo":{"name":"nexus-memory","version":"0.22.48"}}}
```

Schritt 2 — Quittung (Antwort `202`, kein Body):

```json
{"jsonrpc":"2.0","method":"notifications/initialized"}
```

Ab jetzt gehört `mcp-session-id` in **jeden** weiteren Request.

Schritt 3 — Recall:

```json
{"jsonrpc":"2.0","id":2,"method":"tools/call",
 "params":{"name":"recall",
           "arguments":{"query":"…","limit":5,"filter_level":"private"}}}
```

Antwort (wieder SSE):

```json
{"jsonrpc":"2.0","id":2,"result":{"content":[
  {"type":"text","text":"{\"results\":[…],\"count\":3}"}],"isError":false}}
```

**Stolperfalle:** `result.content[0].text` ist ein *String*, der erst geparst
werden muss. Jeder Treffer hat u. a. `text`, `score`, `category`, `source`,
`access_level`, `created_at`, `verification`, `id`.

`filter_level` ist das Access-Level des Aufrufers: `private` sieht alles,
`public` sieht nur öffentliche Einträge.

### 1.2 Capture (`POST /write`)

```json
{"content":"<text>","category":"session","access_level":"private",
 "source":"dsh-plugin","source_type":"session"}
```

Antwort `200`:

```json
{"status":"ok","id":"<uuid>","access_level":"private","category":"session","scope":"default","superseded":null}
```

Antwort `422` (Write-Gate hat abgelehnt — nichts wurde geschrieben):

```json
{"status":"rejected","reason":"…"}
```

Das Gate gilt hier genauso wie für alle anderen Clients: nicht-leerer Inhalt,
`fact` muss eine Aussage sein, Herkunft (`source`) nicht leer, Access-Level
passend zur Quelle, `rule` nur mit `rule_proof`.

### 1.3 Fehlerbilder

| Symptom | Ursache |
|---|---|
| `401 {"error":"unauthorized"}` | Key fehlt oder ist falsch |
| `401 {"error":"plugin update required"}` | Installationsschlüssel auf einer Datenroute benutzt — Plugin muss sich per `/enroll` einen eigenen Schlüssel holen |
| `400 {"code":-32600,"message":"Bad Request: Missing session ID"}` | `initialize` fehlt oder `mcp-session-id` wird nicht mitgeschickt |
| `503` auf `/embed` | kein Embedding-Provider im Dienst verfügbar |
| Verbindung abgelehnt | Daemon läuft nicht (oder anderer Port) |

---

## 2. Was Hermes tun muss

1. **Daemon prüfen.** `curl -s http://127.0.0.1:9122/healthz` →
   `{"status":"ok","qdrant":true}`. Läuft er nicht: `./scripts/install_serve.sh`
   (installiert ihn als OS-Dienst; idempotent). Ohne Daemon funktioniert nur der
   Fallback aus Abschnitt 4.
2. **Schlüssel feststellen.** Zwei Dateien, zwei Aufgaben:
   `<data-dir>/service_auth.key` ist der Installationsschlüssel (nur `POST /enroll`,
   Loopback), `<data-dir>/agent_keys/dsh.key` ist der eigene Schlüssel des Harness
   (Datenzugriff) und entsteht beim ersten Start des Plugins. Fehlt er, einmalig
   mit dem Installationsschlüssel enrolen: `POST /enroll` mit `{"harness":"dsh"}`.
   Der Agentenschlüssel muss für den DSH-Nutzer lesbar sein.
3. **Nichts am Store ändern.** Kein neuer Collection-Name, keine neue
   Embedding-Konfiguration, kein zweiter Qdrant. Der Harness schreibt in
   dieselbe Collection `nexus` und wird als eigener Agent sichtbar
   (`source: dsh-plugin`).
4. **Optional: Retention für `session` prüfen.** Die gecapturten Turns sind
   episodisch (`category: session`). `SICA_RETENTION_SESSION` (Default 7 Tage)
   bestimmt, wie lange sie im Recall auftauchen. Wer längere Erinnerung will,
   hebt den Wert oder lässt den Consolidation-Daemon sie in Fakten verdichten.
5. **Plugin im DSH-Profil installieren** (siehe Abschnitt 3) und den Harness
   neu starten — Bundle-Listen werden nur beim Start gelesen.

## 3. Installation auf der DSH-Seite

Das Bundle liegt als Ordner `nexus-memory/` vor (dieses Verzeichnis). Auf dem
Rechner des Harness, **ein Schritt** (macOS, Linux, Windows gleich):

```bash
node scripts/install_dsh_plugin.mjs --profile desktop --harness dsh
# danach den Harness neu starten
```

Das Skript prüft den Dienst (`/healthz`), prüft einen schon vorhandenen Schlüssel
gegen `/embed` und enrolt nur, wenn keiner gilt oder er abgelehnt wird. Es legt
`<datenverzeichnis>/agent_keys/<harness>.key` an und setzt `NEXUS_AGENT_KEY` in
`<profil>/.env` — daraus liest die mitgelieferte Werkzeug-Anbindung ihren
Schlüssel beim Start.

Von Hand, ohne Skript:

```powershell
# 1. Ordner ablegen, z. B. unter <workspace>\plugins\nexus-memory
# 2. Als Profil-Bundle registrieren (schreibt dependency + bundles-Liste):
dsh plugin --profile desktop add <absoluter-pfad>/nexus-memory

# 3. Harness neu starten
```

Alternativ von Hand in `~/.dsh/profiles/<profil>/package.json`:

```json
{
  "dependencies": { "@local/dsh-nexus-memory": "link:<absoluter-pfad>/nexus-memory" },
  "dsh": { "profile": { "bundles": ["@deepseek-ai/dsh-base", "@deepseek-ai/dsh-web-app", "@local/dsh-nexus-memory"] } }
}
```

Danach in `cordis.patch.yml` (im Bundle) oder direkt in der Profil-Patch-Datei:

```yaml
- insert:
    - id: nexus-memory
      name: '@local/dsh-nexus-memory'
      config:
        transport: http
        baseUrl: 'http://127.0.0.1:9122'
        keyFile: '~/.nexus-memory/service_auth.key'   # Installationsschlüssel, nur für /enroll
        accessLevel: private
```

Läuft der Daemon auf einem anderen Port/Host, nur `baseUrl` ändern.

## 4. Fallback ohne Daemon: `transport: stdio`

Wenn auf der Harness-Maschine kein Dienst laufen soll, das Python-Paket aber
installiert ist (`pip install -e <checkout>` oder `pip install nexus-memory`),
importiert `nexus_bridge.py` den Store direkt:

```yaml
config:
  transport: stdio
  python: 'C:\…\python.exe'          # Interpreter, der nexus_memory importieren kann
  nexusHome: 'C:\…\nexus-memory'      # optional, wird PYTHONPATH vorangestellt
  pythonPath: '…\nexus-memory\src;…\pylibs'
  qdrantHost: 127.0.0.1
  qdrantPort: 6333
```

Dieser Pfad benutzt `MemoryStore.remember()` / `.recall()` — dieselben
Funktionen wie der MCP-Server, also dieselbe Collection und dieselben Regeln,
aber **ohne** Write-Gate-HTTP-Runde. Er ist der Rückfallweg, nicht der
Normalbetrieb.

## 5. Abnahme

```bash
# a) Dienst lebt
curl -s http://127.0.0.1:9122/healthz

# b) eigener Agentenschlüssel funktioniert
KEY=$(cat ~/.nexus-memory/agent_keys/dsh.key)
curl -s -X POST http://127.0.0.1:9122/write \
  -H 'content-type: application/json' -H "x-nexus-service-key: $KEY" \
  -d '{"content":"Abnahme-Probe: DSH schreibt über den Serve-Daemon.","category":"session","source":"dsh-handover","source_type":"session"}'
# → {"status":"ok","id":"…"}
# Gegenprobe: der Installationsschlüssel muss hier 401 "plugin update required" geben

# c) Recall findet sie
#    (gleiche initialize/tools/call-Sequenz wie in 1.1, filter_level: private)
```

Im Harness:

1. Neustart, dann eine Frage stellen, die zu einer gespeicherten Erinnerung
   passt.
2. Im Session-Log erscheint vor der Antwort eine User-Nachricht mit
   `source.kind = "nexus-memory"` und dem Block
   `Nexus Memory — N relevant entries …`.
3. Nach dem Turn ist ein neuer Punkt im Store (Dashboard oder `recall`).

**Negativtest:** Daemon stoppen, Turn fahren. Der Turn läuft normal durch, im
Log steht eine Warnung `auto-recall skipped`, nichts blockiert. Das ist Absicht
— Erinnerung darf nie Arbeit verhindern.

## 6. Was das Plugin bewusst nicht tut

- **Kein eigener Store, keine eigenen Embeddings.** Es ruft den Dienst, der die
  Vektoren ohnehin schon hat.
- **Kein LLM-Extraktor im Capture.** Gespeichert wird der Turn als Episode;
  Destillieren in Fakten übernimmt der Consolidation-Daemon (Dreaming/SICA),
  der bereits läuft.
- **Kein Schreiben in den Systemprompt.** Die Erinnerungen sind eine
  quellenattribuierte User-Nachricht im Session-Log: sichtbar, forkbar,
  nachvollziehbar.
- **Kein Retry-Sturm.** Recall hat ein hartes Zeitlimit (Default 4 s), Capture
  läuft fire-and-forget nach `turn/end`.

## 7. Tuning für den Alltag

| Feld | Default | Wirkung |
|---|---|---|
| `recallLimit` | 5 | Treffer pro Turn |
| `recallBudgetChars` | 1600 | Zeichenbudget des injizierten Blocks |
| `recallTimeoutMs` | 4000 | danach ohne Erinnerung weiter |
| `accessLevel` | private | bestimmt Sichtbarkeit **und** Ablage-Level |
| `captureCategory` | session | Kategorie der Auto-Captures |
| `captureMinChars` | 12 | kürzere Turns werden nicht gespeichert |
| `captureSubagents` | false | Subagent-Turns nicht mitschreiben |

Die vollständige Liste steht in `README.md` im Bundle.
