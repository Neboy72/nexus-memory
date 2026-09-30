#!/usr/bin/env python3
"""
self_report.py — server-side watchdog for agent memory-plugin health.

The Hermes-native plugin writes a per-process self-check file
(``<data-dir>/agent-selfcheck-<agent-id>.json``, legacy single
``agent-selfcheck.json``) whenever it loads. ``ok: false`` means the agent's
memory plugin is loaded but NOT working — the server used to stay silent about
that, so a broken plugin could go unnoticed for days. This module closes the
gap: it reads those reports, watches the agent registry for suspicious silence,
and raises an alert through whatever channel the operator configured.

DESIGN RULES (same spirit as health_audit / retrieval_watch):
  - READ-ONLY: never touches memories, never repairs anything.
  - FAIL-OPEN in every direction: a broken watchdog must never affect storage,
    retrieval or the server's ability to serve. Every read/log/notify path
    swallows its error.
  - Silent by default: with no channel configured, alerts are computed and
    deduped but nothing leaves the process (a headless server must not try to
    talk).
  - Conservative silence heuristic: only agents that actually used their memory
    AND are quiet within a bounded window count as "silent". Agents that only
    talk via MCP are expected to be quiet; agents quiet for longer than the
    dormant horizon have simply moved on — not an incident.

Env:
  NEXUS_SELFREPORT=0              → kill-switch, disables the daemon
  NEXUS_SELFREPORT_INTERVAL_SEC   → pass interval (default 21600 = 6 h)
  NEXUS_SELFREPORT_START_DELAY    → initial delay before the first pass (90 s)
  NEXUS_ALERT_WEBHOOK_URL         → alert webhook (fallback NEXUS_WEBHOOK_URL)
  NEXUS_ALERT_MACOS=1             → also post a macOS desktop notification
"""
import json
import logging
import math
import os
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

log = logging.getLogger("nexus.self_report")

# A broken report older than this is history, not a current alarm.
REPORT_STALE_HOURS = 168.0  # 7 days
# An agent that used its memory and has been quiet at least this long is a
# candidate for "memory not loading"; past DORMANT_HOURS it is simply unused.
SILENCE_HOURS = 96.0  # 4 days
DORMANT_HOURS = 336.0  # 14 days

DEFAULT_INTERVAL = 21600.0  # 6 h
MIN_INTERVAL = 60.0
MAX_INTERVAL = 7 * 24 * 3600.0
DEFAULT_START_DELAY = 90
STATE_RETENTION_DAYS = 30

_DEDUP_BROKEN_HOURS = 48
_DEDUP_SILENT_HOURS = 168

# Hostile/corrupt inputs must never blind the watchdog or exhaust memory:
# skip a file bigger than 1 MiB (log once) and cap how many glob matches we
# process per directory.
_MAX_FILE_BYTES = 1024 * 1024
_MAX_GLOB_MATCHES = 50
_OVERSIZE_LOGGED: "set[str]" = set()
# A timestamp further than this into the future is not "fresh" — clocks can be
# wrong or a report can be hostile, and a future ts would otherwise stay fresh
# forever and re-alert on every pass.
_FUTURE_SKEW_SEC = 300.0

# Alert payloads end up in a webhook and in an AppleScript literal; long
# hostile strings must not be forwarded verbatim.
_MAX_REASON_CHARS = 500
_MAX_AGENT_ID_CHARS = 120


def _truncate(value: Any, limit: int) -> str:
    """Cap a string at *limit* characters, marking an actual cut with an ellipsis."""
    text = str(value)
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


def _skip_if_oversized(path: Path) -> bool:
    """True when *path* exceeds the read cap (logged once at debug)."""
    try:
        size = path.stat().st_size
    except OSError:
        return False
    if size <= _MAX_FILE_BYTES:
        return False
    key = str(path)
    if key not in _OVERSIZE_LOGGED:
        _OVERSIZE_LOGGED.add(key)
        log.debug("Self-report: skipping oversized file %s (%d bytes > %d)",
                  path, size, _MAX_FILE_BYTES)
    return True


def _env_number(name: str, default: float, minimum: float) -> float:
    """Parse a numeric env var, falling back to *default* on anything unusable.

    A malformed value (``"abc"``, ``"-5"``) must never raise at import time —
    that would turn a bad config value into a startup crash of the package.
    """
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        val = float(raw)
    except (TypeError, ValueError):
        log.warning("%s=%r is not a number — using default %s", name, raw, default)
        return default
    if not math.isfinite(val):
        # "inf"/"nan" parse as floats but are unusable: NEXUS_..._START_DELAY=inf
        # made time.sleep() raise outside the loop's try and killed the thread.
        log.warning("%s=%r is not finite — using default %s", name, raw, default)
        return default
    if val < minimum:
        log.warning("%s=%s out of range (min %s) — using default %s",
                    name, val, minimum, default)
        return default
    return val


def _resolve_data_dir() -> Path:
    """Resolve the data dir; separate name so ``data_dir`` stays unshadowed."""
    env = os.environ.get("NEXUS_DATA_DIR", "").strip()
    if env:
        return Path(os.path.expanduser(env))
    return Path.home() / ".nexus-memory"


def data_dir() -> Path:
    """Nexus data directory: ``$NEXUS_DATA_DIR`` when set, else ``~/.nexus-memory``."""
    return _resolve_data_dir()


def default_registry_path() -> Path:
    """Agent registry location (lives in the same data dir as the self-checks)."""
    return data_dir() / "agents.json"


def _parse_ts(ts: Any) -> Optional[float]:
    """Epoch seconds for an ISO-8601 UTC timestamp, or None when unparsable."""
    if not isinstance(ts, str) or not ts.strip():
        return None
    value = ts.strip()
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _to_epoch(now: Union[None, float, int, datetime]) -> float:
    """Normalize an injected ``now`` (epoch number, datetime or None) to epoch."""
    if now is None:
        return time.time()
    if isinstance(now, datetime):
        return now.timestamp()
    return float(now)


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def read_agent_reports(data_dir: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Read every ``agent-selfcheck*.json`` and annotate each with ``age_hours``.

    Covers the per-agent files and the legacy single file; a corrupt or
    unreadable file is skipped with a debug log, never fatal. ``age_hours`` is
    ``inf`` when ``ts`` is missing or unparsable.
    """
    directory = Path(data_dir) if data_dir is not None else _resolve_data_dir()
    now = time.time()
    reports: List[Dict[str, Any]] = []
    try:
        paths = sorted(directory.glob("agent-selfcheck*.json"))[:_MAX_GLOB_MATCHES]
    except OSError as exc:
        log.debug("Self-report: cannot list %s: %s", directory, exc)
        return reports
    for path in paths:
        if _skip_if_oversized(path):
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.debug("Self-report: skipping unreadable %s: %s", path, exc)
            continue
        if not isinstance(data, dict):
            log.debug("Self-report: skipping non-object report %s", path)
            continue
        ts_epoch = _parse_ts(data.get("ts"))
        data["age_hours"] = (now - ts_epoch) / 3600.0 if ts_epoch is not None else float("inf")
        reports.append(data)
    return reports


def read_registry(path: Optional[Path] = None) -> Dict[str, Any]:
    """Read + parse ``agents.json``; unreadable → ``{"agents": []}`` (never raises).

    When the path resolves to the default (``$NEXUS_DATA_DIR`` unset) the
    canonical owner ``agent_detect.load_agents_registry()`` is reused so the
    watchdog reads the same file with the same locking/parse rules as the
    writer. A redirected ``NEXUS_DATA_DIR`` may point agent_detect elsewhere,
    so the local reader is kept as the fallback for that case.
    """
    if path is None and not os.environ.get("NEXUS_DATA_DIR", "").strip():
        try:
            from nexus_memory.agent_detect import load_agents_registry
            data = load_agents_registry()
            if isinstance(data, dict) and isinstance(data.get("agents"), list):
                return data
            log.debug("Self-report: agent_detect registry has an unexpected shape")
            return {"agents": []}
        except Exception as exc:
            log.debug("Self-report: agent_detect loader unavailable (%s) — "
                      "falling back to local reader", exc)
    target = Path(path) if path is not None else default_registry_path()
    if _skip_if_oversized(target):
        return {"agents": []}
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.debug("Self-report: registry unreadable at %s: %s", target, exc)
        return {"agents": []}
    if not isinstance(data, dict) or not isinstance(data.get("agents"), list):
        log.debug("Self-report: registry at %s has an unexpected shape", target)
        return {"agents": []}
    return data


def _silent_agents(registry: Dict[str, Any], now: float) -> List[Dict[str, Any]]:
    """Conservative silence detection — see module docstring for the rules."""
    out: List[Dict[str, Any]] = []
    agents = registry.get("agents") if isinstance(registry, dict) else None
    if not isinstance(agents, list):
        return out
    for agent in agents:
        if not isinstance(agent, dict):
            continue
        install_type = str(agent.get("install_type") or "")
        # MCP-only agents are expected to be quiet; only plugin installs can
        # silently lose their in-process memory provider.
        if "plugin" not in install_type:
            continue
        try:
            used = int(agent.get("reads") or 0) + int(agent.get("writes") or 0)
        except (TypeError, ValueError, OverflowError):
            # JSON Infinity/NaN in reads/writes: int() raises OverflowError
            # (or ValueError for NaN) — treat as unused, never go blind.
            used = 0
        if used <= 0:
            continue
        ts_epoch = _parse_ts(agent.get("last_seen"))
        if ts_epoch is None:
            continue
        silent_hours = (now - ts_epoch) / 3600.0
        if not math.isfinite(silent_hours):
            continue
        if SILENCE_HOURS <= silent_hours < DORMANT_HOURS:
            out.append({
                "agent_id": str(agent.get("id") or "unknown"),
                "silent_hours": silent_hours,
                "last_seen": agent.get("last_seen"),
            })
    return out


def evaluate(*, reports: Optional[List[Dict[str, Any]]] = None,
             registry: Optional[Dict[str, Any]] = None,
             now: Union[None, float, int, datetime] = None) -> Dict[str, Any]:
    """Pure evaluation of self-check reports + registry silence.

    When both ``reports`` and ``registry`` are passed this performs NO I/O, so
    it is trivially testable. Returns ``status``, every report (each with a
    ``fresh`` flag), the currently-broken agents, and the suspiciously silent
    ones. ``status`` is ``"warning"`` iff either list is non-empty.
    """
    if reports is None:
        reports = read_agent_reports()
    if registry is None:
        registry = read_registry()
    epoch = _to_epoch(now)

    annotated: List[Dict[str, Any]] = []
    broken: List[str] = []
    for report in reports or []:
        if not isinstance(report, dict):
            continue
        ts_epoch = _parse_ts(report.get("ts"))
        if ts_epoch is None or not math.isfinite(ts_epoch):
            age_hours = float("inf")
        else:
            age_hours = (epoch - ts_epoch) / 3600.0
            if not math.isfinite(age_hours) or age_hours < -(_FUTURE_SKEW_SEC / 3600.0):
                # Non-finite or more than 5 min in the future: not a current
                # alarm — a future ts would otherwise stay fresh forever.
                age_hours = float("inf")
            elif age_hours < 0:
                age_hours = 0.0
        fresh = age_hours <= REPORT_STALE_HOURS
        ok = bool(report.get("ok", True))
        agent_id = str(report.get("agent_id") or "unknown")
        annotated.append({
            "agent_id": agent_id,
            "ok": ok,
            "reason": report.get("reason") or "",
            "fix": report.get("fix") or "",
            "ts": report.get("ts"),
            "age_hours": age_hours,
            "fresh": fresh,
        })
        if not ok and fresh:
            broken.append(agent_id)

    silent = _silent_agents(registry or {"agents": []}, epoch)
    return {
        "status": "warning" if (broken or silent) else "ok",
        "reports": annotated,
        "broken_agents": broken,
        "silent_agents": silent,
    }


def alert_messages(evaluation: Dict[str, Any]) -> List[Dict[str, Any]]:
    """One alert entry per issue, with the dedup window that fits its urgency."""
    alerts: List[Dict[str, Any]] = []
    reports = {
        r.get("agent_id"): r
        for r in (evaluation.get("reports") or [])
        if isinstance(r, dict)
    }
    for raw_agent_id in evaluation.get("broken_agents") or []:
        report = reports.get(raw_agent_id) or {}
        agent_id = _truncate(raw_agent_id, _MAX_AGENT_ID_CHARS)
        reason = _truncate(report.get("reason") or "an unknown error", _MAX_REASON_CHARS)
        fix = _truncate(report.get("fix") or "reinstall the memory plugin", _MAX_REASON_CHARS)
        alerts.append({
            "key": f"broken:{agent_id}",
            "title": f"Nexus: memory plugin broken for '{agent_id}'",
            "text": (
                f"The memory plugin for agent '{agent_id}' is loaded but NOT "
                f"working: {reason}. Fix: run `{fix}` and restart the agent. "
                f"Your stored memories are safe and not lost — running the fix "
                f"restores memory."
            ),
            "dedup_hours": _DEDUP_BROKEN_HOURS,
        })
    for entry in evaluation.get("silent_agents") or []:
        agent_id = _truncate(entry.get("agent_id") or "unknown", _MAX_AGENT_ID_CHARS)
        days = float(entry.get("silent_hours") or 0.0) / 24.0
        alerts.append({
            "key": f"silent:{agent_id}",
            "title": f"Nexus: agent '{agent_id}' has been quiet",
            "text": (
                f"Agent '{agent_id}' has been quiet for {days:.1f} days. Either "
                f"it is simply not being used (then you can ignore this), or its "
                f"memory plugin is not loading. The fix only if it is the latter: "
                f"run the plugin's one-line repair command — the agent itself "
                f"shows the exact command when it starts."
            ),
            "dedup_hours": _DEDUP_SILENT_HOURS,
        })
    return alerts


def _webhook_url() -> str:
    return (os.environ.get("NEXUS_ALERT_WEBHOOK_URL", "").strip()
            or os.environ.get("NEXUS_WEBHOOK_URL", "").strip())


def _macos_enabled() -> bool:
    return os.environ.get("NEXUS_ALERT_MACOS", "") == "1" and sys.platform == "darwin"


def channel_configured() -> bool:
    """True when at least one delivery channel is set up (used for startup log)."""
    return bool(_webhook_url()) or _macos_enabled()


def _sanitize_applescript_text(text: str) -> str:
    """Collapse every control character (incl. raw newlines) to a space.

    A raw ``\\n``/``\\r``/``\\t`` inside the string literal produces an invalid
    AppleScript program, so ``display notification`` fails silently. The value
    is still passed as a single argv element, never through a shell.
    """
    return "".join(
        " " if ch < " " or ch == "\x7f" else ch for ch in str(text)
    )


def _applescript_quote(text: str) -> str:
    """Escape backslash + double quote for an AppleScript string literal.

    Control characters are stripped first so the assembled script stays a
    single valid line; the result is still passed as a single argv element —
    never through a shell — so this is about a valid AppleScript literal, not
    shell safety.
    """
    cleaned = _sanitize_applescript_text(text)
    return cleaned.replace("\\", "\\\\").replace('"', '\\"')


_ORIGINAL_URLOPEN = urllib.request.urlopen


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect — a webhook must not bounce us to another target."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


_NO_REDIRECT_OPENER: Optional[urllib.request.OpenerDirector] = None


def _redirect_safe_opener() -> urllib.request.OpenerDirector:
    """Lazily build the opener that refuses redirects."""
    global _NO_REDIRECT_OPENER
    if _NO_REDIRECT_OPENER is None:
        _NO_REDIRECT_OPENER = urllib.request.build_opener(_NoRedirect())
    return _NO_REDIRECT_OPENER


def _open_url(req: urllib.request.Request, timeout: float):
    """Open *req* without following redirects.

    ``urllib.request.urlopen`` is kept as the seam: tests (and embedders that
    already monkeypatch it) replace it, so a replaced callable is honored.
    Production goes through the no-redirect opener.
    """
    if urllib.request.urlopen is not _ORIGINAL_URLOPEN:
        return urllib.request.urlopen(req, timeout=timeout)
    return _redirect_safe_opener().open(req, timeout=timeout)


def _webhook_url_is_safe(url: str) -> bool:
    """True when *url* passed the repo's SSRF guard (scheme fallback if absent)."""
    try:
        from nexus_memory.mcp_server import _assert_ssrf_safe
        _assert_ssrf_safe(url, what="webhook url")
        return True
    except ImportError:
        return url.lower().startswith(("http://", "https://"))
    except ValueError as exc:
        log.warning("Self-report webhook rejected (%s)", exc)
        return False
    except Exception as exc:
        log.warning("Self-report webhook SSRF check failed (%s)", exc)
        return False


def notify(text: str, title: str = "") -> bool:
    """Deliver one alert through every configured channel; True if any succeeded.

    Fail-open: each channel swallows and logs its own error. With no channel
    configured this does nothing and returns False (silent by default).
    """
    delivered = False
    url = _webhook_url()
    if url and not _webhook_url_is_safe(url):
        # Fail-open: the webhook channel is unavailable, but the macOS
        # channel below may still deliver. Never post to a rejected target.
        url = ""
    if url:
        try:
            payload = {"content": f"🦊 {title}\n{text}"}
            req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            resp = _open_url(req, timeout=5)
            try:
                status = int(getattr(resp, "status", 200))
            finally:
                close = getattr(resp, "close", None)
                if close is not None:
                    try:
                        close()
                    except Exception:
                        pass
            if 200 <= status < 300:
                delivered = True
                log.info("Self-report alert delivered via webhook (%s)", status)
            else:
                log.warning("Self-report webhook returned HTTP %s", status)
        except Exception as exc:
            log.warning("Self-report webhook failed: %s", exc)
    if _macos_enabled():
        try:
            # argv, not a shell: user text never reaches an interpreter.
            script = (
                f'display notification "{_applescript_quote(text)}"'
                f' with title "{_applescript_quote(title)}"'
            )
            proc = subprocess.run(
                ["osascript", "-e", script], timeout=10, capture_output=True,
            )
            if proc.returncode == 0:
                delivered = True
                log.info("Self-report alert delivered via macOS notification")
            else:
                log.warning("Self-report macOS notification failed (rc=%s)",
                            proc.returncode)
        except Exception as exc:
            log.warning("Self-report macOS notification failed: %s", exc)
    return delivered


class SelfReportDaemon:
    """In-process loop: evaluate self-checks + registry, alert with dedup.

    Modelled on RetrievalWatch / HealthAuditor: a plain daemon thread lives
    with the server process, the kill-switch is ``NEXUS_SELFREPORT=0`` and
    every pass is wrapped so the loop can never raise out.
    """

    def __init__(self, store: Optional[Any] = None,
                 data_dir: Optional[Union[str, Path]] = None) -> None:
        self._store = store
        self._data_dir = Path(data_dir) if data_dir is not None else _resolve_data_dir()
        # True only for the unredirected default: then the registry read may
        # reuse agent_detect (the file's canonical owner) instead of a local read.
        self._use_default_data_dir = (
            data_dir is None and not os.environ.get("NEXUS_DATA_DIR", "").strip()
        )
        self._state_path = self._data_dir / "selfreport-state.json"
        self._interval = min(
            _env_number("NEXUS_SELFREPORT_INTERVAL_SEC", DEFAULT_INTERVAL, MIN_INTERVAL),
            MAX_INTERVAL,
        )
        self._start_delay = _env_number(
            "NEXUS_SELFREPORT_START_DELAY", DEFAULT_START_DELAY, 0.0)
        self._thread: Optional[threading.Thread] = None

    # ── one pass ──────────────────────────────────────────────────────
    def run_pass(self, now: Union[None, float, int, datetime] = None) -> Dict[str, Any]:
        """Evaluate once, deliver every non-suppressed alert, persist dedup state."""
        reports = read_agent_reports(self._data_dir)
        registry = read_registry(
            None if self._use_default_data_dir else self._data_dir / "agents.json"
        )
        evaluation = evaluate(reports=reports, registry=registry, now=now)
        alerts = alert_messages(evaluation)
        epoch = _to_epoch(now)
        state = self._load_state()
        delivered: List[str] = []
        for alert in alerts:
            key = alert["key"]
            last = state.get(key)
            if last is not None and (epoch - last) < alert["dedup_hours"] * 3600:
                continue
            if not notify(alert["text"], alert["title"]):
                # Delivery failed on every channel: do NOT record the dedup
                # entry, otherwise a real alert is suppressed for 48 h / 7 d
                # and the user never learns about a broken agent. Retry next pass.
                log.warning("Self-report alert delivery failed (will retry): %s", key)
                continue
            state[key] = epoch
            delivered.append(key)
        if delivered:
            self._save_state(state, epoch)
        return {"evaluation": evaluation, "delivered": delivered}

    # ── dedup state ───────────────────────────────────────────────────
    def _load_state(self) -> Dict[str, float]:
        if _skip_if_oversized(self._state_path):
            return {}
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        state: Dict[str, float] = {}
        for key, ts in data.items():
            parsed = _parse_ts(ts)
            if parsed is None and isinstance(ts, (int, float)) and not isinstance(ts, bool):
                try:
                    parsed = float(ts)
                except (TypeError, ValueError, OverflowError):
                    parsed = None
            # A non-finite value (JSON Infinity) would make _iso() raise
            # OverflowError, so the state would never be saved and every pass
            # would re-alert. Drop it instead.
            if parsed is not None and math.isfinite(parsed):
                state[str(key)] = parsed
        return state

    def _save_state(self, state: Dict[str, float], now: float) -> None:
        """Atomically persist dedup state, pruning entries older than 30 days."""
        cutoff = now - STATE_RETENTION_DAYS * 86400
        pruned = {
            k: v for k, v in state.items()
            if isinstance(v, (int, float)) and math.isfinite(v) and v >= cutoff
        }
        tmp = self._state_path.with_name(f"{self._state_path.name}.tmp-{os.getpid()}")
        try:
            self._data_dir.mkdir(parents=True, exist_ok=True)
            tmp.write_text(
                json.dumps({k: _iso(v) for k, v in pruned.items()}),
                encoding="utf-8",
            )
            os.replace(tmp, self._state_path)
        except (OSError, OverflowError, ValueError) as exc:
            log.debug("Self-report: cannot persist dedup state: %s", exc)
            try:
                tmp.unlink()
            except OSError:
                pass

    # ── daemon loop ───────────────────────────────────────────────────
    def start(self) -> None:
        """Start the non-blocking loop; kill-switch NEXUS_SELFREPORT=0 disables it."""
        if os.environ.get("NEXUS_SELFREPORT", "1") == "0":
            log.info("Self-report daemon disabled (NEXUS_SELFREPORT=0)")
            return
        if self._thread is not None and self._thread.is_alive():
            return

        def _loop() -> None:
            if self._start_delay > 0:
                time.sleep(self._start_delay)
            while True:
                try:
                    self.run_pass()
                except Exception as exc:  # never break the server
                    log.warning("Self-report pass failed: %s", exc)
                time.sleep(self._interval)

        thread = threading.Thread(target=_loop, name="nexus-self-report", daemon=True)
        self._thread = thread
        thread.start()
        log.info(
            "Self-report daemon started (interval %.1f h, notifications %s)",
            self._interval / 3600.0,
            "configured" if channel_configured() else "disabled",
        )
