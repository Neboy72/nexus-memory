"""Per-installation service-key generation and failure handling."""

from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

import nexus_memory.service_auth as service_auth


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    """Tests must never touch the real data directory."""
    monkeypatch.setenv("NEXUS_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("NEXUS_SERVICE_AUTH_KEY", raising=False)


def test_empty_existing_key_is_not_overwritten(monkeypatch, tmp_path, caplog):
    """Punkt 4: an existing empty key file must not be silently replaced."""
    path = tmp_path / "service_auth.key"
    path.write_text("", encoding="utf-8")

    with caplog.at_level("ERROR", logger="nexus_memory.service_auth"):
        key = service_auth.ensure_service_key()

    assert key == ""
    assert path.read_text(encoding="utf-8") == ""
    assert "empty" in caplog.text
    assert str(path) in caplog.text


def test_unreadable_existing_key_is_not_overwritten(monkeypatch, tmp_path, caplog):
    """Punkt 4: an unreadable existing key must not be silently replaced."""
    path = tmp_path / "service_auth.key"
    path.write_text("valid-key", encoding="utf-8")
    os.chmod(path, 0o000)

    try:
        with caplog.at_level("ERROR", logger="nexus_memory.service_auth"):
            key = service_auth.ensure_service_key()
    finally:
        os.chmod(path, 0o600)

    assert key == ""
    assert "unreadable" in caplog.text
    assert str(path) in caplog.text


def test_parallel_ensure_service_key_returns_same_key(monkeypatch, tmp_path):
    """Punkt 4: concurrent starters must not produce different keys."""
    results: list[str] = []
    lock = threading.Lock()

    def caller():
        value = service_auth.ensure_service_key()
        with lock:
            results.append(value)

    threads = [threading.Thread(target=caller) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    non_empty = [r for r in results if r]
    assert non_empty, "at least one caller must obtain a key"
    assert len(set(non_empty)) == 1, "all successful callers must share the same key"
    assert (tmp_path / "service_auth.key").exists()
