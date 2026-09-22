"""Baustein A: ``nexus-memory serve`` — Streamable HTTP + ``/healthz``.

Design: ``docs/standalone-design-20260922.md`` §Baustein A.

These tests bind no port and require no live Qdrant: the Starlette app is
driven in-process through Starlette's ASGI ``TestClient``. The stdio path
is asserted to stay the default so ``serve`` remains purely additive.
"""

from __future__ import annotations

import inspect
import json
import sys

import nexus_memory.mcp_server as mcp


def _client():
    """In-process ASGI client over the real serve app (lifespan runs on enter)."""
    from starlette.testclient import TestClient

    return TestClient(mcp.build_serve_app(), base_url="http://127.0.0.1:9122")


def _jsonrpc_messages(response):
    """Return JSON-RPC message(s) from a JSON or SSE response body.

    Streamable HTTP answers ``initialize`` with SSE (``text/event-stream``)
    by default; a JSON content type is also accepted.
    """
    if "text/event-stream" in response.headers.get("content-type", ""):
        messages = []
        for line in response.text.splitlines():
            if line.startswith("data:"):
                payload = line[len("data:"):].strip()
                if payload:
                    messages.append(json.loads(payload))
        return messages
    return [response.json()]


# ===========================================================================
# 1. /healthz
# ===========================================================================


class TestHealthz:
    def test_ok_when_qdrant_reachable(self, monkeypatch):
        monkeypatch.setattr(mcp, "_qdrant_reachable", lambda *a, **k: True)
        with _client() as client:
            r = client.get("/healthz")

        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
        assert body["qdrant"] is True
        assert isinstance(body["version"], str) and body["version"]
        assert isinstance(body["uptime_s"], (int, float)) and body["uptime_s"] >= 0

    def test_degraded_when_qdrant_down(self, monkeypatch):
        monkeypatch.setattr(mcp, "_qdrant_reachable", lambda *a, **k: False)
        with _client() as client:
            r = client.get("/healthz")

        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "degraded"
        assert body["qdrant"] is False

    def test_reports_configured_version(self, monkeypatch):
        monkeypatch.setattr(mcp, "_qdrant_reachable", lambda *a, **k: True)
        with _client() as client:
            body = client.get("/healthz").json()

        assert body["version"] == mcp.nexus_version


# ===========================================================================
# 2. MCP over Streamable HTTP
# ===========================================================================


class TestMcpOverHttp:
    def test_initialize_round_trip(self):
        with _client() as client:
            r = client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "pytest", "version": "1.0"},
                    },
                },
                headers={
                    "Accept": "application/json, text/event-stream",
                    "Content-Type": "application/json",
                },
                # Real MCP clients POST straight to /mcp and do not follow
                # redirects — a 307 here would break them.
                follow_redirects=False,
            )

        assert r.status_code == 200, r.text
        init = next(m for m in _jsonrpc_messages(r) if m.get("id") == 1)
        result = init["result"]
        assert result["serverInfo"]["name"] == "nexus-memory"
        assert result["serverInfo"]["version"] == mcp.nexus_version
        assert "protocolVersion" in result
        assert "capabilities" in result

    def test_tools_list_after_initialize(self):
        """The 15 tools registered on the lowlevel server are reachable over HTTP."""
        with _client() as client:
            init = client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "pytest", "version": "1.0"},
                    },
                },
                headers={"Accept": "application/json, text/event-stream"},
            )
            session_id = init.headers.get("mcp-session-id")
            headers = {"Accept": "application/json, text/event-stream"}
            if session_id:
                headers["mcp-session-id"] = session_id
            r = client.post(
                "/mcp",
                json={"jsonrpc": "2.0", "id": 2, "method": "notifications/initialized"},
                headers=headers,
            )
            r = client.post(
                "/mcp",
                json={"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}},
                headers=headers,
            )

        assert r.status_code == 200, r.text
        listing = next(m for m in _jsonrpc_messages(r) if m.get("id") == 3)
        tools = listing["result"]["tools"]
        assert len(tools) >= 15
        assert any(t["name"] == "remember" for t in tools)


# ===========================================================================
# 3. stdio default mode stays untouched
# ===========================================================================


class TestStdioDefaultUnchanged:
    def test_cli_without_args_runs_stdio_main(self, monkeypatch):
        captured = {}

        def fake_run(coro):
            captured["qualname"] = getattr(coro, "__qualname__", "")
            coro.close()

        monkeypatch.setattr(mcp.asyncio, "run", fake_run)
        monkeypatch.setattr(sys, "argv", ["nexus-memory"])

        assert mcp.cli() == 0
        assert captured["qualname"].endswith("main")

    def test_cli_without_args_does_not_call_serve(self, monkeypatch):
        monkeypatch.setattr(mcp, "serve", lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("serve must not run in default mode")))
        monkeypatch.setattr(mcp.asyncio, "run", lambda coro: coro.close())
        monkeypatch.setattr(sys, "argv", ["nexus-memory"])

        assert mcp.cli() == 0

    def test_cli_serve_dispatches_to_serve(self, monkeypatch):
        calls = {}

        def fake_serve(*a, **k):
            calls["called"] = True
            return 0

        monkeypatch.setattr(mcp, "serve", fake_serve)
        monkeypatch.setattr(sys, "argv", ["nexus-memory", "serve"])

        assert mcp.cli() == 0
        assert calls.get("called") is True

    def test_main_still_speaks_stdio(self):
        assert "stdio_server" in inspect.getsource(mcp.main)


# ===========================================================================
# 4. Port configuration
# ===========================================================================


class TestServePort:
    def test_default_is_9122(self, monkeypatch):
        monkeypatch.delenv("NEXUS_SERVE_PORT", raising=False)
        assert mcp._serve_port() == 9122
        assert mcp.DEFAULT_SERVE_PORT == 9122

    def test_env_override_wins(self, monkeypatch):
        monkeypatch.setenv("NEXUS_SERVE_PORT", "9999")
        assert mcp._serve_port() == 9999

    def test_malformed_env_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("NEXUS_SERVE_PORT", "not-a-port")
        assert mcp._serve_port() == 9122
