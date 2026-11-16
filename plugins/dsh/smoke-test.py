"""End-to-end check for the native Nexus Memory bridge.

    python plugins/nexus-memory/smoke-test.py

Starts ``nexus_bridge.py`` exactly like the plugin does, then exercises
health, remember, recall and capture over the newline-delimited JSON protocol.
Exit code 0 means Qdrant, the embedding provider, and the Nexus store all work.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

PLUGIN_DIR = Path(__file__).resolve().parent
WS = PLUGIN_DIR.parent.parent
BRIDGE = PLUGIN_DIR / "nexus_bridge.py"

env = os.environ.copy()
env.update(
    {
        "PYTHONPATH": ";".join(
            [
                str(WS / ".nexus" / "nexus-memory"),
                str(WS / ".nexus" / "nexus-memory" / "src"),
                str(WS / ".nexus" / "pylibs"),
            ]
        ),
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUNBUFFERED": "1",
        "NEXUS_DATA_DIR": str(WS / ".nexus" / "data"),
        "NEXUS_QDRANT_HOST": "127.0.0.1",
        "NEXUS_QDRANT_PORT": "6333",
        "NEXUS_EMBEDDING_PROVIDER": "ollama",
        "NEXUS_AGENT_ID": "dsh",
        "NEXUS_PLUGIN_ACCESS_LEVEL": "private",
        "NEXUS_PLUGIN_CAPTURE_CATEGORY": "session",
        "NEXUS_PLUGIN_QDRANT_EXE": str(WS / ".nexus" / "qdrant" / "qdrant.exe"),
        "NEXUS_PLUGIN_QDRANT_STORAGE": str(WS / ".nexus" / "qdrant-storage"),
        "NEXUS_PLUGIN_OLLAMA_EXE": str(Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama.exe"),
        "NEXUS_PLUGIN_LOG_DIR": str(WS / ".nexus" / "logs"),
    }
)

proc = subprocess.Popen(
    [sys.executable, str(BRIDGE)],
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    env=env,
    text=True,
    encoding="utf-8",
    bufsize=1,
)


def call(request_id: int, op: str, timeout: float = 180.0, **payload):
    proc.stdin.write(json.dumps({"id": request_id, "op": op, **payload}) + "\n")
    proc.stdin.flush()
    deadline = time.time() + timeout
    while time.time() < deadline:
        line = proc.stdout.readline()
        if not line:
            raise RuntimeError("bridge closed stdout")
        message = json.loads(line)
        if message.get("id") == request_id:
            if not message.get("ok"):
                raise RuntimeError(message.get("error"))
            return message["result"]
    raise TimeoutError(op)


def show(label: str, value) -> None:
    text = json.dumps(value, ensure_ascii=False)
    print(f"{label}: {text[:500]}")


def main() -> int:
    show("health", call(1, "health"))

    stamp = time.strftime("%H:%M:%S")
    show(
        "remember",
        call(
            2,
            "remember",
            text=f"Smoke-Test {stamp}: das native Nexus-Plugin schreibt direkt in die Collection.",
            category="fact",
        ),
    )

    time.sleep(1.0)
    recalled = call(3, "recall", query="natives Nexus Plugins schreibt Collection", limit=3)
    show("recall", recalled)

    show(
        "capture",
        call(
            4,
            "capture",
            session="smoke-test-session",
            turn=1,
            user="Wie binde ich Nexus Memory nativ in den Harness ein?",
            assistant="Das Plugin injiziert vor dem ersten Schritt Treffer und speichert den Turn am Ende.",
        ),
    )

    time.sleep(1.5)
    captured = call(5, "recall", query="Nexus nativ in den Harness einbinden", limit=3)
    show("recall-captured", captured)

    proc.stdin.close()
    proc.wait(timeout=20)
    ok = bool(recalled) and bool(captured)
    print("RESULT:", "ok" if ok else "failed")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
