#!/usr/bin/env python3
"""Native-bridge for the DSH Nexus Memory plugin.

Speaks newline-delimited JSON on stdin/stdout and drives the local Nexus
``MemoryStore`` directly (same Qdrant collection, same embeddings, same access
levels as the MCP server and the other Nexus plugins).

Requests
--------
``{"id": 1, "op": "recall", "query": "...", "limit": 5}``
``{"id": 2, "op": "capture", "session": "...", "turn": 1, "user": "...", "assistant": "..."}``
``{"id": 3, "op": "remember", "text": "...", "category": "fact"}``
``{"id": 4, "op": "health"}``

Responses are ``{"id": 1, "ok": true, "result": ...}`` or
``{"id": 1, "ok": false, "error": "..."}``.

stdout belongs to the protocol. Stray ``print`` output from Nexus is redirected
to stderr, and the real stdout is kept on a duplicated descriptor.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

# --------------------------------------------------------------------------
# Keep stdout for the protocol only.
# --------------------------------------------------------------------------
_rpc_fd = os.dup(1)
os.dup2(2, 1)
_rpc_out = os.fdopen(_rpc_fd, "w", encoding="utf-8", buffering=1)
sys.stdout = sys.stderr

logging.basicConfig(stream=sys.stderr, level=logging.WARNING, format="[nexus-bridge] %(levelname)s %(message)s")

LOG = logging.getLogger("nexus-bridge")

ACCESS_LEVEL = os.environ.get("NEXUS_PLUGIN_ACCESS_LEVEL", "private").strip() or "private"
CAPTURE_CATEGORY = os.environ.get("NEXUS_PLUGIN_CAPTURE_CATEGORY", "session").strip() or "session"
CAPTURE_MAX_CHARS = int(os.environ.get("NEXUS_PLUGIN_CAPTURE_MAX_CHARS", "1600"))
CAPTURE_MIN_CHARS = int(os.environ.get("NEXUS_PLUGIN_CAPTURE_MIN_CHARS", "12"))
QDRANT_URL = f"http://{os.environ.get('NEXUS_QDRANT_HOST', '127.0.0.1')}:{os.environ.get('NEXUS_QDRANT_PORT', '6333')}/healthz"
OLLAMA_URL = os.environ.get("NEXUS_OLLAMA_URL", "http://127.0.0.1:11434") + "/api/tags"
LOG_DIR = Path(os.environ.get("NEXUS_PLUGIN_LOG_DIR", "") or Path.home() / ".nexus-memory" / "logs")


def log(message: str) -> None:
    LOG.warning(message)


def reachable(url: str, timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.status < 400
    except Exception:
        return False


def spawn_detached(argv: list[str], name: str, cwd: str, env: dict[str, str]) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    sink = open(LOG_DIR / f"{name}.log", "ab")
    flags = 0
    if os.name == "nt":
        flags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    subprocess.Popen(
        argv,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=sink,
        stderr=sink,
        creationflags=flags,
        close_fds=True,
    )
    log(f"started {name}: {argv[0]}")


def ensure_qdrant() -> None:
    if reachable(QDRANT_URL):
        return
    exe = os.environ.get("NEXUS_PLUGIN_QDRANT_EXE", "").strip()
    if not exe or not Path(exe).exists():
        log("Qdrant is not reachable and no NEXUS_PLUGIN_QDRANT_EXE is configured")
        return
    storage = os.environ.get("NEXUS_PLUGIN_QDRANT_STORAGE", "").strip()
    env = os.environ.copy()
    env.setdefault("QDRANT__SERVICE__HTTP_PORT", os.environ.get("NEXUS_QDRANT_PORT", "6333"))
    env.setdefault("QDRANT__SERVICE__HOST", os.environ.get("NEXUS_QDRANT_HOST", "127.0.0.1"))
    if storage:
        env.setdefault("QDRANT__STORAGE__STORAGE_PATH", storage)
    env.setdefault("QDRANT__TELEMETRY_DISABLED", "true")
    try:
        spawn_detached([exe], "qdrant", str(Path(exe).parent.parent), env)
    except Exception as error:  # noqa: BLE001 - fail-open, the store reports the detail
        log(f"could not start Qdrant: {error}")
        return
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if reachable(QDRANT_URL):
            log("Qdrant is up")
            return
        time.sleep(0.5)
    log("Qdrant did not come up within 60 s")


def ensure_ollama() -> None:
    if reachable(OLLAMA_URL, timeout=1.0):
        return
    exe = os.environ.get("NEXUS_PLUGIN_OLLAMA_EXE", "").strip()
    if not exe or not Path(exe).exists():
        return
    try:
        spawn_detached([exe, "serve"], "ollama", str(Path(exe).parent), os.environ.copy())
    except Exception as error:  # noqa: BLE001
        log(f"could not start Ollama: {error}")


def extract_text(blocks) -> str:
    parts = []
    for block in blocks or []:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text", "")))
    return "\n".join(parts).strip()


def build_capture_text(user: str, assistant: str, session: str, turn: int) -> str:
    header = f"[DSH session {session[:8]} · turn {turn}]"
    body = f"User: {user.strip()}\n\nAssistant: {assistant.strip()}"
    text = f"{header}\n{body}"
    if len(text) > CAPTURE_MAX_CHARS:
        text = text[: CAPTURE_MAX_CHARS - 1].rstrip() + "…"
    return text


class Bridge:
    def __init__(self) -> None:
        self._store = None
        self._lock = asyncio.Lock()

    def store(self):
        if self._store is None:
            from nexus_memory.mcp_server import MemoryStore  # imported late: needs env + Qdrant

            self._store = MemoryStore()
        return self._store

    async def handle(self, request: dict) -> dict:
        op = request.get("op")
        if op == "health":
            return {"status": "ok", "accessLevel": ACCESS_LEVEL, "category": CAPTURE_CATEGORY}
        if op == "warmup":
            async with self._lock:
                self.store()
            return {"ready": True}
        if op == "recall":
            query = str(request.get("query", "")).strip()
            if len(query) < 2:
                return []
            limit = max(1, min(int(request.get("limit", 5)), 20))
            # Auswahlregel (20.11.2026): Kategorien und Mindestpunktzahl
            # entscheidet der DIENST, nicht der Bridge. Die alte Schwelle
            # hier (RECALL_MIN_SCORE auf dem auf den Spitzenreiter
            # normalisierten Wert) konnte nie greifen: der erste Treffer hat
            # immer 1.0, der fuenfte selten unter 0.35 — sie war Zierde.
            async with self._lock:
                results = await self.store().recall(
                    query, agent_level=ACCESS_LEVEL, limit=limit,
                    categories=request.get("categories"),
                    min_score=request.get("min_score"),
                    via="plugin", actor=request.get("actor"),
                )
            memories = []
            for item in results:
                score = float(item.get("score") or 0.0)
                memories.append(
                    {
                        "id": str(item.get("id", "")),
                        "text": str(item.get("text", "")).strip(),
                        "score": round(score, 3),
                        "category": str(item.get("category", "")),
                        "source": str(item.get("source", "")),
                        "created_at": str(item.get("created_at", "")),
                        "access_level": str(item.get("access_level", "")),
                        "verification": str(item.get("verification", "")),
                        # Vergleichbare Punktzahl (Kosinus) — die Zahl, gegen
                        # die eine Mindestpunktzahl prueft.
                        "relevance": item.get("relevance"),
                    }
                )
            return memories
        if op == "capture":
            user = str(request.get("user", ""))
            assistant = str(request.get("assistant", ""))
            if len(user.strip()) < CAPTURE_MIN_CHARS or not assistant.strip():
                return {"stored": False, "reason": "too-short"}
            text = build_capture_text(user, assistant, str(request.get("session", "")), int(request.get("turn", 0)))
            async with self._lock:
                result = await self.store().remember(
                    text=text,
                    category=CAPTURE_CATEGORY,
                    access_level=ACCESS_LEVEL,
                    source="dsh-plugin",
                    source_type="session",
                    explicit=False,
                )
            return {
                "stored": isinstance(result, dict) and result.get("status") == "ok",
                "id": (result or {}).get("id", "") if isinstance(result, dict) else "",
            }
        if op == "remember":
            text = str(request.get("text", "")).strip()
            if not text:
                return {"stored": False, "reason": "empty"}
            async with self._lock:
                result = await self.store().remember(
                    text=text,
                    category=str(request.get("category", "fact")),
                    access_level=ACCESS_LEVEL,
                    source="dsh-plugin",
                    source_type="manual",
                    explicit=True,
                )
            return {"stored": isinstance(result, dict) and result.get("status") == "ok", "id": (result or {}).get("id", "")}
        raise ValueError(f"unknown op {op!r}")


async def serve() -> None:
    bridge = Bridge()
    loop = asyncio.get_running_loop()
    while True:
        line = await loop.run_in_executor(None, sys.stdin.readline)
        if not line:
            break
        line = line.strip()
        if not line:
            continue
        request_id = None
        try:
            request = json.loads(line)
            request_id = request.get("id")
            result = await bridge.handle(request)
            payload = {"id": request_id, "ok": True, "result": result}
        except Exception as error:  # noqa: BLE001 - every failure becomes a response
            LOG.exception("request failed")
            payload = {"id": request_id, "ok": False, "error": f"{type(error).__name__}: {error}"}
        _rpc_out.write(json.dumps(payload, ensure_ascii=False) + "\n")
        _rpc_out.flush()


def main() -> None:
    ensure_qdrant()
    ensure_ollama()
    try:
        asyncio.run(serve())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
