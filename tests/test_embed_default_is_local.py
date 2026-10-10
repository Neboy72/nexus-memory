"""Regression guard: the LOCAL embedding path must stay the default.

R-EMBED-DEFAULT. This regressed twice, in two directions:

1. The plugins once shipped a **cloud-first** default (Voyage) in all three
   native integrations, so a user with no cloud key got no memory at all —
   while the docs claimed the setup was "private" and "self-hosted".
2. They then defaulted to a local **Ollama**, which is still extra software that
   not every machine has: a fresh Claude Code or OpenClaw install with nothing
   but the Nexus service had no way in — and if Ollama ever drops a model, an
   install that landed there moves to a different one.

The default is therefore the **local Nexus service**: it embeds with the
engine's own provider (local HuggingFace), so nothing has to be installed, no
text leaves the machine, and every agent shares one vector space. A cloud
provider stays an explicit opt-in; Ollama stays available for whoever names it.

These tests fail loudly if a default drifts back to cloud, or if the local path
loses its branch.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
CLAUDE_SCRIPTS = REPO_ROOT / "plugins" / "claude-code" / "scripts"
OPENCLAW_LIB = REPO_ROOT / "plugins" / "openclaw" / "lib"
CLOUD_PROVIDERS = ("voyage", "openai", "google", "jina")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _provider_default(path: Path) -> str:
    src = _read(path)
    match = re.search(r'NEXUS_EMBEDDING_PROVIDER"\s*,\s*"(\w+)"', src)
    assert match, f"no NEXUS_EMBEDDING_PROVIDER default found in {path.name}"
    return match.group(1)


class TestLocalIsTheDefaultProvider:
    """The Claude Code hooks must default to the local service, never cloud."""

    @pytest.mark.parametrize(
        "script", ["auto_capture.py", "auto_recall.py", "session_start.py"]
    )
    def test_hook_defaults_to_the_local_service(self, script: str) -> None:
        default = _provider_default(CLAUDE_SCRIPTS / script)
        assert default == "auto", (
            f"{script} defaults NEXUS_EMBEDDING_PROVIDER to {default!r}. The "
            "default must be 'auto' — the local Nexus service, which embeds "
            "with the engine's local HuggingFace model. A cloud provider is an "
            "explicit opt-in; otherwise a user with no cloud key gets a memory "
            "layer that silently stores nothing."
        )

    @pytest.mark.parametrize(
        "script", ["auto_capture.py", "auto_recall.py", "session_start.py", "self_check.py"]
    )
    def test_no_hook_defaults_to_a_cloud_provider(self, script: str) -> None:
        src = _read(CLAUDE_SCRIPTS / script)
        for cloud in CLOUD_PROVIDERS:
            assert f'"{cloud}"' not in re.findall(
                r'NEXUS_EMBEDDING_PROVIDER"\s*,\s*"(\w+)"', src
            ), f"{script} would default to the cloud provider {cloud!r}"

    def test_self_check_defaults_to_the_local_service(self) -> None:
        src = _read(CLAUDE_SCRIPTS / "self_check.py")
        match = re.search(r'^DEFAULT_PROVIDER\s*=\s*"(\w+)"', src, re.M)
        assert match, "self_check.py has no DEFAULT_PROVIDER"
        assert match.group(1) == "auto", (
            "self_check.py reports a non-service provider as the default; the "
            "health check would then warn about a missing key (or a missing "
            "Ollama) on a perfectly healthy local setup."
        )

    @pytest.mark.parametrize(
        "script", ["auto_capture.py", "auto_recall.py", "session_start.py"]
    )
    def test_hooks_ask_the_local_service(self, script: str) -> None:
        """Without this branch the automatic default has nowhere to go."""
        src = _read(CLAUDE_SCRIPTS / script)
        assert "embed_via_serve" in src, (
            f"{script} has no branch for the local service — the automatic "
            "default would then embed nothing at all."
        )

    def test_self_check_probes_the_local_service(self) -> None:
        src = _read(CLAUDE_SCRIPTS / "self_check.py")
        assert "serve_reachable" in src, (
            "self_check.py cannot tell whether the service it now depends on "
            "is up, so a broken setup would look healthy."
        )

    def test_the_shared_embedding_layer_exists_and_is_shared(self) -> None:
        src = _read(CLAUDE_SCRIPTS / "_embedding.py")
        assert "def embed_via_serve" in src and "def is_serve_provider" in src, (
            "_embedding.py is the one place the hooks agree on; if it loses "
            "these, each hook grows its own provider list again."
        )


class TestExplicitProvidersStillWork:
    """Choosing a provider must stay possible — it is just never automatic."""

    def test_hooks_keep_the_explicit_branches(self) -> None:
        for script in ("auto_capture.py", "auto_recall.py", "session_start.py"):
            src = _read(CLAUDE_SCRIPTS / script)
            assert re.search(r'EMBEDDING_PROVIDER\s*==\s*"ollama"', src), (
                f"{script} lost its Ollama branch — an explicit choice would "
                "silently stop working."
            )


class TestLocalModelDefault:
    """The explicit local model must stay multilingual and match our collection."""

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
    """OpenClaw must resolve to a local provider with no key present."""

    def test_detect_provider_resolves_to_the_local_service(self) -> None:
        src = _read(OPENCLAW_LIB / "embedder.ts")
        match = re.search(r"export function detectProvider\(\)[^{]*\{(.*?)\n\}", src, re.S)
        assert match, "detectProvider() not found in embedder.ts"
        body = match.group(1)
        assert 'return "nexus"' in body, (
            "detectProvider() must fall back to 'nexus' (the local Nexus "
            "service). Returning null makes the Embedder constructor throw "
            "'No embedding provider configured', so an OpenClaw user with no "
            "cloud key cannot use memory at all."
        )
        assert "return null" not in body, (
            "detectProvider() still returns null on the no-key path; that is "
            "the hard-failure mode this guard exists to prevent."
        )
        for cloud in CLOUD_PROVIDERS:
            assert f'return "{cloud}"' not in body, (
                f"detectProvider() falls back to the cloud provider {cloud!r}"
            )

    def test_openclaw_knows_the_local_service_and_its_endpoint(self) -> None:
        src = _read(OPENCLAW_LIB / "embedder.ts")
        assert "embedNexus" in src, "embedder.ts has no local-service path"
        assert "/embed" in src, "the local-service path does not call /embed"
        assert "nexus:" in src and "9122" in src, (
            "the nexus provider default (base URL) is missing"
        )
        config = _read(OPENCLAW_LIB / "config.ts")
        assert '"nexus"' in config, (
            "config.ts does not accept the 'nexus' provider, so the documented "
            "default would be rejected as invalid"
        )

    def test_ollama_stays_available_when_named(self) -> None:
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
    """The published skill card must describe the path the code takes."""

    def test_skill_doc_names_the_local_service_as_default(self) -> None:
        path = (
            REPO_ROOT / "plugins" / "claude-code" / "skills" / "nexus-memory" / "SKILL.md"
        )
        src = _read(path)
        assert re.search(r"`NEXUS_EMBEDDING_PROVIDER`\s*\|\s*`auto`", src), (
            "SKILL.md does not document 'auto' (the local Nexus service) as the "
            "default provider - the published card would mislead users about "
            "where their data goes."
        )
        assert "default is local" in src, (
            "SKILL.md must state plainly that the embedding default is local."
        )
