"""``api_key: env:NAME`` in config.yaml must resolve, not be sent as the key itself.

Measured 18.11.2026: config.yaml named ``env:OLLAMA_API_KEY``, the resolver returned that literal
text, and every extraction answered 401 — the entity extractor then fell back to its heuristic,
which reports zero relations, so the knowledge graph stopped growing while looking healthy.
"""

from pathlib import Path

from nexus_memory.llm_endpoint_config import resolve_llm_config


def _home(tmp_path: Path, key_line: str) -> Path:
    (tmp_path / "config.yaml").write_text(
        "model:\n"
        "  default: test-model\n"
        "  provider: custom:testcloud\n"
        "providers:\n"
        "  testcloud:\n"
        "    base_url: https://example.invalid/v1\n"
        f"    api_key: {key_line}\n",
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text("NEXUS_TEST_CLOUD_KEY=real-secret-value\n", encoding="utf-8")
    return tmp_path


def test_env_indirection_is_resolved_from_the_env_file(tmp_path, monkeypatch):
    monkeypatch.delenv("NEXUS_TEST_CLOUD_KEY", raising=False)
    home = _home(tmp_path, "env:NEXUS_TEST_CLOUD_KEY")

    config = resolve_llm_config(str(home))

    assert config["api_key"] == "real-secret-value"
    assert config["base_url"] == "https://example.invalid/v1"
    assert config["model"] == "test-model"


def test_env_indirection_prefers_the_process_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_TEST_CLOUD_KEY", "from-environment")
    home = _home(tmp_path, "env:NEXUS_TEST_CLOUD_KEY")

    config = resolve_llm_config(str(home))

    assert config["api_key"] == "from-environment"


def test_a_literal_key_is_left_alone(tmp_path):
    home = _home(tmp_path, "literal-key")

    config = resolve_llm_config(str(home))

    assert config["api_key"] == "literal-key"
