"""NEXUS_ALLOWED_HOSTS / NEXUS_ALLOWED_ORIGINS: reverse-proxy allow-lists.

The defaults keep the DNS-rebinding guard on loopback only, exactly as before
these knobs existed. Only the Host/Origin check widens — the service key stays
mandatory for /embed, /write and /mcp.

No port is bound and no Qdrant is needed: the tests assert the resolved lists
and the values the app was built with, not the network path.
"""

from __future__ import annotations

import pytest

import nexus_memory.mcp_server as mcp


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("NEXUS_ALLOWED_HOSTS", raising=False)
    monkeypatch.delenv("NEXUS_ALLOWED_ORIGINS", raising=False)
    # build_serve_app() needs a key; the value itself is irrelevant here.
    monkeypatch.setenv("NEXUS_SERVICE_AUTH_KEY", "test-service-key")


def _built_app():
    return mcp.build_serve_app()


def _middleware_lists(app):
    """Host/origin lists handed to the service-key middleware."""
    (middleware,) = [
        entry for entry in app.user_middleware
        if entry.cls.__name__ == "ServiceKeyMiddleware"
    ]
    return middleware.kwargs["allowed_hosts"], middleware.kwargs["allowed_origins"]


def _mcp_security_settings(app):
    """The TransportSecuritySettings the MCP session manager was built with."""
    mounts = [
        r for r in app.routes
        if hasattr(getattr(r, "app", None), "session_manager")
    ]
    (mount,) = mounts
    return mount.app.session_manager.security_settings


# ===========================================================================
# Defaults (no environment variable)
# ===========================================================================

def test_defaults_are_loopback_only():
    assert mcp.allowed_hosts_from_env() == ["127.0.0.1:*", "localhost:*"]
    assert mcp.allowed_origins_from_env() == [
        "http://127.0.0.1:*", "http://localhost:*"]


def test_defaults_match_previous_behaviour():
    """The constants are the literal values every earlier release used."""
    assert mcp.DEFAULT_ALLOWED_HOSTS == ["127.0.0.1:*", "localhost:*"]
    assert mcp.DEFAULT_ALLOWED_ORIGINS == [
        "http://127.0.0.1:*", "http://localhost:*"]


# ===========================================================================
# An added host (the Tailscale name) never replaces the loopback entries
# ===========================================================================

def test_env_host_is_added_and_foreign_host_still_rejected(monkeypatch):
    monkeypatch.setenv(
        "NEXUS_ALLOWED_HOSTS", "nexus-test-host.tail000000.ts.net:443")

    hosts = mcp.allowed_hosts_from_env()

    assert hosts == ["nexus-test-host.tail000000.ts.net:443"]
    # The pattern matcher must say yes to the proxy host and no to anyone else.
    assert _matches(hosts, "nexus-test-host.tail000000.ts.net:443")
    assert not _matches(hosts, "example.com")
    assert not _matches(hosts, "127.0.0.1:9122")


def test_env_hosts_keep_loopback_when_listed(monkeypatch):
    monkeypatch.setenv(
        "NEXUS_ALLOWED_HOSTS",
        "127.0.0.1:9122,localhost:9122,"
        "nexus-test-host.tail000000.ts.net:443")

    hosts = mcp.allowed_hosts_from_env()

    assert _matches(hosts, "127.0.0.1:9122")
    assert _matches(hosts, "localhost:9122")
    assert _matches(hosts, "nexus-test-host.tail000000.ts.net:443")
    assert not _matches(hosts, "example.com")


# ===========================================================================
# Empty / whitespace-only values fall back to the default
# ===========================================================================

@pytest.mark.parametrize("raw", ["", " ", " , ,", ",,,", "  ,  "])
def test_empty_entries_fall_back_to_default(monkeypatch, raw):
    monkeypatch.setenv("NEXUS_ALLOWED_HOSTS", raw)
    monkeypatch.setenv("NEXUS_ALLOWED_ORIGINS", raw)

    assert mcp.allowed_hosts_from_env() == ["127.0.0.1:*", "localhost:*"]
    assert mcp.allowed_origins_from_env() == [
        "http://127.0.0.1:*", "http://localhost:*"]


def test_blank_entries_are_dropped_between_real_ones(monkeypatch):
    monkeypatch.setenv("NEXUS_ALLOWED_HOSTS", " , example.com , ")

    assert mcp.allowed_hosts_from_env() == ["example.com"]


# ===========================================================================
# Both guards use the resolved lists
# ===========================================================================

def test_both_guards_share_the_default_lists():
    app = _built_app()
    hosts, origins = _middleware_lists(app)
    settings = _mcp_security_settings(app)

    assert hosts == ["127.0.0.1:*", "localhost:*"]
    assert origins == ["http://127.0.0.1:*", "http://localhost:*"]
    assert settings.allowed_hosts == hosts
    assert settings.allowed_origins == origins
    assert settings.enable_dns_rebinding_protection is True


def test_both_guards_share_the_env_lists(monkeypatch):
    monkeypatch.setenv(
        "NEXUS_ALLOWED_HOSTS",
        "127.0.0.1:9122,nexus-test-host.tail000000.ts.net:443")
    monkeypatch.setenv(
        "NEXUS_ALLOWED_ORIGINS",
        "https://nexus-test-host.tail000000.ts.net")

    app = _built_app()
    hosts, origins = _middleware_lists(app)
    settings = _mcp_security_settings(app)

    assert hosts == [
        "127.0.0.1:9122", "nexus-test-host.tail000000.ts.net:443"]
    assert origins == ["https://nexus-test-host.tail000000.ts.net"]
    assert settings.allowed_hosts == hosts
    assert settings.allowed_origins == origins


# ===========================================================================
# The service key stays mandatory for the widened lists
# ===========================================================================

def test_widened_host_still_requires_service_key(monkeypatch):
    from starlette.testclient import TestClient

    monkeypatch.setenv(
        "NEXUS_ALLOWED_HOSTS", "nexus-test-host.tail000000.ts.net:443")

    # Explicit :443 so the Host header carries the port the allow-list names.
    client = TestClient(
        _built_app(),
        base_url="http://nexus-test-host.tail000000.ts.net:443",
    )
    response = client.post("/write", json={"content": "x"})

    assert response.status_code == 401
    assert response.json() == {"error": "unauthorized"}


def test_unlisted_host_is_rejected_through_the_app(monkeypatch):
    from starlette.testclient import TestClient

    monkeypatch.setenv(
        "NEXUS_ALLOWED_HOSTS", "nexus-test-host.tail000000.ts.net:443")
    monkeypatch.setenv("NEXUS_SERVICE_AUTH_KEY", "test-service-key")

    client = TestClient(_built_app(), base_url="http://example.com")
    response = client.post(
        "/write",
        json={"content": "x"},
        headers={"x-nexus-service-key": "test-service-key"},
    )

    assert response.status_code == 421
    assert response.json() == {"error": "invalid host"}


# ---------------------------------------------------------------------------
# Helper mirroring the matcher both guards use (exact match, or "prefix:*").
# ---------------------------------------------------------------------------

def _matches(patterns, value):
    value = value.lower()
    for allowed in patterns:
        if allowed.endswith(":*"):
            if value.startswith(allowed[:-2] + ":"):
                return True
        elif value == allowed:
            return True
    return False
