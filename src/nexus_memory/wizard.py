#!/usr/bin/env python3
"""Nexus Memory — Interactive Embedding Setup Wizard.

Run: python3 -m nexus_memory.wizard
  or: nexus-memory-init (after installing with pip install -e .)

This wizard scans the system for available embedding providers,
shows quality rankings, lets the user choose, asks for API keys,
installs dependencies, saves config, and verifies the setup.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# ── ANSI color helpers ──────────────────────────────────────────────────
RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
GREEN = "\033[0;32m"
YELLOW = "\033[1;33m"
RED = "\033[0;31m"
CYAN = "\033[0;36m"
BLUE = "\033[0;34m"
MAGENTA = "\033[0;35m"
WHITE_BOLD = "\033[1;37m"

# ── Provider definitions ───────────────────────────────────────────────
PROVIDERS = [
    {
        "id": "voyage",
        "name": "Voyage AI",
        "dims": 1024,
        "quality": "excellent",
        "type": "cloud",
        "key_url": "https://dash.voyageai.com/api-keys",
        "key_env": "VOYAGE_API_KEY",
        "icon": "☁️",
        "pip_package": "voyageai",
    },
    {
        "id": "openai",
        "name": "OpenAI",
        "dims": 1536,
        "quality": "excellent",
        "type": "cloud",
        "key_url": "https://platform.openai.com/api-keys",
        "key_env": "OPENAI_API_KEY",
        "icon": "☁️",
        "pip_package": "openai",
    },
    {
        "id": "google",
        "name": "Google / Vertex AI",
        "dims": 768,
        "quality": "good",
        "type": "cloud",
        "key_url": "https://aistudio.google.com/apikey",
        "key_env": "GOOGLE_API_KEY",
        "icon": "💚",
        "pip_package": "google-generativeai",
    },
    {
        "id": "jina",
        "name": "Jina",
        "dims": 1024,
        "quality": "good",
        "type": "cloud",
        "key_url": "https://jina.ai/platform/embeddings",
        "key_env": "JINA_API_KEY",
        "icon": "💜",
        "pip_package": None,  # uses requests (already a dep)
    },
    {
        "id": "ollama",
        "name": "Ollama",
        "dims": 768,
        "quality": "good",
        "type": "local",
        "key_url": "https://ollama.com/download",
        "key_env": None,
        "icon": "🦙",
        "pip_package": None,  # uses requests
    },
    {
        "id": "local",
        "name": "sentence-transformers",
        "dims": 384,
        "quality": "basic",
        "type": "local",
        "key_url": "",
        "key_env": None,
        "icon": "🏠",
        "pip_package": "sentence-transformers",
    },
]

QUALITY_ORDER = {"excellent": 0, "good": 1, "basic": 2}
TYPE_ORDER = {"cloud": 0, "local": 1}


@dataclass
class ProviderStatus:
    provider: dict
    available: bool = False
    key_detected: bool = False
    ollama_model: str = ""


# ── Helpers ────────────────────────────────────────────────────────────


def _print(text: str = "", end: str = "\n") -> None:
    """Print to stdout, flushing to work reliably across shells."""
    if text:
        sys.stdout.write(text + end)
    else:
        sys.stdout.write(end)
    sys.stdout.flush()


def _input(prompt: str) -> str:
    """Prompt user for input, with graceful Ctrl+C handling."""
    _print(prompt, end="")
    try:
        return input()
    except KeyboardInterrupt:
        _print(f"\n\n{YELLOW}Setup cancelled.{RESET}")
        sys.exit(0)
    except EOFError:
        _print(f"\n\n{YELLOW}Input stream closed — exiting.{RESET}")
        sys.exit(0)


def _confirm(prompt: str, default: bool = True) -> bool:
    """Ask a yes/no question."""
    suffix = " [Y/n]: " if default else " [y/N]: "
    answer = _input(prompt + suffix).strip().lower()
    if not answer:
        return default
    return answer.startswith("y")


def _run_pip(package: str) -> bool:
    """Install a pip package. Returns True on success."""
    _print(f"\n  {CYAN}Installing {package}...{RESET}")
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", package, "--quiet"],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode == 0:
            return True
        else:
            # Fallback: try without --quiet to see errors
            _print(f"  {YELLOW}Standard install failed, retrying...{RESET}")
            subprocess.run(
                [sys.executable, "-m", "pip", "install", package],
                timeout=120,
            )
            return True
    except subprocess.TimeoutExpired:
        _print(f"  {RED}Install timed out for {package}{RESET}")
        return False
    except Exception as e:
        _print(f"  {RED}Install failed: {e}{RESET}")
        return False


def _run_cmd(cmd: list[str], timeout: int = 60) -> tuple[int, str, str]:
    """Run a command, return (returncode, stdout, stderr)."""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return -1, "", "timed out"
    except FileNotFoundError:
        return -1, "", "command not found"


# ── Detection ──────────────────────────────────────────────────────────


def _check_ollama() -> tuple[bool, str]:
    """Check if Ollama is running and has an embed model. Returns (available, model_name)."""
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


def _check_sentence_transformers() -> bool:
    """Check if sentence-transformers is installed and working."""
    try:
        from sentence_transformers import SentenceTransformer
        # Don't actually load the model (slow), just check import
        return True
    except ImportError:
        return False
    except Exception:
        return False


def _check_pip_package(package: str) -> bool:
    """Check if a Python package is installed."""
    if package is None:
        return True
    # Map pip package name to import name
    import_map = {
        "voyageai": "voyageai",
        "openai": "openai",
        "google-generativeai": "google.generativeai",
        "sentence-transformers": "sentence_transformers",
    }
    import_name = import_map.get(package, package.replace("-", "_"))
    try:
        # Use pkgutil to avoid actually importing
        import pkgutil
        return pkgutil.find_loader(import_name) is not None
    except Exception:
        return False


def _scan_providers() -> list[ProviderStatus]:
    """Scan the system and return provider statuses."""
    results = []

    for p in PROVIDERS:
        ps = ProviderStatus(provider=p)

        if p["id"] == "voyage":
            key = os.environ.get("VOYAGE_API_KEY", "")
            ps.key_detected = bool(key and (key.startswith("vo-") or key.startswith("pa-")))
            ps.available = ps.key_detected and _check_pip_package("voyageai")

        elif p["id"] == "openai":
            key = os.environ.get("OPENAI_API_KEY", "")
            ps.key_detected = bool(key and key.startswith("sk-"))
            ps.available = ps.key_detected and _check_pip_package("openai")

        elif p["id"] == "google":
            key = os.environ.get("GOOGLE_API_KEY", "")
            ps.key_detected = bool(key and key.startswith("AIza"))
            ps.available = ps.key_detected and _check_pip_package("google-generativeai")

        elif p["id"] == "jina":
            key = os.environ.get("JINA_API_KEY", "")
            ps.key_detected = bool(key)
            ps.available = ps.key_detected

        elif p["id"] == "ollama":
            avail, model = _check_ollama()
            ps.available = avail
            ps.ollama_model = model

        elif p["id"] == "local":
            ps.available = _check_sentence_transformers()

        results.append(ps)

    return results


def _find_recommended(statuses: list[ProviderStatus]) -> int:
    """Find the best available provider index. Returns 0-based index."""
    # Priority: excellent > good > basic, cloud > local
    for quality in ["excellent", "good", "basic"]:
        for provider_type in ["cloud", "local"]:
            for i, ps in enumerate(statuses):
                if (
                    ps.provider["quality"] == quality
                    and ps.provider["type"] == provider_type
                    and ps.available
                ):
                    return i

    # If nothing available, recommend local (will prompt to install)
    for i, ps in enumerate(statuses):
        if ps.provider["id"] == "local":
            return i

    return 0  # fallback


# ── Config persistence ─────────────────────────────────────────────────


def _get_config_dir() -> Path:
    """Get the config directory for Nexus Memory."""
    return Path.home() / ".nexus-memory"


def _get_env_file() -> Path:
    """Get the .env file path."""
    return _get_config_dir() / ".env"


def _save_config(provider_id: str) -> None:
    """Save the provider choice to config.json."""
    config_dir = _get_config_dir()
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "config.json"

    config = {}
    if config_path.exists():
        try:
            config = json.loads(config_path.read_text())
        except Exception:
            pass

    config["embedding_provider"] = provider_id
    config_path.write_text(json.dumps(config, indent=2) + "\n")
    _print(f"  {GREEN}✓{RESET} Config saved: {config_path}")


def _save_api_key(key_env: str, api_key: str) -> None:
    """Save an API key to the .env file."""
    config_dir = _get_config_dir()
    config_dir.mkdir(parents=True, exist_ok=True)
    env_path = _get_env_file()

    # Read existing .env content
    existing = {}
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, _, v = line.partition("=")
                existing[k.strip()] = v.strip().strip('"').strip("'")

    # Update/add the key
    existing[key_env] = api_key

    # Write back
    lines = []
    for k, v in existing.items():
        lines.append(f'{k}="{v}"')
    env_path.write_text("\n".join(lines) + "\n")
    _print(f"  {GREEN}✓{RESET} API key saved: {env_path}")


def _set_env_var(key: str, value: str) -> None:
    """Set an environment variable in the current process."""
    os.environ[key] = value


# ── Verification ───────────────────────────────────────────────────────


def _verify_embedding(provider_id: str, provider_name: str, dims: int, quality: str) -> bool:
    """Verify that the embedding provider works by embedding a test string."""
    _print(f"\n  {CYAN}Verifying embedding setup...{RESET}")

    try:
        # Use the EmbeddingProvider from embeddings.py
        from nexus_memory.embeddings import EmbeddingProvider

        provider = EmbeddingProvider(preferred=provider_id)
        if not provider.available:
            _print(f"  {RED}✗ Provider '{provider_id}' is not available.{RESET}")
            _print(f"  {YELLOW}  Check that the API key is valid and the package is installed.{RESET}")
            return False

        # Try to embed a test string
        import asyncio

        async def _test():
            text = "Hello, Nexus Memory embedding setup!"
            vec = await provider.embed(text)
            if vec and len(vec) > 0:
                return True
            return False

        loop = asyncio.new_event_loop()
        try:
            success = loop.run_until_complete(_test())
        finally:
            loop.close()

        if success:
            _print(f"\n  {GREEN}✅ Embedding configured: {provider_name} ({dims}d, {quality}){RESET}")
            return True
        else:
            _print(f"  {RED}✗ Embedding returned empty result.{RESET}")
            return False

    except ImportError as e:
        _print(f"  {RED}✗ Import error: {e}{RESET}")
        _print(f"  {YELLOW}  Make sure the required package is installed.{RESET}")
        return False
    except Exception as e:
        _print(f"  {RED}✗ Verification failed: {e}{RESET}")
        return False


# ── Display ─────────────────────────────────────────────────────────────


def _display_header() -> None:
    """Display the wizard header."""
    _print()
    _print(f"{BOLD}{WHITE_BOLD}Nexus Memory — Embedding Setup{RESET}")
    _print(f"{DIM}═══════════════════════════════════════════════{RESET}")
    _print()


def _display_provider_list(statuses: list[ProviderStatus], recommended_idx: int) -> None:
    """Display the numbered provider list with status indicators."""
    _print(f"{BOLD}Detecting available providers...{RESET}")
    _print()

    # Sort: excellent cloud > excellent local > good cloud > good local > basic
    def sort_key(ps: ProviderStatus) -> tuple:
        p = ps.provider
        return (QUALITY_ORDER.get(p["quality"], 99), TYPE_ORDER.get(p["type"], 99))

    sorted_statuses = sorted(enumerate(statuses), key=lambda x: sort_key(x[1]))

    for display_num, (orig_idx, ps) in enumerate(sorted_statuses, 1):
        p = ps.provider
        icon = p["icon"]
        name = p["name"]
        dims = p["dims"]
        quality = p["quality"]

        # Build status
        if p["id"] == "ollama":
            if ps.available:
                status = f"{GREEN}✓{RESET} {ps.ollama_model} detected"
            else:
                status = f"{YELLOW}-{RESET} not running or no embed model"
        elif p["id"] == "local":
            if ps.available:
                status = f"{GREEN}✓{RESET} installed"
            else:
                status = f"{YELLOW}-{RESET} not installed"
        else:
            if ps.key_detected:
                status = f"{GREEN}✓{RESET} API key detected"
            else:
                status = f"{DIM}-{RESET} no key"

        # Highlight recommended
        marker = f"  {BOLD}{CYAN}← Recommended{RESET}" if orig_idx == recommended_idx else ""

        _print(f"  [{display_num}] {icon} {BOLD}{name:<22}{RESET} {DIM}{dims}d  {quality:<9}{RESET}  {status}{marker}")

    _print()


def _get_user_selection(statuses: list[ProviderStatus], recommended_idx: int) -> ProviderStatus:
    """Get the user's provider selection."""
    # Build mapping: display_num -> original index
    sorted_indices = sorted(
        range(len(statuses)),
        key=lambda i: (
            QUALITY_ORDER.get(statuses[i].provider["quality"], 99),
            TYPE_ORDER.get(statuses[i].provider["type"], 99),
        ),
    )
    display_to_idx = {str(dn): oi for dn, oi in enumerate(sorted_indices, 1)}
    # Also map provider IDs and names to indices
    name_to_idx = {}
    for oi, ps in enumerate(statuses):
        name_to_idx[ps.provider["id"]] = oi
        name_to_idx[ps.provider["name"].lower()] = oi
        name_to_idx[ps.provider["name"].lower().split("/")[0].strip()] = oi

    # Default is recommended
    default_display = next(
        (str(dn) for dn, oi in display_to_idx.items() if oi == recommended_idx),
        "1",
    )

    rec_name = statuses[recommended_idx].provider["name"]
    prompt = f"\n  Select provider [{default_display}] (or name/id): "

    while True:
        choice = _input(prompt).strip().lower()

        if not choice:
            return statuses[recommended_idx]

        # Try display number
        if choice in display_to_idx:
            return statuses[display_to_idx[choice]]

        # Try name/id
        if choice in name_to_idx:
            return statuses[name_to_idx[choice]]

        _print(f"  {YELLOW}Invalid selection. Enter a number, name, or press Enter for default.{RESET}")
        prompt = f"  Select provider [{default_display}]: "


# ── Provider setup steps ───────────────────────────────────────────────


def _setup_cloud_provider(ps: ProviderStatus) -> str | None:
    """Set up a cloud provider. Returns the API key or None if skipped."""
    p = ps.provider
    key_env = p["key_env"]
    key_url = p["key_url"]
    provider_name = p["name"]

    if not ps.key_detected:
        _print(f"\n  {BOLD}Get your API key at: {CYAN}{key_url}{RESET}")
        prompt = f"  Enter your {provider_name} API key (or press Enter to skip and use auto-detect): "
        api_key = _input(prompt).strip()

        if api_key:
            _save_api_key(key_env, api_key)
            _set_env_var(key_env, api_key)
            return api_key
        else:
            _print(f"  {YELLOW}Skipping API key setup — will use auto-detect at runtime.{RESET}")
            # Try existing env var again
            existing = os.environ.get(key_env, "")
            return existing if existing else None
    else:
        # Key already detected
        existing = os.environ.get(key_env, "")
        _print(f"\n  {GREEN}✓{RESET} Using existing {key_env} from environment.")
        return existing


def _setup_ollama(ps: ProviderStatus) -> bool:
    """Set up Ollama. Prompt to install an embed model if needed."""
    if ps.available:
        _print(f"\n  {GREEN}✓{RESET} Ollama is running with embed model: {ps.ollama_model}")
        return True

    _print(f"\n  {YELLOW}⚠{RESET} Ollama is not running or no embedding model found.")
    _print(f"  Install an embedding model: {CYAN}ollama pull nomic-embed-text{RESET}")

    if _confirm(f"  Install nomic-embed-text now?"):
        _print(f"\n  {CYAN}Pulling nomic-embed-text...{RESET}")
        ret, out, err = _run_cmd(["ollama", "pull", "nomic-embed-text"], timeout=300)
        if ret == 0:
            _print(f"  {GREEN}✓{RESET} nomic-embed-text installed successfully.")
            return True
        else:
            _print(f"  {RED}✗{RESET} Failed to pull model. Is Ollama installed?")
            _print(f"  Download: {CYAN}{ps.provider['key_url']}{RESET}")
            return False
    else:
        _print(f"  {YELLOW}Skipping Ollama setup. You can install later:{RESET}")
        _print(f"    {CYAN}ollama pull nomic-embed-text{RESET}")
        return True  # Not a hard failure


def _setup_local(ps: ProviderStatus) -> bool:
    """Set up sentence-transformers. Install if needed."""
    if ps.available:
        _print(f"\n  {GREEN}✓{RESET} sentence-transformers is already installed.")
        return True

    _print(f"\n  {YELLOW}⚠{RESET} sentence-transformers is not installed.")
    if _confirm(f"  Install sentence-transformers now?"):
        success = _run_pip("sentence-transformers")
        if success:
            _print(f"  {GREEN}✓{RESET} sentence-transformers installed.")
            return True
        else:
            _print(f"  {RED}✗{RESET} Failed to install sentence-transformers.")
            return False
    else:
        _print(f"  {YELLOW}Skipping. Install manually: pip install sentence-transformers{RESET}")
        return True  # Not a hard failure


# ── Main workflow ──────────────────────────────────────────────────────


def _install_pip_package(ps: ProviderStatus) -> bool:
    """Install the required pip package for the provider."""
    pkg = ps.provider.get("pip_package")
    if pkg is None:
        return True  # No extra package needed

    if _check_pip_package(pkg):
        _print(f"\n  {GREEN}✓{RESET} {pkg} is already installed.")
        return True

    _print(f"\n  {YELLOW}⚠{RESET} Required package '{pkg}' is not installed.")
    return _run_pip(pkg)


def _show_next_steps() -> None:
    """Show next steps after successful setup."""
    _print()
    _print(f"{DIM}═══════════════════════════════════════════════{RESET}")
    _print(f"\n  {BOLD}{GREEN}Setup complete!{RESET} Next steps:")
    _print(f"  {CYAN}•{RESET} Start the MCP server: {BOLD}nexus-memory{RESET}")
    _print(f"  {CYAN}•{RESET} Or run {BOLD}hermes memory setup{RESET} if you use Hermes Agent")
    _print()


def main() -> None:
    """Run the interactive embedding setup wizard."""
    try:
        _run_wizard()
    except KeyboardInterrupt:
        _print(f"\n\n{YELLOW}Setup cancelled.{RESET}")
        sys.exit(0)


def _run_wizard() -> None:
    """Internal wizard logic, separated for clean import."""
    _display_header()

    # 1. Scan the system
    statuses = _scan_providers()

    # 2. Find the recommended provider
    recommended_idx = _find_recommended(statuses)

    # 3. Display the list
    _display_provider_list(statuses, recommended_idx)

    # 4. Get user selection
    selected = _get_user_selection(statuses, recommended_idx)
    provider_id = selected.provider["id"]
    provider_name = selected.provider["name"]
    dims = selected.provider["dims"]
    quality = selected.provider["quality"]

    _print(f"\n  {BOLD}Selected: {selected.provider['icon']} {provider_name}{RESET}")

    # 5. Provider-specific setup
    api_key = None
    if provider_id in ("voyage", "openai", "google", "jina"):
        api_key = _setup_cloud_provider(selected)
        if api_key is None:
            _print(f"  {YELLOW}⚠ No API key provided. The provider will use auto-detect at runtime.{RESET}")
    elif provider_id == "ollama":
        _setup_ollama(selected)
    elif provider_id == "local":
        if not _setup_local(selected):
            _print(f"  {RED}Failed to set up sentence-transformers. Exiting.{RESET}")
            sys.exit(1)

    # 6. Install pip package
    if not _install_pip_package(selected):
        _print(f"  {RED}Failed to install required package. Exiting.{RESET}")
        sys.exit(1)

    # 7. Save config
    _save_config(provider_id)

    # 8. Verify
    verified = _verify_embedding(provider_id, provider_name, dims, quality)

    if verified:
        _show_next_steps()
    else:
        _print(f"\n  {RED}✗ Verification failed. You may need to restart your terminal.{RESET}")
        _print(f"  {YELLOW}Run this wizard again or check the docs.{RESET}")
        sys.exit(1)


if __name__ == "__main__":
    main()
