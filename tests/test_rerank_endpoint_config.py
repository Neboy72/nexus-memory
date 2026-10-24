"""Tests for the universal LLM endpoint resolver in ``nexus_memory.reranker``.

All tests are offline: no credential is real and no HTTP call reaches the
network.  The suite verifies the precedence rule:

    NEXUS_RERANK_LLM_* env vars > Hermes config.yaml > no endpoint

and that an API key never appears in logs.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import yaml

import nexus_memory.reranker as rr


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    """Keep the process environment free of rerank-specific LLM variables."""
    for name in (
        "NEXUS_RERANK_LLM_BASE_URL",
        "NEXUS_RERANK_LLM_API_KEY",
        "NEXUS_RERANK_LLM_MODEL",
        "NEXUS_RERANK",
        "NEXUS_RERANKER",
        "TEST_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)


class TestResolveLlmEndpoint:
    def test_env_vars_win_against_config(self, tmp_path, monkeypatch):
        """Dedicated env variables have absolute priority over config.yaml."""
        config = {
            "model": {"default": "cfg-model", "provider": "ollama-cloud"},
            "providers": {
                "ollama-cloud": {
                    "base_url": "http://cfg-host:11434/v1",
                    "api_key": "cfg-secret",
                }
            },
        }
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text(yaml.safe_dump(config))

        monkeypatch.setenv("NEXUS_RERANK_LLM_BASE_URL", "http://env-host:11434/v1")
        monkeypatch.setenv("NEXUS_RERANK_LLM_API_KEY", "env-secret")
        monkeypatch.setenv("NEXUS_RERANK_LLM_MODEL", "env-model")

        base_url, api_key, model, configured = rr.resolve_llm_endpoint(str(cfg_path))
        assert configured is True
        assert base_url == "http://env-host:11434/v1"
        assert api_key == "env-secret"
        assert model == "env-model"

    def test_config_only_resolves(self, tmp_path):
        """Hermes config.yaml is used when no dedicated env vars are set."""
        config = {
            "model": {"default": "user-model", "provider": "ollama-cloud"},
            "providers": {
                "ollama-cloud": {
                    "base_url": "http://user-host:11434/v1",
                    "api_key": "user-secret",
                }
            },
        }
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text(yaml.safe_dump(config))

        base_url, api_key, model, configured = rr.resolve_llm_endpoint(str(cfg_path))
        assert configured is True
        assert base_url == "http://user-host:11434/v1"
        assert api_key == "user-secret"
        assert model == "user-model"

    def test_api_key_env_reference(self, tmp_path, monkeypatch):
        """``api_key: env:NAME`` is expanded from the environment."""
        config = {
            "model": {"default": "m", "provider": "ollama-cloud"},
            "providers": {
                "ollama-cloud": {
                    "base_url": "http://host:11434/v1",
                    "api_key": "env:TEST_API_KEY",
                }
            },
        }
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text(yaml.safe_dump(config))

        monkeypatch.setenv("TEST_API_KEY", "secret-from-env")
        base_url, api_key, model, configured = rr.resolve_llm_endpoint(str(cfg_path))
        assert configured is True
        assert api_key == "secret-from-env"
        assert model == "m"

    def test_api_key_env_reference_missing_makes_unconfigured(self, tmp_path):
        """``api_key: env:NAME`` with missing NAME leaves the endpoint unconfigured."""
        config = {
            "model": {"default": "m", "provider": "ollama-cloud"},
            "providers": {
                "ollama-cloud": {
                    "base_url": "http://host:11434/v1",
                    "api_key": "env:TEST_API_KEY",
                }
            },
        }
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text(yaml.safe_dump(config))

        base_url, api_key, model, configured = rr.resolve_llm_endpoint(str(cfg_path))
        assert configured is False
        assert api_key is None

    def test_missing_config_yields_unconfigured(self, tmp_path):
        """No config at all means no endpoint and no built-in model fallback."""
        base_url, api_key, model, configured = rr.resolve_llm_endpoint(
            str(tmp_path / "missing.yaml")
        )
        assert configured is False
        assert base_url is None
        assert api_key is None
        assert model is None

    def test_malformed_yaml_is_swallowed(self, tmp_path, caplog):
        """Broken YAML does not crash; the endpoint is simply unconfigured."""
        cfg_path = tmp_path / "broken.yaml"
        cfg_path.write_text("model: [`unbalanced")

        with caplog.at_level(logging.DEBUG, logger="nexus.hermes_plugin.rerank"):
            base_url, api_key, model, configured = rr.resolve_llm_endpoint(str(cfg_path))

        assert configured is False
        assert base_url is None
        assert api_key is None
        assert model is None

    def test_api_key_never_appears_in_logs(self, tmp_path, caplog, monkeypatch):
        """Only host and model are logged; the API key is never emitted."""
        config = {
            "model": {"default": "secret-model", "provider": "ollama-cloud"},
            "providers": {
                "ollama-cloud": {
                    "base_url": "http://log-host:11434/v1",
                    "api_key": "super-secret-key-12345",
                }
            },
        }
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text(yaml.safe_dump(config))

        # Force at least one DEBUG log path to run.
        monkeypatch.setenv("PYTEST_CAPTURE_LOGS", "1")
        with caplog.at_level(logging.DEBUG, logger="nexus.hermes_plugin.rerank"):
            rr.resolve_llm_endpoint(str(cfg_path))

        log_text = "\n".join(f"{r.levelname}|{r.message}" for r in caplog.records)
        assert "super-secret-key-12345" not in log_text
        # Host and model may be logged, proving logging is active.
        assert any("log-host" in r.message for r in caplog.records)
        assert any("secret-model" in r.message for r in caplog.records)


class TestLoadRerankConfigEndpoint:
    def test_load_rerank_config_includes_llm_endpoint(self, tmp_path):
        """``load_rerank_config`` exposes the resolved LLM endpoint."""
        config = {
            "nexus-memory": {"rerank": True, "reranker": "llm", "rerank_pool": 10},
            "model": {"default": "m", "provider": "ollama-cloud"},
            "providers": {
                "ollama-cloud": {
                    "base_url": "http://host:11434/v1",
                    "api_key": "k",
                }
            },
        }
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text(yaml.safe_dump(config))

        cfg = rr.load_rerank_config(str(cfg_path))
        assert cfg["enabled"] is True
        assert cfg["reranker"] == "llm"
        assert cfg["pool_k"] == 10
        assert cfg["llm_base_url"] == "http://host:11434/v1"
        assert cfg["llm_model"] == "m"
        assert cfg["llm_configured"] is True

    def test_load_rerank_config_without_model_default(self, tmp_path, monkeypatch):
        """Config without model.default -> llm_configured False, no HTTP call."""
        # _rerank_llm resolves the endpoint on its own (~/.hermes/config.yaml).
        # Point HOME at the empty tmp dir so a developer's real endpoint — or a
        # key another test left in os.environ — cannot make this a live call.
        monkeypatch.setenv("HOME", str(tmp_path))
        config = {
            "nexus-memory": {"rerank": True, "reranker": "llm"},
            "model": {"provider": "ollama-cloud"},  # no default
            "providers": {
                "ollama-cloud": {
                    "base_url": "http://host:11434/v1",
                    "api_key": "k",
                }
            },
        }
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text(yaml.safe_dump(config))

        cfg = rr.load_rerank_config(str(cfg_path))
        assert cfg["llm_model"] is None
        assert cfg["llm_configured"] is False

        # _rerank_llm must not open a connection.
        with patch("nexus_memory.reranker.urllib.request.urlopen") as urlopen_mock:
            rr._rerank_llm("q", [{"_idx": 0, "text": "t"}])
        urlopen_mock.assert_not_called()

    def test_load_rerank_config_without_config(self, tmp_path):
        """Without a config file the LLM endpoint is unconfigured but defaults stand."""
        cfg = rr.load_rerank_config(str(tmp_path / "missing.yaml"))
        assert cfg["enabled"] is False
        assert cfg["reranker"] == "llm"
        assert cfg["pool_k"] == 20
        assert cfg["llm_base_url"] is None
        assert cfg["llm_model"] is None
        assert cfg["llm_configured"] is False


class TestRerankLlmWithoutFallback:
    def test_no_fallback_model_no_http_call(self, tmp_path, monkeypatch):
        """Without env, model, key and base, rerank_points keeps vector order and is silent."""
        # Same isolation as above: the resolver must not read the real config.
        monkeypatch.setenv("HOME", str(tmp_path))
        # Clear any cached config.
        monkeypatch.setattr(rr, "_LLM_UNAVAILABLE_LOGGED", False)

        with patch("nexus_memory.reranker.urllib.request.urlopen") as urlopen_mock:
            pts = [
                SimpleNamespace(id="p0", payload={"id": "p0", "content": "a"}, score=0.9),
                SimpleNamespace(id="p1", payload={"id": "p1", "content": "b"}, score=0.8),
            ]
            out = rr.rerank_points("q", pts, reranker="llm", pool_k=2)
            assert [p.id for p in out] == ["p0", "p1"]
            urlopen_mock.assert_not_called()


class TestUniversalProduct:
    def test_no_hardcoded_vendor_strings_remain(self):
        """The implementation must not contain our private model or provider names."""
        source = Path(rr.__file__).read_text()
        forbidden = ["deepseek-v4.1-flash:1017-cloud", "ollama-cloud", "Neboy"]
        for token in forbidden:
            assert token not in source, f"private token {token!r} remains in reranker.py"

    def test_default_llm_model_constant_removed(self):
        """The old constant no longer exists."""
        assert not hasattr(rr, "DEFAULT_LLM_MODEL")
