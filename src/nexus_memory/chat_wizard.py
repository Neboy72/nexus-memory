#!/usr/bin/env python3
"""Nexus Memory — Chat-Based Onboarding Wizard.

This module provides a non-interactive API for chat-based installation.
Instead of terminal input()/output, it returns JSON that an agent
can display in chat (Telegram, Discord, WhatsApp, etc.).

Usage by an agent:
    1. Call scan_providers() → get available providers as JSON
    2. Display to user in chat
    3. User picks a number
    4. Call apply_choice(provider_id, api_key?) → save config
    5. Call get_trust_level_options() → get trust-level choices as JSON
    6. User picks a level
    7. Call save_trust_level(level) → save to config

Two modes:
    - "scan": Return provider list for display
    - "apply": Save the chosen provider + API key
    - "trust": Return trust-level options
    - "save_trust": Save trust-level choice
    - "status": Return current config status

Run standalone:
    python3 chat_wizard.py scan
    python3 chat_wizard.py apply voyage vo-xxx
    python3 chat_wizard.py trust
    python3 chat_wizard.py save_trust trusted
    python3 chat_wizard.py status
"""

from __future__ import annotations

import json
import os
import sys
import subprocess
from pathlib import Path
from typing import Optional

# Reuse provider definitions from wizard.py
try:
    from nexus_memory.wizard import PROVIDERS, _check_ollama, _check_sentence_transformers, _check_pip_package
except ImportError:
    # Fallback if running standalone
    PROVIDERS = [
        {"id": "voyage", "name": "Voyage AI", "dims": 1024, "quality": "excellent", "type": "cloud", "key_url": "https://dash.voyageai.com/api-keys", "key_env": "VOYAGE_API_KEY", "icon": "☁️", "pip_package": "voyageai"},
        {"id": "openai", "name": "OpenAI", "dims": 1536, "quality": "excellent", "type": "cloud", "key_url": "https://platform.openai.com/api-keys", "key_env": "OPENAI_API_KEY", "icon": "☁️", "pip_package": "openai"},
        {"id": "google", "name": "Google / Vertex AI", "dims": 768, "quality": "good", "type": "cloud", "key_url": "https://aistudio.google.com/apikey", "key_env": "GOOGLE_API_KEY", "icon": "💚", "pip_package": "google-generativeai"},
        {"id": "jina", "name": "Jina", "dims": 1024, "quality": "good", "type": "cloud", "key_url": "https://jina.ai/platform/embeddings", "key_env": "JINA_API_KEY", "icon": "💜", "pip_package": None},
        {"id": "ollama", "name": "Ollama (bge-m3, lokal)", "dims": 1024, "quality": "good", "type": "local", "key_url": "https://ollama.com/download", "key_env": None, "icon": "🦙", "pip_package": None},
        {"id": "local", "name": "sentence-transformers", "dims": 384, "quality": "basic", "type": "local", "key_url": "", "key_env": None, "icon": "🏠", "pip_package": "sentence-transformers"},
    ]

    def _check_ollama():
        try:
            import requests
            r = requests.get("http://localhost:11434/api/tags", timeout=2)
            if r.status_code < 400:
                models = [m["name"] for m in r.json().get("models", [])]
                emb_model = next((m for m in models if "embed" in m.lower()), None)
                if emb_model:
                    return True, emb_model
        except Exception:
            pass
        return False, ""

    def _check_sentence_transformers():
        try:
            from sentence_transformers import SentenceTransformer
            return True
        except ImportError:
            return False

    def _check_pip_package(package):
        if package is None:
            return True
        import pkgutil
        import_map = {"voyageai": "voyageai", "openai": "openai", "google-generativeai": "google.generativeai", "sentence-transformers": "sentence_transformers"}
        import_name = import_map.get(package, package.replace("-", "_"))
        try:
            return pkgutil.find_loader(import_name) is not None
        except Exception:
            return False

QUALITY_ORDER = {"excellent": 0, "good": 1, "basic": 2}
TYPE_ORDER = {"cloud": 0, "local": 1}

# Trust levels
TRUST_LEVELS = [
    {
        "id": "public",
        "name": "Public",
        "icon": "🟢",
        "description": "Only general facts (project info, tech stack, docs). No personal or setup data.",
        "recommended_for": "Untrusted agents, public demos, shared environments"
    },
    {
        "id": "trusted",
        "name": "Trusted",
        "icon": "🟡",
        "description": "Public facts + setup details (GitHub username, server config, timezone, tool preferences). No personal data.",
        "recommended_for": "Coding assistants you trust (Claude Code, Codex, Cursor)"
    },
    {
        "id": "private",
        "name": "Private",
        "icon": "🔴",
        "description": "Everything including personal data (family, expenses, documents, personal preferences).",
        "recommended_for": "Your primary agent only (Hermes, OpenClaw owner)"
    },
]


def _get_config_dir() -> Path:
    return Path.home() / ".nexus-memory"


def _get_config_path() -> Path:
    return _get_config_dir() / "config.json"


def _get_env_path() -> Path:
    return _get_config_dir() / ".env"


def _load_config() -> dict:
    path = _get_config_path()
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            pass
    return {}


def _save_config(config: dict) -> None:
    config_dir = _get_config_dir()
    config_dir.mkdir(parents=True, exist_ok=True)
    _get_config_path().write_text(json.dumps(config, indent=2) + "\n")


def _save_api_key(key_env: str, api_key: str) -> None:
    config_dir = _get_config_dir()
    config_dir.mkdir(parents=True, exist_ok=True)
    env_path = _get_env_path()
    existing = {}
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, _, v = line.partition("=")
                existing[k.strip()] = v.strip().strip('"').strip("'")
    existing[key_env] = api_key
    lines = [f'{k}="{v}"' for k, v in existing.items()]
    env_path.write_text("\n".join(lines) + "\n")


def _install_pip(package: str) -> bool:
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", package, "--quiet"],
            capture_output=True, text=True, timeout=120
        )
        return result.returncode == 0
    except Exception:
        return False


def scan_providers() -> dict:
    """Scan system for available embedding providers. Returns JSON for chat display."""
    results = []
    for p in PROVIDERS:
        available = False
        key_detected = False
        ollama_model = ""
        needs_install = False

        if p["id"] == "voyage":
            key = os.environ.get("VOYAGE_API_KEY", "")
            key_detected = bool(key and (key.startswith("vo-") or key.startswith("pa-")))
            available = key_detected and _check_pip_package("voyageai")
            needs_install = key_detected and not _check_pip_package("voyageai")
        elif p["id"] == "openai":
            key = os.environ.get("OPENAI_API_KEY", "")
            key_detected = bool(key and key.startswith("sk-"))
            available = key_detected and _check_pip_package("openai")
            needs_install = key_detected and not _check_pip_package("openai")
        elif p["id"] == "google":
            key = os.environ.get("GOOGLE_API_KEY", "")
            key_detected = bool(key and key.startswith("AIza"))
            available = key_detected and _check_pip_package("google-generativeai")
            needs_install = key_detected and not _check_pip_package("google-generativeai")
        elif p["id"] == "jina":
            key = os.environ.get("JINA_API_KEY", "")
            key_detected = bool(key)
            available = key_detected
        elif p["id"] == "ollama":
            available, ollama_model = _check_ollama()
        elif p["id"] == "local":
            available = _check_sentence_transformers()
            needs_install = not available

        results.append({
            "id": p["id"],
            "name": p["name"],
            "icon": p["icon"],
            "dims": p["dims"],
            "quality": p["quality"],
            "type": p["type"],
            "available": available,
            "key_detected": key_detected,
            "needs_install": needs_install,
            "ollama_model": ollama_model,
            "key_url": p["key_url"],
            "key_env": p["key_env"],
            "needs_api_key": p["key_env"] is not None and not key_detected,
        })

    # Find recommended (best available)
    recommended_idx = 0
    for quality in ["excellent", "good", "basic"]:
        for ptype in ["cloud", "local"]:
            for i, r in enumerate(results):
                if r["quality"] == quality and r["type"] == ptype and r["available"]:
                    recommended_idx = i
                    break
            else:
                continue
            break
        else:
            continue
        break

    # If nothing available, recommend ollama (easiest to set up)
    if not any(r["available"] for r in results):
        for i, r in enumerate(results):
            if r["id"] == "ollama":
                recommended_idx = i
                break

    return {
        "step": "embedding_provider",
        "title": "Nexus Memory Setup - Embedding Provider",
        "providers": results,
        "recommended_index": recommended_idx,
        "instructions": "Reply with the number of your choice (1-N). If the provider needs an API key, include it: '3 vo-your-key-here'"
    }


def apply_choice(provider_id: str, api_key: str = None) -> dict:
    """Apply the user's embedding provider choice. Saves config + installs deps."""
    provider = next((p for p in PROVIDERS if p["id"] == provider_id), None)
    if not provider:
        return {"error": f"Unknown provider: {provider_id}"}

    # Install pip package if needed
    if provider["pip_package"] and not _check_pip_package(provider["pip_package"]):
        success = _install_pip(provider["pip_package"])
        if not success:
            return {"error": f"Failed to install {provider['pip_package']}"}

    # Save API key if provided
    if api_key and provider["key_env"]:
        _save_api_key(provider["key_env"], api_key)
        os.environ[provider["key_env"]] = api_key

    # Save config
    config = _load_config()
    config["embedding_provider"] = provider_id
    _save_config(config)

    return {
        "step": "embedding_applied",
        "provider": provider_id,
        "name": provider["name"],
        "dims": provider["dims"],
        "quality": provider["quality"],
        "status": "configured",
        "message": f"Embedding provider set to {provider['name']} ({provider['dims']}d, {provider['quality']})."
    }


def get_trust_levels() -> dict:
    """Return trust-level options for chat display."""
    config = _load_config()
    current_level = config.get("trust_level", None)

    return {
        "step": "trust_level",
        "title": "Nexus Memory - Trust Level",
        "description": "Which memories may this agent read?",
        "levels": TRUST_LEVELS,
        "current": current_level,
        "instructions": "Reply with 1, 2, or 3."
    }


def save_trust_level(level_id: str) -> dict:
    """Save the trust-level choice to config."""
    valid_levels = [l["id"] for l in TRUST_LEVELS]
    if level_id not in valid_levels:
        return {"error": f"Invalid trust level: {level_id}. Must be one of: {valid_levels}"}

    config = _load_config()
    config["trust_level"] = level_id
    _save_config(config)

    level = next(l for l in TRUST_LEVELS if l["id"] == level_id)
    return {
        "step": "trust_level_saved",
        "level": level_id,
        "name": level["name"],
        "icon": level["icon"],
        "status": "configured",
        "message": f"Trust level set to {level['name']}."
    }


def get_status() -> dict:
    """Return current Nexus Memory configuration status."""
    config = _load_config()
    env_path = _get_env_path()

    # Check Qdrant
    qdrant_running = False
    try:
        import urllib.request
        req = urllib.request.Request("http://localhost:6333/health", headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=2) as resp:
            qdrant_running = resp.status == 200
    except Exception:
        pass

    # Check which API keys are set
    api_keys = {}
    for p in PROVIDERS:
        if p["key_env"]:
            key = os.environ.get(p["key_env"], "")
            api_keys[p["id"]] = bool(key)

    return {
        "step": "status",
        "embedding_provider": config.get("embedding_provider", "not set"),
        "trust_level": config.get("trust_level", "not set"),
        "qdrant_running": qdrant_running,
        "api_keys_detected": api_keys,
        "config_path": str(_get_config_path()),
        "env_path": str(_get_env_path()),
    }


def get_qdrant_filter_for_trust_level(level_id: str) -> dict:
    """Return a Qdrant filter dict for the given trust level.

    Used by plugins (Claude Code, OpenClaw) to filter memories by access level.
    """
    level_order = ["public", "trusted", "private"]
    if level_id not in level_order:
        level_id = "public"  # Safe default

    idx = level_order.index(level_id)
    allowed = level_order[:idx + 1]  # Include all levels up to and including chosen

    return {
        "should": [
            {"key": "access_level", "match": {"value": lvl}}
            for lvl in allowed
        ]
    }


# ── CLI Entry Point ──────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        print(json.dumps({"error": "Usage: chat_wizard.py [scan|apply|trust|save_trust|status] [args...]"}))
        sys.exit(1)

    command = sys.argv[1]

    if command == "scan":
        result = scan_providers()
    elif command == "apply":
        if len(sys.argv) < 3:
            print(json.dumps({"error": "Usage: chat_wizard.py apply <provider_id> [api_key]"}))
            sys.exit(1)
        provider_id = sys.argv[2]
        api_key = sys.argv[3] if len(sys.argv) > 3 else None
        result = apply_choice(provider_id, api_key)
    elif command == "trust":
        result = get_trust_levels()
    elif command == "save_trust":
        if len(sys.argv) < 3:
            print(json.dumps({"error": "Usage: chat_wizard.py save_trust <level_id>"}))
            sys.exit(1)
        result = save_trust_level(sys.argv[2])
    elif command == "status":
        result = get_status()
    elif command == "filter":
        # Returns Qdrant filter for a trust level (used by plugins)
        if len(sys.argv) < 3:
            print(json.dumps({"error": "Usage: chat_wizard.py filter <level_id>"}))
            sys.exit(1)
        result = get_qdrant_filter_for_trust_level(sys.argv[2])
    else:
        result = {"error": f"Unknown command: {command}"}

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()