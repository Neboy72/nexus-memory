# Nexus Memory — Roadmap

> Lernpunkte aus externen Systemen, die wir adaptieren oder als Benchmark nutzen können.
> Jeder Eintrag: Quelle, was übernommen werden soll, warum, Status.

---

## 1. TencentDB Agent Memory — Schichten-Destillation (L0→L3)

- **Quelle:** https://github.com/TencentCloud/TencentDB-Agent-Memory (27.038 Stars Stand 20.09.2026, MIT, letzter Commit 20.09.2026, stabil v2.0.1)
- **Was:** Vierstufige Destillations-Pipeline: L0 Rohkonversation → L1 atomare Fakten → L2 Szenarien-Blöcke → L3 Persona, per asynchroner LLM-Extraktion mit Vektordedup beim Schreiben. L2/L3 werden in den Systemprompt injiziert, L1/L0 als Tools on-demand (KV-Cache-freundlich).
- **Warum für Nexus:** Nexus' Auto-Capture speichert Turns linear; eine echte Schichten-Destillation mit automatischer Persona-Synthese (L3) fehlt. Die L1-Extraktions-Prompts und die Vektordedup-Logik sind direkt benchbar gegen unsere remember-Pipeline.
- **Status:** Offen — noch nicht begonnen.
- **Nächster Schritt:** Evaluierungs-Deployment auf dem Mac Mini (SQLite-Backend + lokale Embeddings, Ports 8125/8420/8421), L1-Extraktions-Prompts gegen Nexus-Auto-Capture benchen.

## 2. Proxy-Injection statt Plugin-Zwang (MemoryProxy)

- **Quelle:** wie oben — transparenter Reverse-Proxy auf :8421/:8096, der OpenAI- und Anthropic-Protokolle unverändert weiterleitet und Memory-Injection + Write-back einhängt.
- **Was:** Zero-Code-Anbindung beliebiger Agenten über base-URL-Wechsel — kein Plugin, kein MCP nötig.
- **Warum für Nexus:** Ergänzt Nexus' Vision "universal memory layer für ALLE Agenten": Agenten ohne Plugin-/MCP-Pfad (z.B. fremde Coding-Tools) bekämen Memory trotzdem. Könnte als optionaler zweiter Pfad neben Hermes/OpenClaw-Plugin und MCP-Server existieren. Das KV-Cache-freundliche Toolisieren von L0/L1 ist ein starkes Detail gegen unser Prefetch-Modell.
- **Status:** Offen.

## 3. Skill-Assets mit Review-Workflow + Web-Panel-Governance

- **Quelle:** MemoryPanel (Web-UI) + Skill-Asset-Typ mit Versionen, Trigger-Grenzen, Ausführungsschritten, Validierungsregeln.
- **Was:** Menschliche Governance-Schicht: Assets reviewen, sharen, Versionen/Ownership/Usage-Counts verwalten.
- **Warum für Nexus:** Nexus hat nur reinen API-Zugriff; ein Review-Panel als UX-Maßstab für unsere (geplante) Team-/Governance-Features.
- **Status:** Offen.

## 4. Lokale Embedding-Fallback-Strategie

- **Quelle:** Tencent läuft default komplett lokal (node-llama-cpp, embeddinggemma-300m GGUF) mit Auto-Fallback-Kette.
- **Warum für Nexus:** Im Auto-Modus ist Nexus heute lokal-zuerst (HuggingFace `sentence-transformers`; Ollama nur auf ausdrückliche Wahl); die Cloud-Kette (Voyage → OpenAI → Google → Jina) läuft nur auf ausdrückliche Wahl. Offen bleibt: Qualitätsangabe je lokalem Tier dokumentieren.
- **Status:** Offen.

## Nicht adaptieren (Differenzierung behalten)

Diese Nexus-Features hat Tencent (Stand 20.09.2026) nicht — das bleibt unser Vorsprung:

- Getypter Knowledge Graph mit Multi-Hop-Traversal (device/service/person/...)
- Fact-History mit Supersession-Chain
- Active Guardrails (destruktive Aktionen prüfen vor Ausführung)
- SICA Self-Improvement mit Drift-/Stale-Detection
- Memory-Kategorien mit Decay und Salience
- Source-Tier-Boost im RRF-Ranking
- Webhook-Subscriptions

## 5. Nexus in den Hermes Plugin-Katalog (GO erteilt 20.09., Ausführung wartet auf Nebos Anstoß)

- **Quelle:** `hermes plugins`-System (Katalog + Install-Weg) in Hermes v0.21.3; Plugin besteht `hermes plugins validate` komplett grün (geprüft 20.09.).
- **Was:** Verpackung, kein Neubau: (1) Install per `hermes plugins install neboy72/nexus-memory` testen und Repo-Layout nötigenfalls anpassen, (2) Katalog-YAML-Eintrag schreiben (auf exakten 40-Zeichen-Commit gepinnt, Kategorie `memory`), (3) Logo aus raw.githubusercontent.com hinterlegen, (4) PR an NousResearch/hermes-agent.
- **Aufwand:** 2-4 Stunden unsere Seite; danach Review durch Nous (Tage bis Wochen, außerhalb unserer Kontrolle). Vor Katalog-Aufnahme funktioniert Repo-Shorthand-Install bereits.
- **Status:** GO erteilt, Start sobald angefragt (morgen/übermorgen) — frische Session dafür.

## Prüfregeln für Adaptions-Kandidaten

- Updates an Nexus NUR nach Tests auf `~/nexus-memory-test/` (HART-Regel, siehe Memory).
- Backups heilig, Portabilität Prio.
- Keine Familien-/Finanz-Daten in öffentlichen Artefakten.