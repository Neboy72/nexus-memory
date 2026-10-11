"""The local Ollama path of the extraction, and the fallback that must be LOUD.

11.10.2026 (measured, reproduced): on the OpenAI-compatible path (``/v1``) the
field ``think: false`` is not passed through. A reasoning model then spends the
whole token budget in the hidden ``reasoning`` field and returns an EMPTY
``content`` — ``json.loads("")`` raises "Expecting value: line 1 column 1
(char 0)". Between 01.10. and 11.10.2026 it happened 313 times (counted on the
machine it ran on, in ~/.hermes/logs/gateway.error.log, lines of 01.10.-11.10.2026
carrying "LLM extraction failed"; 267 of them this empty answer, 46 other causes
such as a truncated JSON string), and every one
of them fell silently back to the heuristic, which yields ZERO relationships: the
graph was written without edges and nobody noticed, because the only trace was a
log line nobody reads.

These tests pin both halves of the fix:

1. When the endpoint is the local Ollama service (port 11434), the extraction
   goes through ``/api/chat``, where ``think: false`` works (measured: 1,9 s and
   0 reasoning tokens instead of 15,5 s and 6531 completion tokens). Every other
   endpoint keeps the previous path untouched.
2. A failed extraction is COUNTED and NAMED in a state file, so a watch can see
   the fallback instead of guessing it.
"""
from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import nexus_memory.entity_extractor as ee  # noqa: E402

GOOD_ANSWER = json.dumps({
    "entities": [
        {"name": "Alpha", "type": "concept", "confidence": 0.9},
        {"name": "Beta", "type": "concept", "confidence": 0.8},
    ],
    "relationships": [
        {"source": "Alpha", "target": "Beta", "relation": "uses", "confidence": 0.7},
    ],
})


def _health(home: Path) -> dict:
    path = home / "state" / "entity-extractor-health.json"
    assert path.exists(), f"no health counter was written at {path}"
    return json.loads(path.read_text(encoding="utf-8"))


def _reachable(monkeypatch) -> None:
    """Take the TCP probe out of the test: it must never decide the outcome.

    The probe is a real socket connect. Left in, the result would depend on
    whether this machine happens to run Ollama — and a test that passes because
    of the machine it runs on has already lied to us once.
    """
    monkeypatch.setattr(ee, "_quick_health_check", lambda *a, **k: True)


def _config(base_url: str) -> dict:
    return {"base_url": base_url, "api_key": "test-key", "model": "test-model"}


# ── 1. the local endpoint takes the native path ──────────────────────────────

@pytest.mark.parametrize("url,expected", [
    ("http://localhost:11434/v1", "http://localhost:11434"),
    ("http://127.0.0.1:11434", "http://127.0.0.1:11434"),
    ("http://ollama-box.local:11434/v1/", "http://ollama-box.local:11434"),
    ("https://api.example.com/v1", ""),
    ("http://localhost:8000/v1", ""),
    ("", ""),
])
def test_native_path_exists_only_for_the_local_port(url: str, expected: str) -> None:
    """Every other endpoint keeps the previous path — the routing is conditional."""
    assert ee._ollama_native_base(url) == expected


def test_local_endpoint_goes_through_the_native_chat_path(tmp_path, monkeypatch) -> None:
    seen: dict = {}

    def fake_chat(base: str, model: str, prompt: str) -> str:
        seen["base"] = base
        seen["model"] = model
        seen["prompt_has_text"] = "Nachricht" in prompt
        return GOOD_ANSWER

    monkeypatch.setattr(ee, "_load_llm_config",
                        lambda home: _config("http://localhost:11434/v1"))
    monkeypatch.setattr(ee, "_ollama_chat", fake_chat)
    _reachable(monkeypatch)

    result = ee._llm_extract_entities("Nachricht", str(tmp_path))

    assert seen.get("base") == "http://localhost:11434", (
        "the local endpoint did not go through /api/chat — on /v1 the field "
        "think:false is ignored and the answer stays empty"
    )
    assert seen.get("prompt_has_text") is True, "the text never reached the model"
    assert [e.name for e in result.entities] == ["Alpha", "Beta"]
    assert [(r.source, r.target, r.relation) for r in result.relationships] == [
        ("Alpha", "Beta", "uses")
    ]
    assert _health(tmp_path)["llm_ok_total"] == 1


def test_non_local_endpoint_keeps_the_openai_path(tmp_path, monkeypatch) -> None:
    """A cloud endpoint must NOT be routed through the Ollama-native call."""
    used_native: list = []

    monkeypatch.setattr(ee, "_ollama_chat",
                        lambda *a, **k: used_native.append(a) or "")
    monkeypatch.setattr(ee, "_load_llm_config",
                        lambda home: _config("https://api.example.com/v1"))
    _reachable(monkeypatch)

    class _Message:
        content = GOOD_ANSWER

    class _Choice:
        message = _Message()

    class _Response:
        choices = [_Choice()]

    class _Completions:
        @staticmethod
        def create(**kwargs):
            return _Response()

    class _Chat:
        completions = _Completions()

    class _Client:
        chat = _Chat()

        def __init__(self, **kwargs):
            pass

    monkeypatch.setitem(sys.modules, "openai",
                        types.SimpleNamespace(OpenAI=_Client))

    result = ee._llm_extract_entities("Nachricht", str(tmp_path))

    assert used_native == [], "a foreign endpoint was sent through /api/chat"
    assert [e.name for e in result.entities] == ["Alpha", "Beta"]


# ── 2. the fallback is counted and named, never silent ───────────────────────

def test_empty_answer_is_counted_and_named(tmp_path, monkeypatch) -> None:
    """The exact defect of 01.10.-11.10.2026: an empty answer, invisible."""
    monkeypatch.setattr(ee, "_load_llm_config",
                        lambda home: _config("http://localhost:11434/v1"))
    monkeypatch.setattr(ee, "_ollama_chat", lambda *a, **k: "")
    _reachable(monkeypatch)

    result = ee._llm_extract_entities("Nachricht", str(tmp_path))

    assert result.entities == [] and result.relationships == [], (
        "an empty answer must produce an empty extraction, not invented data"
    )
    health = _health(tmp_path)
    assert health["fallback_total"] == 1, (
        "the fallback was not counted — a silent fallback is what made 313 "
        "extractions disappear unnoticed"
    )
    assert "Expecting value" in health["fallback_letzter_grund"], (
        f"the reason was not recorded verbatim (got {health['fallback_letzter_grund']!r}); "
        "a counter without a reason cannot be diagnosed"
    )


def test_success_and_failure_are_counted_separately(tmp_path, monkeypatch) -> None:
    """One counter file, two numbers — a watch can compute a failure rate."""
    answers = iter([GOOD_ANSWER, "", ""])
    monkeypatch.setattr(ee, "_load_llm_config",
                        lambda home: _config("http://localhost:11434/v1"))
    monkeypatch.setattr(ee, "_ollama_chat", lambda *a, **k: next(answers))
    _reachable(monkeypatch)

    ee._llm_extract_entities("eins", str(tmp_path))
    ee._llm_extract_entities("zwei", str(tmp_path))
    ee._llm_extract_entities("drei", str(tmp_path))

    health = _health(tmp_path)
    assert health["llm_ok_total"] == 1
    assert health["fallback_total"] == 2
    assert health["llm_letzte_beziehungen"] == 1, (
        "the relationship count of the last good extraction is not visible"
    )


def test_unreachable_endpoint_is_counted_too(tmp_path, monkeypatch) -> None:
    """The second silent exit: the endpoint does not answer at all.

    This one returned an empty result with only a debug log line — the same
    invisible outcome as the empty answer, from a different cause.
    """
    monkeypatch.setattr(ee, "_load_llm_config",
                        lambda home: _config("http://127.0.0.1:59999/v1"))
    monkeypatch.setattr(ee, "_quick_health_check", lambda *a, **k: False)

    result = ee._llm_extract_entities("Nachricht", str(tmp_path))

    assert result.entities == [] and result.relationships == []
    health = _health(tmp_path)
    assert health["fallback_total"] == 1
    assert "nicht erreichbar" in health["fallback_letzter_grund"], (
        "an unreachable endpoint must name itself in the counter, not vanish"
    )


def test_missing_model_is_counted_too(tmp_path, monkeypatch) -> None:
    """The third silent exit: no model configured."""
    monkeypatch.setattr(ee, "_load_llm_config",
                        lambda home: {"base_url": "http://localhost:11434/v1",
                                      "api_key": "k", "model": ""})

    result = ee._llm_extract_entities("Nachricht", str(tmp_path))

    assert result.entities == []
    health = _health(tmp_path)
    assert "Modell" in health["fallback_letzter_grund"]
