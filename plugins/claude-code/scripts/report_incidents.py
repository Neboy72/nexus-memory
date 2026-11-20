#!/usr/bin/env python3
"""Nexus Memory — failed tool calls, reported at the end of a session.

Runs as a Claude Code `Stop` hook. It reads the session transcript, takes the
tool results the harness itself marked as failed (`is_error: true`), and sends
them to the Nexus service (`POST /events`) so the nightly learner sees them.

Why the transcript and not the hook payload: `PostToolUse` carries the tool
response, but its shape differs per tool and per version, and guessing a
failure out of that text is exactly the mistake the old dreamer made. The
transcript carries the harness's own verdict, one line per tool result.

Rules, the same in every Nexus plugin:

- only what the harness marks as failed, never a guess from the text;
- tool name, error text (capped), session id — no conversation content;
- best effort: any failure here exits 0 and stays silent. A reporting hook
  that breaks a session is worse than one that misses a report.

Switch: NEXUS_REPORT_INCIDENTS=0 disables it. Default on.
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

MAX_FEHLERTEXT = 2000
MAX_MELDUNGEN = 50
TIMEOUT_S = 4.0


def _env_on(name: str, default: bool = True) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return str(value).strip().lower() not in ("0", "false", "no", "off", "")


def _cap(text: Any, limit: int) -> str:
    return " ".join(str(text or "").split())[:limit]


def _content_text(content: Any) -> str:
    """Turn a tool_result content (string or block list) into text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
            elif isinstance(block, str):
                parts.append(block)
        return " ".join(parts)
    return ""


def incidents_from_transcript(path: str, limit: int = MAX_MELDUNGEN,
                              session: str = "") -> List[Dict[str, Any]]:
    """Failed tool calls of one transcript. Pure function, no network.

    A transcript line is a JSON object. The shapes this reads:

    - ``{"type": "assistant", "message": {"content": [{"type": "tool_use",
      "id": ..., "name": ...}]}}`` — the name behind a call id;
    - ``{"type": "user", "message": {"content": [{"type": "tool_result",
      "tool_use_id": ..., "is_error": true, "content": ...}]}}`` — the
      harness's own failure mark.
    """
    names: Dict[str, str] = {}
    incidents: List[Dict[str, Any]] = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except Exception:
                    continue
                if not isinstance(entry, dict):
                    continue
                content = (entry.get("message") or {}).get("content")
                if not isinstance(content, list):
                    continue
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    kind = block.get("type")
                    if kind == "tool_use" and block.get("id") and block.get("name"):
                        names[str(block["id"])] = str(block["name"])
                    elif kind == "tool_result" and block.get("is_error") is True:
                        tool = names.get(str(block.get("tool_use_id") or ""), "")
                        text = _cap(_content_text(block.get("content")),
                                    MAX_FEHLERTEXT)
                        if not tool or not text:
                            continue
                        incidents.append({
                            "tool": tool[:64],
                            "ok": False,
                            "error": text,
                            "hint": "",
                            "session": _cap(session or Path(path).stem, 128),
                        })
                        if len(incidents) >= limit:
                            return incidents
    except Exception:
        return incidents
    return incidents


def _basis() -> str:
    return (os.environ.get("NEXUS_SERVICE_URL")
            or os.environ.get("NEXUS_SERVE_URL")
            or "http://127.0.0.1:9122").rstrip("/")


def _key() -> str:
    for candidate in ("~/.nexus-memory/agent_keys/claude-code.key",
                      "~/.nexus-memory/service_auth.key"):
        try:
            value = Path(candidate).expanduser().read_text(encoding="utf-8").strip()
            if value:
                return value
        except Exception:
            continue
    return ""


def report(incidents: Iterable[Dict[str, Any]], *, base: Optional[str] = None,
           key: Optional[str] = None, timeout: float = TIMEOUT_S) -> int:
    """Send the incidents. Returns how many the service accepted; never raises."""
    batch = list(incidents or [])
    if not batch or not _env_on("NEXUS_REPORT_INCIDENTS"):
        return 0
    target = (base or _basis()).rstrip("/")
    credential = key if key is not None else _key()
    if not credential:
        return 0
    sent = 0
    for start in range(0, len(batch), 100):
        payload = json.dumps(batch[start:start + 100], ensure_ascii=False).encode()
        request = urllib.request.Request(
            f"{target}/events", data=payload, method="POST",
            headers={"Content-Type": "application/json",
                     "x-nexus-service-key": credential})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as answer:
                body = json.loads(answer.read().decode("utf-8") or "{}")
            sent += int(body.get("stored") or 0)
        except Exception:
            break
    return sent


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--transcript", help="read this file instead of stdin")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    payload: Dict[str, Any] = {}
    if not args.transcript:
        try:
            payload = json.load(sys.stdin) or {}
        except Exception:
            payload = {}
    transcript = args.transcript or payload.get("transcript_path") or ""
    if not transcript or not os.path.exists(transcript):
        return 0
    incidents = incidents_from_transcript(
        transcript, session=str(payload.get("session_id") or ""))
    if args.dry_run:
        print(json.dumps(incidents, ensure_ascii=False, indent=1))
        return 0
    sent = report(incidents)
    if sent:
        print(f"[nexus incidents] {sent} reported", file=sys.stderr)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:
        # A reporting hook must never break a session.
        raise SystemExit(0)
