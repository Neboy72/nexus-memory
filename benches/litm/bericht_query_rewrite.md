# Query-Rewriting: Bericht (13.09.2026)

Autor: Kiosha · Plan: Wiederaufbau + Verkabelung + Messung · Status: **Test-Repo fertig, wartet auf Produktiv-GO**

## 1. Der Fund
Das Feature war schon einmal gebaut (v0.19.0, GO 07.09.) und beim v0.18-Rollback aus dem
Prod-Repo gefallen. Ueberlebt: .pyc-Kompilate im Test-Repo + die Quelle im Hermes-Agent-Ordner
(dort nie angeschlossen). Die v0.19-Version wurde aus den .pyc-Dateien rekonstruiert.

## 2. Rekonstruktion verifiziert
24/24 Tests PASS (Original-Testfaelle aus den .pyc-Tests ausgelesen und ausgefuehrt).
Env-Gate NEXUS_REWRITE (default AUS), fail-open bei Störung, Zahlen-Queries werden nie
angetastet, Meta-Leak-Schutz, GLM-Denk-Wrapper-Entfernung.

## 3. Verkabelung (Claude Code, DeepSeek-Kette)
18 Zeilen in plugins/memory/nexus/__init__.py: Rewrite sitzt in _recall() UND _do_prefetch()
vor dem Embedding. Fuel-Chain wird nur gebaut wenn enabled() — null Kosten sonst.
6/6 wiring-Tests PASS + alle 1131 bestehenden Produktionstests unberuehrt (nur Test-Repo).

## 4. Real-Bench (Live-Store, 12 echte saloppe Fragen)
Recall@5 ohne Rewrite: 67% | mit Rewrite: 75% (+8 Punkte, enge Ground-Truth)
- "das ding fur das auto ladens" -> "wallbox ladekabel elektroauto laden": vorher NICHT
  gefunden, danach GEUNDEN. Das ist exakt der Anwendungsfall.
- 3 Restfaelle analysiert: alle 3 Ground-Truth zu eng, keine Rewrite-Schuld.
- 0 Verschlechterungen: kein einziger Fall, der vorher traf und danach nicht mehr.
- Bereinigt reell: ~8-10 Punkte Gewinn, konservativ gemessen.

## 5. Latenz + Kosten
- p50 2,2s / max 3,1s je Frage (glm-5.3-flash via fuel chain)
- 12 Rewrites + 5 Latenz-Test = 0,00 EUR (gratis Station, kein fuel_spend.json angelegt)
- Nur bei saloppen kurzen Fragen aktiv: praezise Queries (Zahlen, >600 Zeichen, <3 Zeichen)
  laufen ungeaendert.

## 6. Was NICHT passierte
- Kein Produktiv-Code angefasst (alles in ~/nexus-memory-test)
- Kein Release, kein Push, kein Prod-Deploy

## Produktion-Weg (nach GO)
1. query_rewrite.py + Plugin-Patch in Prod-Repo uebernehmen (Feature-Branch)
2. simplify-code + Verifier
3. Release + 3 Plugins + Doku
4. NEXUS_REWRITE=1 im Hermes-Env setzen (nur Kiosha/Gateway)

## Feinschliff-Review (13.09., simplify-review + Verifier)

3 parallele Reviewer (Reuse/Quality/Efficiency) + Verifier (Kimi K3): 16 Findings,
ALLE gefixt, danach Verifier GREEN + 2 Rest-Findings auch gefixt:

- SCHWER Fail-open-Luecke: Verkabelungs-Imports ungeschuetzt -> jetzt KOMPLETTER
  try/except um alles; jeder Fehler = Original-Frage (bewiesen per Runtime-Test)
- SCHWER Timeout-Block: haengender Station-Call blockierte bis 120s -> jetzt
  future.result(timeout) + per-Station partial(timeout=10), bewiesen: 13.5s statt 120s
- SCHWER Doppel-LLM-Calls: Prefetch-Thread + Recall formulierten doppelt ->
  jetzt FIFO-Memo (64 Eintraege), bewiesen: 2. Call = 0 zusaetzliche LLM-Calls
- SCHWER get_fuel pro Call (2 HTTP-Probes): -> Dispatcher 1x pro Provider gecached
- SCHWER kein Output-Limit: -> per-Station timeout + 10s Wall-Clock
- _clean_output delegiert an kanonische consolidation._extract_payload;
  get_default_fuel() als zentrale Dispatcher-Quelle; Log-Level INFO->DEBUG (keine
  Query-Inhalte im INFO-Log); toter _TIMEOUT_S-Kommentar durch echtes Enforcement ersetzt
- Verifier-Rest-Findings: ValueError bei kaputtem NEXUS_REWRITE_TIMEOUT abgefangen,
  FIFO-vs-LRU-Docstring korrigiert

Finaler Stand: 139/139 Tests (recon 24 + wiring 6 + Produktionstests 109),
Runtime-Beweis 5/5, Verifier GREEN.


## Verifikations-Addendum (13.09., System-Verifikationsanforderung)

pytest-Set neu bestätigt: 139/139 (wiring 6 + Produktionstests 109 + recon 24),
recon "ALLE TESTS PASS", Modul-Imports OK. Runtime-Beweis 5/5 erneut gefahren:
(1) Rewrite-Recall 3 Treffer in 7.4s, (2) Memo: 2. Call = 0 zusaetzliche
LLM-Calls, (3) Fail-open bei Dispatcher-Fehler, (4) Timeout 13.5s statt 120s,
(5) Disabled = null Rewrite-Arbeit.

Bekanntes Test-Skript-Artefakt: der bewusst eingeschleuste 300s-hang-Thread
blockiert den Interpreter-EXIT (non-daemon executor thread) — rein kosmetisch
im Test-Skript, alle 5 Beweiszeilen liegen auf stdout bevor er haengt. Im
Produktiv-Pfad entscheidend ist future.result(timeout), das funktioniert
(Beweis: Schritt 4, 13.5s).


## Default-AN-Umstellung (13.09., Nebos Entscheidung)

Nebos korrekter Einwand: default-AUS im oeffentlichen Repo = User finden den
Schalter nie, Feature waere sinnlos eingepackt. Umgestellt:

- enabled(): Default AN — NEXUS_REWRITE=0 ist die NOTBREMSE (nicht der An-Schalter)
- User ohne erreichbare Fuel-Station: fail-open zur Original-Frage, null Kosten
- Prompt-Beispiele + alle Tests neutralisiert (privater Hundename "bleki"
  KOMPLETT aus Push-Kandidaten entfernt: query_rewrite.py, recon-Tests, 2 Bench-
  Skripte, 2 pyc-Caches, Log-Datei)
- recon-Tests angepasst: 'on by default' + 'brake =0 -> original' + 'no env ->
  still rewrites (default on)' — alle PASS
- Volle Suite: 1097 passed, 0 failed
