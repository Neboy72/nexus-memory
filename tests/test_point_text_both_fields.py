"""Der Inhalt eines Punktes: ``content`` oder ``text`` (15.11.2026).

Warum: 628 von 52.631 Einträgen trugen ihren Text NUR unter ``text``
(296 OpenClaw-Protokolle, 301 Wiki-Einträge, 29 ``openclaw_tool``,
2 ``insight-agent``). Der Bestand ist nachgetragen; diese Tests halten die
Ursache zu: ein Punkt mit nur ``text`` muss gefunden werden und mit vollem
Inhalt zurückkommen, ein Punkt mit ``content`` bleibt unverändert, und liegen
beide Felder vor, gewinnt ``content``.

Geprüft wird an derselben Stelle, an der es weh tat: `point_text()` (der
zentrale Helfer), der Recall-Pfad über `MemoryStore.recall()` und die
`fact_history`-Ausgabe.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import nexus_memory.mcp_server as mcp
from nexus_memory.collection_vectors import point_text


# ===========================================================================
# Der zentrale Helfer
# ===========================================================================


class TestPointText:
    """``content`` führt, ``text`` ist der Rückfall — nie umgekehrt."""

    def test_only_text_returns_text(self):
        assert point_text({"text": "nur im text-Feld"}) == "nur im text-Feld"

    def test_only_content_returns_content(self):
        assert point_text({"content": "nur im content-Feld"}) == "nur im content-Feld"

    def test_both_fields_content_wins(self):
        assert point_text({"content": "führend", "text": "veraltet"}) == "führend"

    def test_empty_content_falls_back_to_text(self):
        # `or`, nicht `is None`: ein leerer content darf den text nicht verdecken.
        assert point_text({"content": "", "text": "der echte Text"}) == "der echte Text"

    def test_neither_field_is_empty_string(self):
        assert point_text({}) == ""
        assert point_text({"content": None, "text": None}) == ""

    def test_non_dict_payload_is_empty_string(self):
        # Payloads kommen als untrusted JSON — ein None/str darf nicht werfen.
        assert point_text(None) == ""
        assert point_text("kein dict") == ""  # type: ignore[arg-type]

    def test_non_string_value_is_never_returned_raw(self):
        assert point_text({"content": 42}) == "42"


# ===========================================================================
# Recall: gefunden werden UND mit Inhalt zurückkommen
# ===========================================================================


class _FakeEmbedder:
    """Ersatz für den echten EmbeddingProvider (kein Netz, kein Modell)."""

    _name = "fake-embedder"
    _dim = 384
    _backend = "local"
    _model = None
    _client = None

    async def embed(self, text: str, is_query: bool = True) -> list[float]:
        return [0.0] * 384

    @property
    def backend(self) -> str:
        return self._backend

    @property
    def provider_type(self) -> str:
        return "local"

    @property
    def name(self) -> str:
        return self._name

    @property
    def dim(self) -> int:
        return self._dim

    @property
    def available(self) -> bool:
        return True

    @property
    def model_name(self) -> str:
        return self._name


@pytest.fixture
def store(monkeypatch, mock_qdrant_client, isolated_env):
    """MemoryStore mit gemocktem Qdrant-Client (Muster aus test_temporal_validity)."""
    monkeypatch.setattr(mcp, "EmbeddingProvider", _FakeEmbedder)
    monkeypatch.setattr(mcp.MemoryStore, "_init_hybrid", lambda self: None)
    mock_qdrant_client.get_collections.return_value = MagicMock(collections=[])
    st = mcp.MemoryStore()
    st._hybrid_retriever = None
    return st


def _hit(pid: str, payload: dict, score: float = 0.9):
    """Eine query_points()-förmige Fundstelle."""
    hit = MagicMock()
    hit.id = pid
    hit.payload = payload
    hit.score = score
    return hit


def _base(**extra) -> dict:
    """Payload-Grundform eines gespeicherten Punktes."""
    payload = {
        "id": "p1",
        "access_level": "public",
        "category": "fact",
        "source": "test",
        "created_at": "2026-01-01T00:00:00+00:00",
        "lifecycle_status": "canonical",
    }
    payload.update(extra)
    return payload


class TestRecallReadsBothFields:
    async def test_text_only_point_is_found_and_returns_full_content(
        self, store, mock_qdrant_client
    ):
        """Der Kern des Befunds: ein Punkt ohne ``content`` war unsichtbar."""
        mock_qdrant_client.query_points.return_value = MagicMock(
            points=[_hit("p1", _base(text="Der Verteiler steht im Flur"))]
        )

        results = await store.recall("verteiler", limit=5)

        assert len(results) == 1, "der Punkt muss gefunden werden, nicht wegfallen"
        assert results[0]["text"] == "Der Verteiler steht im Flur"

    async def test_content_only_point_is_unchanged(self, store, mock_qdrant_client):
        mock_qdrant_client.query_points.return_value = MagicMock(
            points=[_hit("p1", _base(content="Unverändert"))]
        )

        results = await store.recall("unverändert", limit=5)

        assert len(results) == 1
        assert results[0]["text"] == "Unverändert"

    async def test_both_fields_content_wins(self, store, mock_qdrant_client):
        mock_qdrant_client.query_points.return_value = MagicMock(
            points=[_hit("p1", _base(content="führend", text="veraltet"))]
        )

        results = await store.recall("führend", limit=5)

        assert len(results) == 1
        assert results[0]["text"] == "führend"


# ===========================================================================
# fact_history: dieselbe Regel, andere Ausgabe
# ===========================================================================


class TestFactHistoryEntry:
    def test_text_only_point_has_a_preview(self):
        entry = mcp.MemoryStore._fact_history_entry(
            "p1", _base(text="Nur im text-Feld")
        )
        assert entry["text"] == "Nur im text-Feld"

    def test_both_fields_content_wins(self):
        entry = mcp.MemoryStore._fact_history_entry(
            "p1", _base(content="führend", text="veraltet")
        )
        assert entry["text"] == "führend"
