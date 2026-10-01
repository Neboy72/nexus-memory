"""Regression guard: the LOCAL embedding provider must stay the default.

R-EMBED-DEFAULT. This has regressed before: the plugins shipped a
cloud-first default (Voyage) in all three native integrations, so a user
with no cloud key got no memory at all - and the docs claimed the setup
was "private" and "self-hosted" while sending memory text to a cloud API.

These tests fail loudly if anyone switches a default back to a cloud
provider. Being a cloud user ourselves is NOT a reason to make cloud the
default for everyone: most users run a local embedding model, and the
whole point of a self-hosted memory layer is that it works with no
account, no key and no data leaving the machine.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
CLAUDE_SCRIPTS = REPO_ROOT / "plugins" / "claude-code" / "scripts"
OPENCLAW_LIB = REPO_ROOT / "plugins" / "openclaw" / "lib"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _provider_default(path: Path) -> str:
    src = _read(path)
    match = re.search(r'NEXUS_EMBEDDING_PROVIDER"\s*,\s*"(\w+)"', src)
    assert match, f"no NEXUS_EMBEDDING_PROVIDER default found in {path.name}"
    return match.group(1)


class TestLocalIsTheDefaultProvider:
    """The three Claude Code hooks must default to a local provider."""

    @pytest.mark.parametrize(
        "script", ["auto_capture.py", "auto_recall.py", "session_start.py"]
    )
    def test_hook_defaults_to_ollama(self, script: str) -> None:
        default = _provider_default(CLAUDE_SCRIPTS / script)
        assert default == "ollama", (
            f"{script} defaults NEXUS_EMBEDDING_PROVIDER to {default!r}. "
            "The default must be the LOCAL provider ('ollama'); a cloud "
            "provider is an explicit opt-in. Otherwise a user with no cloud "
            "key gets a memory layer that silently stores nothing."
        )

    def test_self_check_defaults_to_ollama(self) -> None:
        src = _read(CLAUDE_SCRIPTS / "self_check.py")
        match = re.search(r'^DEFAULT_PROVIDER\s*=\s*"(\w+)"', src, re.M)
        assert match, "self_check.py has no DEFAULT_PROVIDER"
        assert match.group(1) == "ollama", (
            "self_check.py reports a cloud provider as the default; the "
            "health check would then warn about a missing cloud key on a "
            "perfectly healthy local setup."
        )

    def test_session_start_has_a_local_embedding_path(self) -> None:
        """session_start.py once embedded via Voyage only.

        With a local default that made session recall fail silently: the
        hook had no branch for a keyless provider at all.
        """
        src = _read(CLAUDE_SCRIPTS / "session_start.py")
        assert re.search(r'EMBEDDING_PROVIDER\s*==\s*"ollama"', src), (
            "session_start.py has no Ollama branch - it can only embed via a "
            "cloud provider, so session recall breaks on the local default."
        )


class TestLocalModelDefault:
    """The default local model must be multilingual and match our collection."""

    @pytest.mark.parametrize("script", ["auto_capture.py", "auto_recall.py"])
    def test_ollama_model_default_is_qwen3(self, script: str) -> None:
        src = _read(CLAUDE_SCRIPTS / script)
        match = re.search(r'NEXUS_OLLAMA_EMBED_MODEL"\s*,\s*"([^"]+)"', src)
        assert match, f"no NEXUS_OLLAMA_EMBED_MODEL default in {script}"
        model = match.group(1)
        assert "qwen3-embedding" in model, (
            f"{script} defaults the local model to {model!r}. The default must "
            "be a multilingual model (qwen3-embedding:0.6b): the previous "
            "nomic-embed-text default is English-focused (768d) and does not "
            "match the 1024d collection used by the other integrations."
        )


class TestOpenClawDefaults:
    """OpenClaw must not hard-fail when no cloud key is present."""

    def test_detect_provider_falls_back_to_ollama(self) -> None:
        src = _read(OPENCLAW_LIB / "embedder.ts")
        match = re.search(r"export function detectProvider\(\)[^{]*\{(.*?)\n\}", src, re.S)
        assert match, "detectProvider() not found in embedder.ts"
        body = match.group(1)
        assert 'return "ollama"' in body, (
            "detectProvider() must fall back to 'ollama'. Returning null makes "
            "the Embedder constructor throw 'No embedding provider configured', "
            "so an OpenClaw user with no cloud key cannot use memory at all."
        )
        assert re.search(r"return null", body) is None, (
            "detectProvider() still returns null on the no-key path; that is "
            "the hard-failure mode this guard exists to prevent."
        )

    def test_ollama_default_model_is_qwen3(self) -> None:
        src = _read(OPENCLAW_LIB / "embedder.ts")
        match = re.search(
            r"ollama:\s*\{\s*model:\s*\"([^\"]+)\",\s*dimensions:\s*(\d+)", src
        )
        assert match, "no ollama entry in PROVIDER_DEFAULTS"
        model, dims = match.group(1), int(match.group(2))
        assert "qwen3-embedding" in model, (
            f"OpenClaw's ollama default model is {model!r}; expected a "
            "multilingual qwen3-embedding model."
        )
        assert dims == 1024, (
            f"OpenClaw's ollama default is {dims}d; the shared collection is "
            "1024d - a 768d default would fail the dimension check at runtime."
        )


class TestSkillDocsTellTheTruth:
    """The published skill card must not advertise a cloud default."""

    def test_skill_doc_names_local_as_default(self) -> None:
        path = (
            REPO_ROOT
            / "plugins"
            / "claude-code"
            / "skills"
            / "nexus-memory"
            / "SKILL.md"
        )
        src = _read(path)
        assert re.search(r"`NEXUS_EMBEDDING_PROVIDER`\s*\|\s*`ollama`", src), (
            "SKILL.md does not document ollama as the default provider - the "
            "published card would mislead users about where their data goes."
        )
        assert "default is local" in src, (
            "SKILL.md must state plainly that the embedding default is local."
        )
