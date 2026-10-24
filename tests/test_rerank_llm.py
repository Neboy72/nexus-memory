"""Tests for the LLM reranker path in ``nexus_memory.reranker``.

All HTTP calls are mocked; no network traffic is generated.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import nexus_memory.reranker as rr


def _pt(point_id: str, content: str, score: float) -> SimpleNamespace:
    """Duck-typed Qdrant ScoredPoint."""
    return SimpleNamespace(
        id=point_id, payload={"id": point_id, "content": content}, score=score
    )


def _pool() -> list:
    """8 points in vector-score order."""
    return [
        _pt("p0", "zero gardening tips", 0.92),
        _pt("p1", "one routing fallback", 0.88),
        _pt("p2", "two cat pictures", 0.85),
        _pt("p3", "three deepseek routing table", 0.80),
        _pt("p4", "four provider config", 0.77),
        _pt("p5", "five unrelated", 0.75),
        _pt("p6", "six fallback routing guide", 0.73),
        _pt("p7", "seven more text", 0.70),
    ]


def _make_urlopen_mock(content: str):
    """Return a stand-in for ``urllib.request.urlopen`` that yields ``content``."""

    def _urlopen(req, timeout=None):
        response = MagicMock()
        response.read.return_value = json.dumps(
            {
                "choices": [
                    {
                        "message": {"content": content},
                        "finish_reason": "stop",
                    }
                ]
            }
        ).encode("utf-8")
        response.__enter__ = lambda s: s
        response.__exit__ = lambda *args: None
        return response

    return _urlopen


class TestRerankLlm:
    def test_reorders_by_model_answer(self, monkeypatch):
        """Antwort '3,0,7' -> Reihenfolge beginnt mit 3,0,7; Rest folgt; Laenge bleibt."""
        monkeypatch.setattr(
            rr, "_get_llm_credentials", lambda: ("key", "http://base", "model")
        )
        monkeypatch.setattr(
            rr.urllib.request, "urlopen", _make_urlopen_mock("3,0,7")
        )

        pts = _pool()
        out = rr.rerank_points("routing fallback", pts, reranker="llm", pool_k=8)

        assert [p.id for p in out[:3]] == ["p3", "p0", "p7"]
        assert len(out) == len(pts)
        # Rest in original order, excluding already-placed indices 0, 3 and 7.
        rest_ids = [p.id for p in out[3:]]
        assert rest_ids == ["p1", "p2", "p4", "p5", "p6"]

    def test_no_indices_keeps_original_order(self, monkeypatch):
        """Antwort ohne Ziffern -> alte Reihenfolge unveraendert."""
        monkeypatch.setattr(
            rr, "_get_llm_credentials", lambda: ("key", "http://base", "model")
        )
        monkeypatch.setattr(
            rr.urllib.request, "urlopen", _make_urlopen_mock("keine Ahnung")
        )

        pts = _pool()
        out = rr.rerank_points("routing fallback", pts, reranker="llm", pool_k=8)
        assert [p.id for p in out] == [p.id for p in pts]

    def test_missing_credentials_fail_open_and_warn_once(self, monkeypatch, caplog):
        """Kein Key/Basis -> unveraenderte Reihenfolge und exakt eine Warnung."""
        monkeypatch.setattr(rr, "_get_llm_credentials", lambda: (None, None, "model"))
        rr._LLM_UNAVAILABLE_LOGGED = False

        pts = _pool()
        with caplog.at_level("WARNING", logger="nexus.hermes_plugin.rerank"):
            out = rr.rerank_points("routing fallback", pts, reranker="llm", pool_k=8)

        assert [p.id for p in out] == [p.id for p in pts]
        assert any("llm rerank unavailable" in rec.message for rec in caplog.records)

        # Second call must not log again.
        caplog.clear()
        with caplog.at_level("WARNING", logger="nexus.hermes_plugin.rerank"):
            rr.rerank_points("routing fallback", pts, reranker="llm", pool_k=8)
        assert not any("llm rerank unavailable" in rec.message for rec in caplog.records)

    def test_resolve_reranker(self):
        """_resolve_reranker("auto") == 'llm'; 'voyage' raises."""
        assert rr._resolve_reranker("auto") == "llm"
        assert rr._resolve_reranker("") == "llm"
        assert rr._resolve_reranker("cross-encoder") == "cross-encoder"
        with pytest.raises(ValueError, match="Voyage reranker has been removed"):
            rr._resolve_reranker("voyage")

    def test_prompt_contains_question_and_limited_lines(self, monkeypatch):
        """Prompt enthaelt die Frage und nicht mehr als 30 Kandidaten-Zeilen."""
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["body"] = json.loads(req.data.decode("utf-8"))
            response = MagicMock()
            response.read.return_value = json.dumps(
                {"choices": [{"message": {"content": "0"}}]}
            ).encode("utf-8")
            response.__enter__ = lambda s: s
            response.__exit__ = lambda *args: None
            return response

        monkeypatch.setattr(
            rr, "_get_llm_credentials", lambda: ("key", "http://base", "model")
        )
        monkeypatch.setattr(rr.urllib.request, "urlopen", fake_urlopen)

        pts = [_pt(f"p{i}", f"content {i}", 0.9 - i * 0.01) for i in range(35)]
        rr.rerank_points("routing fallback", pts, reranker="llm", pool_k=30)

        prompt = captured["body"]["messages"][0]["content"]
        assert "routing fallback" in prompt
        lines = [line for line in prompt.splitlines() if line.startswith("[")]
        assert len(lines) == 30
        assert all(len(line) <= 300 + len("[X] ") for line in lines)

        # Required payload fields.
        assert captured["body"]["model"] == "model"
        assert captured["body"]["max_tokens"] == 6000
        assert captured["body"]["temperature"] == 0
        assert captured["body"]["reasoning_effort"] == "low"

    def test_http_timeout_is_90_seconds(self, monkeypatch):
        """Timeout-Parameter im urlopen-Aufruf ist 90 s."""
        captured = []

        def fake_urlopen(req, timeout=None):
            captured.append(timeout)
            response = MagicMock()
            response.read.return_value = json.dumps(
                {"choices": [{"message": {"content": "0"}}]}
            ).encode("utf-8")
            response.__enter__ = lambda s: s
            response.__exit__ = lambda *args: None
            return response

        monkeypatch.setattr(
            rr, "_get_llm_credentials", lambda: ("key", "http://base", "model")
        )
        monkeypatch.setattr(rr.urllib.request, "urlopen", fake_urlopen)
        rr._LLM_UNAVAILABLE_LOGGED = False

        pts = _pool()
        rr.rerank_points("routing fallback", pts, reranker="llm", pool_k=8)
        assert captured == [90]

    def test_retry_on_empty_response_then_succeeds(self, monkeypatch):
        """Erster Aufruf leer, zweiter liefert '2,1' -> Reihenfolge wird angewendet."""
        monkeypatch.setattr(
            rr, "_get_llm_credentials", lambda: ("key", "http://base", "model")
        )
        calls = []

        def fake_urlopen(req, timeout=None):
            calls.append(timeout)
            content = "" if len(calls) == 1 else "2,1"
            response = MagicMock()
            response.read.return_value = json.dumps(
                {"choices": [{"message": {"content": content}}]}
            ).encode("utf-8")
            response.__enter__ = lambda s: s
            response.__exit__ = lambda *args: None
            return response

        monkeypatch.setattr(rr.urllib.request, "urlopen", fake_urlopen)
        rr._LLM_UNAVAILABLE_LOGGED = False

        pts = _pool()
        out = rr.rerank_points("routing fallback", pts, reranker="llm", pool_k=8)

        assert [p.id for p in out[:2]] == ["p2", "p1"]
        assert [p.id for p in out[2:]] == ["p0", "p3", "p4", "p5", "p6", "p7"]
        assert len(calls) == 2
        assert all(t == 90 for t in calls)

    def test_double_empty_response_keeps_order_and_warns_once(self, monkeypatch, caplog):
        """Beide Aufrufe leer -> alte Reihenfolge, genau EINE Warnung."""
        monkeypatch.setattr(
            rr, "_get_llm_credentials", lambda: ("key", "http://base", "model")
        )
        rr._LLM_UNAVAILABLE_LOGGED = False

        def fake_urlopen(req, timeout=None):
            response = MagicMock()
            response.read.return_value = json.dumps(
                {"choices": [{"message": {"content": ""}}]}
            ).encode("utf-8")
            response.__enter__ = lambda s: s
            response.__exit__ = lambda *args: None
            return response

        monkeypatch.setattr(rr.urllib.request, "urlopen", fake_urlopen)

        pts = _pool()
        with caplog.at_level("WARNING", logger="nexus.hermes_plugin.rerank"):
            out = rr.rerank_points("routing fallback", pts, reranker="llm", pool_k=8)

        assert [p.id for p in out] == [p.id for p in pts]
        warning_records = [
            r for r in caplog.records if "llm rerank unavailable" in r.message
        ]
        assert len(warning_records) == 1
