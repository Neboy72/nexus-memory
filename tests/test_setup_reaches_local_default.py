"""Regression guard: the setup path must reach the local default.

R-WIZARD-REACH. The interactive wizard has always done the right thing -
scan the machine, show every provider, recommend the good local one, offer
to pull it. But nothing called it: the install scripts ship their own
copy-pasted detection instead. That copy was cloud-first, defaulted the
local model to the English-focused 768d nomic-embed-text, and hard-failed
with "no embedding provider detected" when the user had neither a cloud
key nor Ollama - which is exactly the user the local default exists for.

The lesson: a correct code path that nothing invokes is not a feature.
These tests guard BOTH halves - the wizard's recommendation logic and the
installer's detection - so a future edit cannot quietly undo either.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
WIZARD = REPO_ROOT / "src" / "nexus_memory" / "wizard.py"
INSTALL_OPENCLAW = REPO_ROOT / "scripts" / "install_openclaw_plugin.sh"
AGENTS_MD = REPO_ROOT / "AGENTS.md"
README_MD = REPO_ROOT / "README.md"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _detect_embedding_block() -> str:
    """The body of detect_embedding() in the installer script."""
    src = _read(INSTALL_OPENCLAW)
    match = re.search(
        r"detect_embedding\(\)\s*\{(.*?)\n\}", src, re.S
    )
    assert match, "detect_embedding() not found in install_openclaw_plugin.sh"
    return match.group(1)


class TestWizardRecommendsLocalWhenNothingIsInstalled:
    """A user with nothing installed must be pointed at the good local model."""

    @staticmethod
    def _statuses(available_ids=(), ollama_model=""):
        from nexus_memory.wizard import PROVIDERS, ProviderStatus

        out = []
        for provider in PROVIDERS:
            status = ProviderStatus(
                provider=provider, available=provider["id"] in available_ids
            )
            if ollama_model:
                status.ollama_model = ollama_model
            out.append(status)
        return out

    def test_empty_system_recommends_ollama_not_sentence_transformers(self):
        """The 384d fallback must never be the recommendation.

        It used to be: with nothing available the loop found no match and
        fell through to the sentence-transformers entry. That silently set
        up the worst local option for a user starting from scratch.
        """
        from nexus_memory.wizard import _find_recommended

        statuses = self._statuses()
        idx = _find_recommended(statuses)
        chosen = statuses[idx].provider["id"]
        assert chosen == "ollama", (
            f"with nothing installed the wizard recommends {chosen!r}; it must "
            "recommend 'ollama' (qwen3-embedding, 1024d, multilingual) and "
            "offer to pull it."
        )

    def test_local_ollama_recommended_over_basic_local(self):
        from nexus_memory.wizard import _find_recommended

        statuses = self._statuses(available_ids={"ollama", "local"})
        idx = _find_recommended(statuses)
        assert statuses[idx].provider["id"] == "ollama"

    def test_cloud_still_wins_when_the_user_has_a_key(self):
        """Cloud-first is fine once the user actually configured one."""
        from nexus_memory.wizard import _find_recommended

        statuses = self._statuses(available_ids={"voyage", "ollama"})
        idx = _find_recommended(statuses)
        assert statuses[idx].provider["id"] == "voyage", (
            "a user who already set a cloud key should still be offered it"
        )


class TestInstallerDetectionMatchesTheDefault:
    """The install script's own detection must not contradict the plugin."""

    def test_no_hard_failure_without_a_cloud_key(self):
        """The dead-end 'plugin will not load' message must be gone."""
        block = _detect_embedding_block()
        assert "plugin will not load until an embedding" not in block, (
            "the installer still dead-ends a user with no cloud key. It must "
            "point them at the local setup instead - that user IS the default "
            "case."
        )

    def test_local_default_is_multilingual_1024d(self):
        block = _detect_embedding_block()
        # What must not happen is *assigning* the 768d English-focused model.
        # (The block may still mention it in a comparison list.)
        assigned = re.findall(r'EMBEDDING_MODEL="([^"]+)"', block)
        for value in assigned:
            assert "nomic-embed-text" not in value, (
                "the installer still assigns nomic-embed-text (768d, "
                "English-focused) as the local default; it must use a 1024d "
                "multilingual model so the collection stays compatible."
            )
        assert "qwen3-embedding" in block, (
            "the installer does not mention qwen3-embedding - the local "
            "default it should be setting up."
        )

    def test_installer_points_at_the_interactive_wizard(self):
        block = _detect_embedding_block()
        assert "nexus_memory.wizard" in block or "nexus-memory-init" in block, (
            "the installer should tell the user how to make an explicit "
            "provider choice instead of deciding for them."
        )


class TestDocsRouteUsersToTheWizard:
    """The docs are the install path - they must mention the picker."""

    def test_agents_md_mentions_the_wizard(self):
        src = _read(AGENTS_MD)
        assert "nexus_memory.wizard" in src or "nexus-memory-init" in src, (
            "AGENTS.md never tells the reader that an interactive provider "
            "picker exists. Users then assume there is no choice."
        )

    def test_agents_md_does_not_lead_with_cloud(self):
        src = _read(AGENTS_MD)
        assert "auto-detected in this order" not in src, (
            "AGENTS.md still presents cloud as the detection order; the "
            "default is local and cloud is the opt-in."
        )

    def test_readme_quickstart_names_the_wizard(self):
        """The README is the front page - the picker must be visible there.

        It used to appear only inside the changelog table, i.e. nowhere a
        new user would look. That is how a built feature stays invisible.
        """
        src = _read(README_MD)
        quickstart = src.split("## 🤖 Quick Start", 1)
        assert len(quickstart) == 2, "README has no Quick Start section"
        body = quickstart[1]
        assert "nexus_memory.wizard" in body or "nexus-memory-init" in body, (
            "the README Quick Start never mentions the interactive provider "
            "picker, so users never learn they can choose."
        )

    def test_readme_does_not_present_cloud_as_the_default(self):
        src = _read(README_MD)
        assert "Embedding Provider (auto-detected)" not in src, (
            "the README still headlines the embedding section as "
            "'auto-detected' with cloud listed first; the default is local."
        )
        assert "The default is local" in src, (
            "the README must state plainly that the embedding default is local."
        )
