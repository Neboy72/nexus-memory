"""Abnahmetest gegen einen laufenden `nexus-memory serve`.

    python plugins/nexus-memory/smoke-test-http.py [base-url] [key-file]

Prüft healthz, MCP-Handshake, `recall`, `POST /write` und den Recall der gerade
geschriebenen Erinnerung. Reines stdlib — läuft auf jedem Rechner mit Python 3.11+.
Exit-Code 0 = der Dienst erfüllt den Vertrag aus HANDOVER.md.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = (sys.argv[1] if len(sys.argv) > 1 else os.environ.get("NEXUS_BASE_URL", "http://127.0.0.1:9122")).rstrip("/")
KEY_FILE = Path(
    sys.argv[2]
    if len(sys.argv) > 2
    else os.environ.get("NEXUS_KEY_FILE", str(Path.home() / ".nexus-memory" / "service_auth.key"))
).expanduser()
KEY = os.environ.get("NEXUS_SERVICE_AUTH_KEY") or (KEY_FILE.read_text().strip() if KEY_FILE.exists() else "")


def _enroll(install_key: str) -> str:
    """Melde den Harness an — der Installationsschluessel liest seit 15.11.2026 keine Daten.

    Gibt der Aufrufer bereits einen EIGENEN Schluessel mit, antwortet ``/enroll``
    mit 401; dann bleibt sein Schluessel stehen und der erste Datenaufruf sagt,
    was gilt.
    """
    request = urllib.request.Request(
        f"{BASE}/enroll",
        data=json.dumps({"harness": "dsh", "label": "DSH smoke test"}).encode("utf-8"),
        headers={"content-type": "application/json", "x-nexus-service-key": install_key},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read().decode("utf-8")).get("key", "") or ""
    except Exception:
        return ""


if KEY:
    KEY = _enroll(KEY) or KEY

SESSION: dict[str, str] = {}


def headers() -> dict[str, str]:
    head = {
        "content-type": "application/json",
        "accept": "application/json, text/event-stream",
        "x-nexus-service-key": KEY,
    }
    if "id" in SESSION:
        head["mcp-session-id"] = SESSION["id"]
    return head


def post(path: str, body: dict, timeout: float = 120.0) -> tuple[int, dict, str]:
    request = urllib.request.Request(
        f"{BASE}{path}", data=json.dumps(body).encode("utf-8"), headers=headers(), method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            text = response.read().decode("utf-8", "replace")
            session = response.headers.get("mcp-session-id")
            if session:
                SESSION["id"] = session
            return response.status, response.headers, text
    except urllib.error.HTTPError as error:
        return error.code, error.headers, error.read().decode("utf-8", "replace")


def rpc(body: dict) -> dict:
    status, _, text = post("/mcp", body)
    if status == 401:
        raise SystemExit("401 unauthorized — service key missing or wrong (see HANDOVER.md 1.3)")
    if status >= 400 and not text.strip().startswith(("{", "event:")):
        raise SystemExit(f"/mcp returned {status}: {text[:200]}")
    for line in text.splitlines():
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload:
            continue
        message = json.loads(payload)
        if message.get("id") == body.get("id"):
            return message
    raise SystemExit(f"no JSON-RPC response for id {body.get('id')}: {text[:200]}")


def main() -> int:
    print(f"base: {BASE}")
    print(f"key file: {KEY_FILE} (exists: {KEY_FILE.exists()}, len: {len(KEY)})")
    with urllib.request.urlopen(f"{BASE}/healthz", timeout=5) as response:
        print("healthz:", response.read().decode("utf-8", "replace"))

    rpc(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "dsh-smoke-test", "version": "0.3.0"},
            },
        }
    )
    status, _, _ = post("/mcp", {"jsonrpc": "2.0", "method": "notifications/initialized"})
    print("handshake: session", SESSION.get("id"), "| initialized:", status)

    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    content = f"Abnahmetest {stamp}: der DSH-Serve-Vertrag funktioniert (recall + write)."
    status, _, text = post(
        "/write",
        {
            "content": content,
            "category": "session",
            "access_level": "private",
            "source": "dsh-smoke-test",
            "source_type": "session",
        },
    )
    print("write:", status, text[:200])
    if status != 200:
        print("RESULT: failed")
        return 1

    time.sleep(1.0)
    message = rpc(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "recall",
                "arguments": {"query": "Abnahmetest DSH Serve-Vertrag", "limit": 3, "filter_level": "private"},
            },
        }
    )
    block = message.get("result", {}).get("content", [{}])[0].get("text", "{}")
    payload = json.loads(block) if block.strip().startswith("{") else {"results": []}
    hits = payload.get("results", [])
    for hit in hits[:3]:
        print("  hit:", round(float(hit.get("score", 0)), 3), hit.get("category"), str(hit.get("text"))[:80])
    found = any("abnahmetest" in str(hit.get("text", "")).lower() for hit in hits)
    print("recall: ", len(hits), "hits, frischer Eintrag gefunden:", found)
    print("RESULT:", "ok" if hits and found else "failed")
    return 0 if hits and found else 1


if __name__ == "__main__":
    raise SystemExit(main())
