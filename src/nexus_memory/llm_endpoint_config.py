"""llm_endpoint_config.py — one safe resolver for the extraction LLM endpoint.

WHY THIS EXISTS (catalog review, 05.10.2026)
    `extractor.py` and `entity_extractor.py` each carried their own copy of
    `_load_llm_config`. Both read `~/.hermes/.env` and paired whichever key they
    found first — `OLLAMA_API_KEY` or `OPENAI_API_KEY` — with the `base_url`
    from `config.yaml`, without checking that the two belong together. On a
    setup whose `model.base_url` points at a local or third-party relay, that
    sends an OpenAI key to an endpoint it was never issued for, and the key can
    end up in that endpoint's logs.

    The rule here is: a key is only used for the endpoint it was configured
    with. `config.yaml` is the configuration of record — when it names a
    `base_url` and an `api_key`, that pair is used as-is. A key from the
    environment (or from `~/.hermes/.env`, which Hermes itself loads into the
    process environment) is only accepted when the endpoint it belongs to
    matches the configured `base_url`. On a mismatch the configured endpoint is
    kept and only the key is replaced by the non-secret local placeholder, so
    no real credential is ever sent to an endpoint the provider was not issued
    for — at the cost of that extraction call failing auth, which is logged.

    Reading keys happens through `os.environ` first. Hermes loads
    `$HERMES_HOME/.env` into the process environment at startup, so the file
    parse is only a fallback for callers that never went through Hermes — and
    it never overrides a value that is already in the environment.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Dict

logger = logging.getLogger(__name__)

#: Endpoint host markers per provider key. A key is only paired with a
#: `base_url` that names its provider — see the module docstring. A marker is
#: either a full host (`api.openai.com`) or a bare host literal (`localhost`,
#: `ollama`); both are compared with an exact-or-subdomain rule, never as a
#: substring, so `api.openai.com.attacker.example` cannot borrow the key.
_PROVIDER_HOSTS: Dict[str, tuple[str, ...]] = {
    "OPENAI_API_KEY": ("api.openai.com",),
    "OLLAMA_API_KEY": ("localhost", "127.0.0.1", "::1", "0.0.0.0", "ollama"),
}

#: The local endpoint used when nothing usable is configured.
_LOCAL_BASE_URL = "http://localhost:11434/v1"
_LOCAL_API_KEY = "ollama"
_DEFAULT_MODEL = "gemma3:4b"


def _env_file_key(hermes_home: str, key_name: str) -> str:
    """Read *key_name* from `$HERMES_HOME/.env`, environment first.

    Never overrides a value already present in `os.environ`: the environment is
    what Hermes itself populated, and a stale file must not win over it.
    """
    existing = os.environ.get(key_name, "").strip()
    if existing:
        return existing
    env_path = Path(hermes_home) / ".env"
    if not env_path.exists():
        return ""
    try:
        for line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            if k.strip() != key_name:
                continue
            return v.strip().strip('"').strip("'")
    except Exception as exc:  # a broken .env must not disable extraction
        logger.warning("llm-endpoint: .env read failed (%s): %s", env_path, exc)
    return ""


def _host_of(base_url: str) -> str:
    if not base_url:
        return ""
    try:
        from urllib.parse import urlparse
        return (urlparse(base_url).hostname or "").lower()
    except Exception:
        return ""


def _key_matches_base_url(key_name: str, base_url: str) -> bool:
    """True when *key_name*'s provider is the one *base_url* points at.

    The comparison is exact or by domain suffix: the host must *be* the marker
    or a subdomain of it. A substring test would accept
    `api.openai.com.attacker.example` (the marker appears in the middle of the
    host) and hand a real key to a foreign endpoint — the exact leak this
    module exists to prevent.

    An unknown key name (a custom provider's own variable) never matches — the
    configured pair from `config.yaml` is the only authority for that case.
    """
    markers = _PROVIDER_HOSTS.get(key_name)
    if not markers:
        return False
    host = _host_of(base_url)
    if not host:
        return False
    return any(host == marker or host.endswith("." + marker) for marker in markers)


def resolve_llm_config(hermes_home: str) -> Dict[str, str]:
    """Read the extraction model config from Hermes `config.yaml` and the env.

    Returns ``{"model": …, "base_url": …, "api_key": …}``. The key always
    belongs to the returned `base_url`: either the pair configured together in
    `config.yaml`, or a provider key whose endpoint matches the configured
    `base_url`, or the local Ollama defaults.
    """
    config: Dict[str, str] = {"model": "", "base_url": "", "api_key": ""}

    config_path = Path(hermes_home) / "config.yaml"
    try:
        import yaml
        with open(config_path) as f:
            cfg = yaml.safe_load(f) or {}

        model = cfg.get("model", {}) or {}
        config["model"] = model.get("default", "")
        config["base_url"] = model.get("base_url", "")
        config["api_key"] = model.get("api_key", "")

        provider = model.get("provider", "")
        if provider and provider.startswith("custom:"):
            provider_name = provider[7:]
            providers = cfg.get("providers", {}) or {}
            if provider_name in providers:
                p = providers[provider_name] or {}
                if not config["base_url"]:
                    config["base_url"] = p.get("base_url", "")
                if not config["api_key"]:
                    config["api_key"] = p.get("api_key", "")
    except Exception as exc:
        logger.warning("llm-endpoint: config read failed (%s): %s", config_path, exc)

    # Endpoint first: the local Ollama endpoint when config.yaml names none.
    if not config["base_url"]:
        config["base_url"] = _LOCAL_BASE_URL

    # Then the key. Only a key whose provider matches the endpoint is eligible:
    # an `OLLAMA_API_KEY` is used for a local/ollama endpoint, an
    # `OPENAI_API_KEY` only for an OpenAI endpoint — a mismatched key is never
    # paired with a foreign base_url. The placeholder below is NOT a secret; it
    # only satisfies clients that insist on an api_key.
    rejected: list[str] = []
    if not config["api_key"]:
        for key_name in ("OLLAMA_API_KEY", "OPENAI_API_KEY"):
            if not _key_matches_base_url(key_name, config["base_url"]):
                # Present but issued for a different endpoint. Collected, not
                # logged yet: a later entry in the loop may still resolve the
                # key, and this runs on every extraction — a warning per call
                # would be log spam, not a mismatch report.
                if _env_file_key(hermes_home, key_name):
                    rejected.append(key_name)
                continue
            candidate = _env_file_key(hermes_home, key_name)
            if candidate:
                config["api_key"] = candidate
                break
    if not config["api_key"]:
        # Nothing resolved. Only here is a rejected key the reason, so only
        # here is it worth saying: otherwise this surfaces downstream as a
        # generic auth failure and reads like an unrelated outage.
        for key_name in rejected:
            logger.warning(
                "llm-endpoint: ignoring %s — not issued for %s",
                key_name, config["base_url"])
        config["api_key"] = _LOCAL_API_KEY
    if not config["model"]:
        config["model"] = _DEFAULT_MODEL

    return config
