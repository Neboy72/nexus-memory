# Wiring-Spec: query_rewrite einhaengen (Test-Repo, env-gated)

Aufgabe: Rekonstruierte Datei src/nexus_memory/query_rewrite.py (liegt fertig, 24/24 Tests PASS in tests/test_query_rewrite_recon.py) in den Recall-Pfad des Plugins einhaengen.

## Wo
1. plugins/memory/nexus/__init__.py — _recall() (Zeile ~506): VOR dem Embedding den Query rewriten.
2. plugins/memory/nexus/__init__.py — _do_prefetch() (Zeile ~374): gleicher Rewrite fuer den Auto-Prefetch-Query.

## Wie (chirurgisch)
- Import lazy im Funktionskoerper: from nexus_memory.query_rewrite import rewrite_query
- generate_fn: from nexus_memory.fuel_chain import get_fuel; fn = get_fuel(OLLAMA_BASE, OLLAMA_MODEL, _ollama_generate) — OLLAMA_BASE/MODEL wie in consolidation.py (env NEXUS_OLLAMA_BASE, NEXUS_CONSOLIDATION_MODEL). get_fuel(None-Kette) -> None -> rewrite_query gibt Original zurueck (fail-open).
- generate_fn NUR aufbauen wenn query_rewrite.enabled() (sonst KEIN fuel-chain-Call, null Kosten).
- rewrite_query(query, fn) ersetzt query in beiden Faellen. as_of bleibt unveraendert.
- KEINE weiteren Aenderungen, KEINE neue Abhaengigkeit, kein Logging-Spam (rewrite loggt selbst via nexus.query_rewrite logger).

## Test
- Neue tests/test_query_rewrite_wiring.py: 6 Faelle (recall-rewrite via monkeypatched rewrite_query, prefetch-rewrite, disabled-passthrough, fuel-fail-open, as_of intact, kein-Call-wenn-disabled via counter).
- pytest tests/test_query_rewrite_recon.py tests/test_query_rewrite_wiring.py muss GRUEN sein.

## Verboten
- Nichts im Prod-Repo ~/nexus-memory anfassen. Kein Netz-Call in Tests (generate_fn immer gemockt). Keine Aenderung an query_rewrite.py selbst.