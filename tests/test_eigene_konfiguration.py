"""Eigene Konfiguration kommt zuerst — Hermes ist nur eine moegliche Quelle.

Befund 25.11.2026: `reranker.py` und `consolidation.py` lasen standardmaessig
`~/.hermes/config.yaml`. Nexus Memory ist eigenstaendig und darf nicht
voraussetzen, dass ein Agent installiert ist.

Geprueft:
1. Ohne Hermes-Datei werden Rerank-Endpunkt und Modell aus
   ``~/.nexus-memory/config.json`` gelesen (``NEXUS_CONFIG`` zeigt im Test
   darauf).
2. Die eigene Konfiguration gewinnt gegen eine vorhandene Hermes-Datei.
3. Ohne jede Konfiguration laeuft beides normal weiter: Rerank bleibt
   abgeschaltet (kein Fehler), Konsolidierung meldet "kein Modell" — nichts
   wirft, nichts schaltet still ab.
"""
from __future__ import annotations

import json

from nexus_memory import consolidation, reranker


def _eigene_config(tmp_path, monkeypatch, inhalt):
    pfad = tmp_path / "config.json"
    pfad.write_text(json.dumps(inhalt), encoding="utf-8")
    monkeypatch.setenv("NEXUS_CONFIG", str(pfad))
    for name in ("NEXUS_RERANK", "NEXUS_RERANKER", "NEXUS_RERANK_LLM_BASE_URL",
                 "NEXUS_RERANK_LLM_API_KEY", "NEXUS_RERANK_LLM_MODEL",
                 "NEXUS_CONSOLIDATION_MODEL"):
        monkeypatch.delenv(name, raising=False)
    return pfad


def test_ohne_hermes_aus_der_eigenen_datei(tmp_path, monkeypatch):
    _eigene_config(tmp_path, monkeypatch, {
        "rerank": {"enabled": True, "reranker": "cross-encoder", "pool_k": 7,
                   "llm": {"base_url": "http://127.0.0.1:11434/v1",
                           "api_key": "test-key", "model": "eigenes-modell"}},
        "consolidation_model": "konsolidierung-modell",
    })
    hermes = tmp_path / "gibt-es-nicht.yaml"

    base, key, model, configured = reranker.resolve_llm_endpoint(config_path=str(hermes))
    assert (base, key, model, configured) == (
        "http://127.0.0.1:11434/v1", "test-key", "eigenes-modell", True)

    cfg = reranker.load_rerank_config(config_path=str(hermes))
    assert cfg["enabled"] is True
    assert cfg["reranker"] == "cross-encoder"
    assert cfg["pool_k"] == 7
    assert cfg["llm_configured"] is True

    assert consolidation.resolve_consolidation_model(config_path=str(hermes)) == "konsolidierung-modell"


def test_eigene_konfiguration_gewinnt_gegen_hermes(tmp_path, monkeypatch):
    _eigene_config(tmp_path, monkeypatch, {
        "rerank": {"llm": {"base_url": "http://eigen/v1", "api_key": "eigen-key",
                           "model": "eigen-modell"}},
        "consolidation": {"model": "eigen-konsolidierung"},
    })
    hermes = tmp_path / "hermes.yaml"
    hermes.write_text(
        "model:\n  default: hermes-modell\n  provider: ollama\n"
        "providers:\n  ollama:\n    base_url: http://hermes/v1\n    api_key: hermes-key\n"
        "nexus-memory:\n  rerank: false\n  consolidation_model: hermes-konsolidierung\n",
        encoding="utf-8")

    base, key, model, configured = reranker.resolve_llm_endpoint(config_path=str(hermes))
    assert (base, model, configured) == ("http://eigen/v1", "eigen-modell", True), (
        "die eigene Konfiguration muss gegenueber Hermes gewinnen")
    assert consolidation.resolve_consolidation_model(config_path=str(hermes)) == "eigen-konsolidierung"


def test_ohne_jede_konfiguration_laeuft_alles_weiter(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_CONFIG", str(tmp_path / "leer.json"))
    for name in ("NEXUS_RERANK", "NEXUS_RERANKER", "NEXUS_RERANK_LLM_BASE_URL",
                 "NEXUS_RERANK_LLM_API_KEY", "NEXUS_RERANK_LLM_MODEL",
                 "NEXUS_CONSOLIDATION_MODEL"):
        monkeypatch.delenv(name, raising=False)

    hermes = tmp_path / "kein-hermes.yaml"
    base, key, model, configured = reranker.resolve_llm_endpoint(config_path=str(hermes))
    assert configured is False, "ohne Quelle bleibt der Reranker abgeschaltet, ohne zu werfen"
    cfg = reranker.load_rerank_config(config_path=str(hermes))
    assert cfg["enabled"] is False
    assert consolidation.resolve_consolidation_model(config_path=str(hermes)) == ""
