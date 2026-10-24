"""Der lokale HuggingFace-Einbetter darf Apples GPU (MPS) nicht selbst waehlen.

Gemessen am 15.10.2026 (Gateway-Log, 9 harte Abbrueche in fuenf Tagen):
sentence-transformers loest auf Apple Silicon von selbst auf ``mps`` auf, und
PyTorch-MPS bricht bei zwei gleichzeitigen Einbettungen im selben Prozess den
GANZEN Prozess ab (Metal-Assertion ``_status < MTLCommandBufferStatusCommitted``
bzw. ``A command encoder is already encoding to this command buffer``). Der
Agent starb mitten in seiner eigenen Gedaechtnisarbeit, danach kam er still
wieder hoch.

Der Test prueft das GERAET, das der Provider beim Laden anfordert — nicht einen
Wortlaut im Quelltext. Der Einbetter selbst wird gefakt: kein echtes Modell
laden (siehe Anleitung, Abschnitt Speicher/MPS).
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def _uname(sysname: str, machine: str):
    return types.SimpleNamespace(sysname=sysname, machine=machine, release="", version="", nodename="")


class _FakeST:
    """Fake sentence-transformers: merkt sich das angeforderte Geraet."""

    def __init__(self, name: str, device=None, **kwargs):
        _FakeST.captured["name"] = name
        _FakeST.captured["device"] = device

    def encode(self, text, **kwargs):
        return [0.0] * 8

    captured: dict = {}


@pytest.fixture()
def fake_st(monkeypatch):
    _FakeST.captured = {}
    module = types.ModuleType("sentence_transformers")
    module.SentenceTransformer = _FakeST
    monkeypatch.setitem(sys.modules, "sentence_transformers", module)
    # Der Provider darf sich nicht auf die Sammlung/andere Wege verlassen.
    for name in ("OPENAI_API_KEY", "GOOGLE_API_KEY", "JINA_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("NEXUS_HF_DEVICE", raising=False)
    monkeypatch.setenv("NEXUS_EMBEDDING_PROVIDER", "sentence-transformers")
    return _FakeST.captured


def _provider():
    from nexus_memory.embeddings import EmbeddingProvider

    return EmbeddingProvider(preferred="sentence-transformers")


def test_apple_silicon_gets_cpu(monkeypatch, fake_st):
    """Auf Apple Silicon liegt das Modell auf der CPU — MPS kann den Prozess toeten."""
    monkeypatch.setattr("os.uname", lambda: _uname("Darwin", "arm64"))
    provider = _provider()
    assert provider.available, "sentence-transformers must be selected for an explicit preference"
    assert fake_st["device"] == "cpu", (
        "the local HF model must not be handed Apple's GPU — MPS aborts the whole "
        f"process under two overlapping encodes (got {fake_st['device']!r})"
    )


def test_explicit_override_wins(monkeypatch, fake_st):
    """NEXUS_HF_DEVICE bleibt der ausdrueckliche Hebel (z. B. cuda auf eigener Karte)."""
    monkeypatch.setattr("os.uname", lambda: _uname("Darwin", "arm64"))
    monkeypatch.setenv("NEXUS_HF_DEVICE", "cuda")
    provider = _provider()
    assert provider.available
    assert fake_st["device"] == "cuda"


def test_other_platforms_keep_the_library_choice(monkeypatch, fake_st):
    """Wo es kein MPS gibt, entscheidet die Bibliothek selbst (cuda, sonst cpu)."""
    monkeypatch.setattr("os.uname", lambda: _uname("Linux", "x86_64"))
    provider = _provider()
    assert provider.available
    assert fake_st["device"] is None, "only Apple Silicon is pinned; elsewhere the library decides"
