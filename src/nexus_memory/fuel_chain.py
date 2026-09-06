#!/usr/bin/env python3
"""fuel_chain.py — multi-provider LLM fuel discovery for the consolidation daemon.

Nebo's tank-station design (2026-09-06): the daemon is a MITFAHRER — it never
needs its own account or setup step. At fuel time it tries stations in
cheapness order, skipping dead ones, waiting (fail-safe) if all are closed:

  1. local Ollama            (free,   NEXUS_OLLAMA_BASE,  default 127.0.0.1:11434)
  2. OpenRouter              (user's existing OPENROUTER_API_KEY, cheapest model)
  3. OpenAI-compatible keys  (OPENAI_API_KEY / NOUS_API_KEY / NEXUS_FUEL_BASE+KEY)
  4. all closed              -> None; caller sleeps and retries next tick

Cost guard: NEXUS_FUEL_BUDGET_USD (default 1.00/month). The tracker counts the
provider-reported API usage per month in ~/.nexus-memory/fuel_spend.json
(OpenRouter's actual cost wins when present; otherwise model-specific
reference prices on token counts; unknown models use a deliberately
conservative fallback price so the budget never under-counts).

Safety invariants (fail-closed on the PAID side, fail-safe for the daemon):

* get_fuel returns a DISPATCHER, not a pre-bound generator: stations are
  probed again at generation time, a provider error during generation moves
  on to the next eligible station, and when nothing can serve the dispatcher
  raises FuelUnavailableError — callers treat that exactly like None
  (sleep + retry, never crash).
* The monthly budget is re-checked before EVERY paid call. Each paid call
  first reserves a conservative max-spend slot under a cross-process file
  lock (fcntl.flock) and reconciles the reservation with the real cost
  afterwards, so a long-lived wrapper can never overspend the month.
* A missing/corrupt budget state, a failed persistence or an invalid budget
  value locks PAID stations only (free Ollama keeps the daemon running).
"""
import fcntl
import json
import logging
import math
import os
import threading
import time
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

log = logging.getLogger("nexus.fuel")

FUEL_BASE = os.environ.get("NEXUS_FUEL_BASE", "")           # optional explicit endpoint
FUEL_KEY = os.environ.get("NEXUS_FUEL_KEY", "")             # optional explicit key
FUEL_MODEL = os.environ.get("NEXUS_FUEL_MODEL", "")         # optional explicit model

DEFAULT_FUEL_BUDGET_USD = 1.00
DEFAULT_MAX_PER_CALL_USD = 0.01  # conservative ceiling per paid call


def _validate_amount(value: Any) -> Optional[float]:
    """Accept only finite, non-negative numbers (bools rejected)."""
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed) or parsed < 0.0:
        return None
    return parsed


def _budget_from_env() -> float:
    """Validate NEXUS_FUEL_BUDGET_USD; fall back to the default on garbage."""
    raw = os.environ.get("NEXUS_FUEL_BUDGET_USD", "")
    if raw.strip() == "":
        return DEFAULT_FUEL_BUDGET_USD
    parsed = _validate_amount(raw)
    if parsed is None:
        log.warning("fuel: invalid NEXUS_FUEL_BUDGET_USD=%r — using default %.2f",
                    raw, DEFAULT_FUEL_BUDGET_USD)
        return DEFAULT_FUEL_BUDGET_USD
    return parsed


def _max_per_call_from_env() -> float:
    raw = os.environ.get("NEXUS_FUEL_MAX_PER_CALL_USD", "")
    if raw.strip() == "":
        return DEFAULT_MAX_PER_CALL_USD
    parsed = _validate_amount(raw)
    if parsed is None:
        log.warning("fuel: invalid NEXUS_FUEL_MAX_PER_CALL_USD=%r — using default %.4f",
                    raw, DEFAULT_MAX_PER_CALL_USD)
        return DEFAULT_MAX_PER_CALL_USD
    return parsed


FUEL_BUDGET_USD = _budget_from_env()
MAX_PER_CALL_USD = _max_per_call_from_env()
FUEL_STATE = Path(os.environ.get(
    "NEXUS_FUEL_STATE", str(Path.home() / ".nexus-memory" / "fuel_spend.json")))

# Per-model reference prices (USD per 1M tokens, input/output) — used only
# when the API does not report usage. Keyed by exact model id, lower-case.
# The budget tracker never hardcodes a flagship: the chain always picks the
# cheapest model per station, and unknown models hit the conservative fallback.
MODEL_PRICES: Dict[str, Dict[str, float]] = {
    "openai/gpt-5.6-luna": {"in": 0.10, "out": 0.50},   # openrouter mini tier
    "gpt-5.6-luna": {"in": 0.20, "out": 1.20},          # first-party mini tier
}
# Unknown models: deliberately expensive so an arbitrarily priced model can
# never silently under-count against the monthly budget.
FALLBACK_MODEL_PRICE = {"in": 0.50, "out": 2.00}

BUDGET_LOCK = threading.RLock()   # in-process guard (threads)
_persist_failed = False           # latched: paid stations stay locked until repair


def _latch_persist_failure() -> None:
    global _persist_failed
    _persist_failed = True


def _current_month() -> str:
    return time.strftime("%Y-%m")


def _state_lock_path() -> Path:
    state = Path(str(FUEL_STATE))
    return state.with_suffix(state.suffix + ".lock")


@contextmanager
def _budget_lock():
    """Cross-process flock on <state>.lock, nested inside the in-process lock.

    If flock is unavailable on the running platform the in-process lock still
    guards the single daemon; the file lock makes concurrent daemon + MCP
    server processes safe.
    """
    with BUDGET_LOCK:
        handle = None
        try:
            try:
                _state_lock_path().parent.mkdir(parents=True, exist_ok=True)
            except Exception:
                pass
            handle = open(_state_lock_path(), "a+")
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            except Exception:
                pass  # keep going under the in-process lock
            yield
        finally:
            if handle is not None:
                try:
                    handle.close()
                except Exception:
                    pass


def _damaged_state() -> Dict[str, Any]:
    """State for a month whose ledger is unreadable/corrupt: treated as
    over-budget so paid stations stay locked (fail-closed)."""
    return {"year_month": _current_month(), "spent_usd": math.inf}


def _load_state_unlocked() -> Dict[str, Any]:
    """Read the budget state; a missing file is a fresh month, a CORRUPT one
    is over-budget. Month turnover resets the spend to 0 for comparisons."""
    try:
        raw = FUEL_STATE.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {"year_month": _current_month(), "spent_usd": 0.0}
    except Exception as exc:
        log.warning("fuel: budget state unreadable (%s) — treating as damaged", exc)
        return _damaged_state()
    try:
        parsed = json.loads(raw)
    except Exception as exc:
        log.warning("fuel: budget state corrupt (%s) — treating as damaged", exc)
        return _damaged_state()
    if not isinstance(parsed, dict):
        log.warning("fuel: budget state has unexpected shape — treating as damaged")
        return _damaged_state()
    if parsed.get("year_month") != _current_month():
        # Month turned over: previous spend no longer counts.
        return {"year_month": _current_month(), "spent_usd": 0.0}
    spend = _validate_amount(parsed.get("spent_usd"))
    if spend is None:
        log.warning("fuel: budget state has invalid spend %r — treating as damaged",
                    parsed.get("spent_usd"))
        return _damaged_state()
    return {"year_month": parsed["year_month"], "spent_usd": spend}


def _persist_state_unlocked(state: Dict[str, Any]) -> bool:
    """Atomically write the budget state (temp file + fsync + os.replace).

    Returns False on failure and latches the paid-station lock (fail-closed):
    a spend we could not persist must not be treated as 'not spent'.
    """
    state_path = Path(str(FUEL_STATE))
    tmp_path = state_path.parent / (state_path.name + ".tmp")
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp_path, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(state))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, state_path)
        global _persist_failed
        _persist_failed = False
        return True
    except Exception as exc:
        log.warning("fuel: persisting budget state failed (%s) — paid stations locked", exc)
        _latch_persist_failure()
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except Exception:
            pass
        return False


def budget_exhausted() -> bool:
    """True when paid-fuel calls are blocked for this month (free stations
    unaffected).

    The current month's spend is always compared against the budget — a
    missing state file or a month turnover counts as 0 spent, so an explicit
    NEXUS_FUEL_BUDGET_USD=0 blocks paid calls from the very first request.
    Corrupt/unreadable state and invalid runtime budgets count as exhausted.
    """
    budget = _validate_amount(FUEL_BUDGET_USD) if not isinstance(FUEL_BUDGET_USD, bool) else None
    if budget is None:
        return True  # fail closed on invalid runtime budget
    try:
        with _budget_lock():
            state = _load_state_unlocked()
    except Exception as exc:
        log.warning("fuel: budget check failed (%s) — assuming exhausted", exc)
        return True
    return float(state.get("spent_usd", 0.0)) >= budget


def reserve_budget(max_spend: Optional[float] = None) -> Optional[float]:
    """Reserve budget for ONE paid request under the shared lock.

    Returns the reserved (conservative) amount, or None when the reservation
    must be refused: budget spent, damaged state, failed persistence or a
    non-positive cap. The reservation is persisted immediately, so parallel
    processes cannot both spend the last free slot. Callers reconcile with
    commit_budget() (or release_budget() on abort).
    """
    cap = _validate_amount(MAX_PER_CALL_USD if max_spend is None else max_spend)
    if cap is None or cap <= 0.0:
        return None
    ceiling = _validate_amount(MAX_PER_CALL_USD)
    if ceiling is None or ceiling <= 0.0:
        return None
    cap = min(cap, ceiling)  # the configured per-call ceiling is a hard limit
    try:
        with _budget_lock():
            if _persist_failed:
                log.warning("fuel: budget persistence broken — paid calls locked")
                return None
            budget = _validate_amount(FUEL_BUDGET_USD) if not isinstance(FUEL_BUDGET_USD, bool) else None
            if budget is None:
                return None
            state = _load_state_unlocked()
            spent = float(state.get("spent_usd", 0.0))
            if not math.isfinite(spent) or spent < 0.0:
                return None  # damaged ledger
            remaining = budget - spent
            if remaining <= 0.0:
                log.info("fuel: monthly budget spent (%.4f/%.4f USD) — paid calls paused",
                         spent, budget)
                return None
            reserved = min(cap, remaining)
            if reserved <= 0.0:
                return None
            state["year_month"] = _current_month()
            state["spent_usd"] = spent + reserved
            if not _persist_state_unlocked(state):
                return None
            return reserved
    except Exception as exc:
        log.warning("fuel: budget reservation failed (%s) — refusing paid call", exc)
        _latch_persist_failure()
        return None


def commit_budget(reserved: Optional[float], actual: Optional[float]) -> None:
    """Reconcile a reservation with the real cost of the completed call.

    Unknown actual cost keeps the conservative reservation (never under-count).
    If the month turned over while the request was in flight, the real cost is
    recorded for the NEW month so the budget comparison still sees it.
    """
    actual_spend = _validate_amount(actual) if actual is not None else None
    if actual_spend is None:
        return  # keep the conservative reservation in place
    try:
        with _budget_lock():
            state = _load_state_unlocked()
            spent = float(state.get("spent_usd", 0.0))
            if state.get("year_month") != _current_month() \
                    or not math.isfinite(spent) or spent < 0.0:
                new_spent = actual_spend
            else:
                res = _validate_amount(reserved) if reserved is not None else None
                new_spent = max(spent - (res or 0.0), 0.0) + actual_spend
            state["year_month"] = _current_month()
            state["spent_usd"] = new_spent
            _persist_state_unlocked(state)
    except Exception as exc:
        log.warning("fuel: budget commit failed (%s) — paid stations locked", exc)
        _latch_persist_failure()


def release_budget(reserved: Optional[float]) -> None:
    """Give back a reservation whose paid call never happened.

    A failed release only over-counts spend (conservative direction), so it
    does not lock paid stations.
    """
    amount = _validate_amount(reserved) if reserved is not None else None
    if amount is None or amount <= 0.0:
        return
    try:
        with _budget_lock():
            state = _load_state_unlocked()
            spent = float(state.get("spent_usd", 0.0))
            if state.get("year_month") == _current_month() \
                    and math.isfinite(spent) and spent >= 0.0:
                state["spent_usd"] = max(spent - amount, 0.0)
                _persist_state_unlocked(state)
    except Exception as exc:
        log.warning("fuel: budget release failed (%s) — keeping reservation", exc)


def _budget_add(usd: float) -> None:
    """Legacy direct spend add (kept for tests/back-compat): locked, atomic,
    amount re-validated."""
    amount = _validate_amount(usd)
    if amount is None or amount <= 0.0:
        return
    try:
        with _budget_lock():
            state = _load_state_unlocked()
            spent = float(state.get("spent_usd", 0.0))
            if not math.isfinite(spent) or spent < 0.0:
                spent = 0.0
            state["year_month"] = _current_month()
            state["spent_usd"] = spent + amount
            _persist_state_unlocked(state)
    except Exception as exc:
        log.warning("fuel: budget add failed (%s)", exc)


class FuelUnavailableError(RuntimeError):
    """Raised by the dispatching generate function when no eligible station
    could serve the request at generation time. Callers must treat it like
    get_fuel() returning None: sleep and retry — never crash the daemon."""


class Station:
    """One fuel station: name + (probe, generate, cost_estimator)."""

    def __init__(self, name: str, probe: Callable[[], bool],
                 generate: Optional[Callable[[str], str]],
                 estimate: Optional[Callable[..., float]] = None,
                 paid: bool = False,
                 model: str = ""):
        self.name = name
        self.probe = probe
        self.generate = generate
        self.estimate = estimate
        self.paid = paid
        self.model = model
        self.last_usage: Optional[Dict[str, Any]] = None  # provider usage of the last call


def _openai_compat_generate(base: str, key: str, model: str, prompt: str,
                            timeout: int = 120) -> Tuple[str, Optional[Dict[str, Any]]]:
    """Minimal OpenAI-compatible /v1/chat/completions call (stations #2 and #3).

    Returns (text, usage) so the budget tracker can bill the provider-reported
    token usage (and OpenRouter's actual cost when present) instead of guessing.
    """
    body = json.dumps({"model": model, "temperature": 0.1,
                       "messages": [{"role": "user", "content": prompt}]}).encode()
    req = urllib.request.Request(
        base.rstrip("/") + "/chat/completions", data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read())
        usage = data.get("usage")
        text = data["choices"][0]["message"]["content"] or ""
        return text, usage if isinstance(usage, dict) else None


def _token_count(*candidates: Any) -> Optional[float]:
    for value in candidates:
        parsed = _validate_amount(value)
        if parsed is not None:
            return parsed
    return None


def _model_prices(model: str) -> Dict[str, float]:
    key = (model or "").strip().lower()
    if key:
        for name, prices in MODEL_PRICES.items():
            if name.lower() == key:
                return prices
    return FALLBACK_MODEL_PRICE


def _estimate_cost(model: str, prompt: str, out: str,
                   usage: Optional[Dict[str, Any]] = None) -> float:
    """Cost of one paid call.

    Priority: OpenRouter's reported actual cost -> provider token usage at
    model-specific prices -> conservative char-based estimate (~4 chars/token).
    Unknown models use the expensive fallback price so the budget never
    under-counts.
    """
    prices = _model_prices(model)
    in_tok = out_tok = None
    if isinstance(usage, dict):
        # Actual cost reported by the API (OpenRouter: usage.cost, USD).
        for key in ("cost", "total_cost"):
            real = _validate_amount(usage.get(key))
            if real is not None and real > 0.0:
                return real
        in_tok = _token_count(usage.get("prompt_tokens"), usage.get("input_tokens"))
        out_tok = _token_count(usage.get("completion_tokens"), usage.get("output_tokens"))
    if in_tok is None:
        in_tok = max(len(prompt or "") // 4, 1)
    if out_tok is None:
        out_tok = max(len(out or "") // 4, 1)
    return (in_tok * prices["in"] + out_tok * prices["out"]) / 1_000_000


def build_chain(ollama_base: str, ollama_model: str,
                ollama_generate: Optional[Callable[[str], str]] = None) -> List[Station]:
    """Build the station list in cheapness order. Injected ollama_generate keeps
    tests mockable; production passes consolidation._ollama_generate."""
    stations: List[Station] = []

    # ── 1. local Ollama (free) ──────────────────────────────────────
    injected = ollama_generate is not None
    def _probe() -> bool:
        if injected:
            return True  # injected generate = trusted open station (test/embed mode)
        try:
            req = urllib.request.Request(ollama_base + "/api/tags")
            with urllib.request.urlopen(req, timeout=5):
                return True
        except Exception:
            return False
    stations.append(Station(
        "ollama", _probe,
        ollama_generate or (lambda p: _default_ollama_generate(ollama_base, ollama_model, p)),
        paid=False, model=ollama_model))

    # ── 2/3. keyed stations, cheap-model-first, only if a key exists ─
    openrouter_key = os.environ.get("OPENROUTER_API_KEY", "")
    if openrouter_key:
        stations.append(_openai_compat_station(
            "openrouter", "https://openrouter.ai/api/v1", openrouter_key,
            os.environ.get("NEXUS_FUEL_MODEL_OPENROUTER", "openai/gpt-5.6-luna")))
    openai_key = os.environ.get("OPENAI_API_KEY", "")
    if openai_key:
        stations.append(_openai_compat_station(
            "openai", "https://api.openai.com/v1", openai_key,
            os.environ.get("NEXUS_FUEL_MODEL_OPENAI", "gpt-5.6-luna")))
    nous_key = os.environ.get("NOUS_API_KEY", "")
    if nous_key:
        stations.append(_openai_compat_station(
            "nous", "https://portal.nousresearch.com/v1", nous_key,
            os.environ.get("NEXUS_FUEL_MODEL_NOUS", "gpt-5.6-luna")))
    fuel_base = os.environ.get("NEXUS_FUEL_BASE", "")
    fuel_key = os.environ.get("NEXUS_FUEL_KEY", "")
    fuel_model = os.environ.get("NEXUS_FUEL_MODEL", "")
    if fuel_base and fuel_key:  # explicit station always wins its place (after ollama)
        stations.insert(1, _openai_compat_station("custom", fuel_base, fuel_key,
                                                  fuel_model or "gpt-5.6-luna"))
    return stations


def _openai_compat_station(name: str, base: str, key: str, model: str) -> Station:
    # The probe is a cheap pre-check only: real availability is validated at
    # generation time by the dispatcher, which handles provider errors.
    station = Station(name, probe=lambda: True, generate=None,
                      estimate=None, paid=True, model=model)

    def _estimate(prompt: str, out: str, usage: Optional[Dict[str, Any]] = None) -> float:
        return _estimate_cost(station.model, prompt, out, usage)

    def _gen(prompt: str, _st: Station = station) -> str:
        result = _openai_compat_generate(base, key, _st.model, prompt)
        if isinstance(result, tuple) and len(result) == 2:
            text, usage = result
        else:  # legacy plain-string mocks / wrappers: no usage data
            text, usage = result, None
        _st.last_usage = usage if isinstance(usage, dict) else None
        return text

    station.estimate = _estimate
    station.generate = _gen
    return station


def _default_ollama_generate(base: str, model: str, prompt: str) -> str:
    body = json.dumps({"model": model, "prompt": prompt, "stream": False,
                       "options": {"temperature": 0.1}}).encode()
    req = urllib.request.Request(base + "/api/generate", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read()).get("response", "")


def _paid_station_allowed(station: Station) -> bool:
    """Budget + persistence gate, re-evaluated before every paid call."""
    if _persist_failed:
        log.warning("fuel: budget state persistence broken — paid station %s locked",
                    station.name)
        return False
    if budget_exhausted():
        log.info("fuel: budget exhausted — skipping paid station %s", station.name)
        return False
    return True


def _call_station(station: Station, prompt: str) -> str:
    """One generation with reservation + reconciliation (paid) or plain
    generation (free). Raises when the paid call cannot be afforded."""
    if not station.paid:
        station.last_usage = None
        gen = station.generate
        if gen is None:
            raise FuelUnavailableError(f"free station {station.name} has no generator")
        return gen(prompt)

    reserved = reserve_budget()
    if reserved is None:
        # Budget ran out between get_fuel() and this call — fail closed.
        raise FuelUnavailableError(
            f"budget reservation refused for paid station {station.name}")
    try:
        out = station.generate(prompt)
    except Exception:
        release_budget(reserved)
        raise
    estimate = None
    try:
        if station.estimate is not None:
            estimate = station.estimate(prompt, out, station.last_usage)
        else:
            estimate = _estimate_cost(station.model, prompt, out, station.last_usage)
    except Exception as exc:
        log.warning("fuel: cost estimate failed for %s (%s) — keeping reservation",
                    station.name, exc)
        estimate = None
    if estimate is None or not isinstance(estimate, (int, float)) \
            or not math.isfinite(float(estimate)) or estimate < 0.0:
        commit_budget(reserved, None)  # unknown cost: keep conservative reservation
    else:
        commit_budget(reserved, float(estimate))
    return out


def get_fuel(ollama_base: str, ollama_model: str,
             ollama_generate: Optional[Callable[[str], str]] = None
             ) -> Optional[Callable[[str], str]]:
    """Return a generate DISPATCHER from the cheapest currently-open station,
    or None when no station can serve right now. Never raises.

    Unlike a plain generator, the dispatcher re-probes stations and re-checks
    the budget AT GENERATION TIME, handles provider errors by moving on to
    the next eligible station, and raises FuelUnavailableError when a call
    cannot be served — callers treat that exactly like None (sleep + retry).
    """
    chain = build_chain(ollama_base, ollama_model, ollama_generate)
    if not chain:
        log.warning("fuel: no stations configured — daemon sleeps until next tick")
        return None

    def dispatch(prompt: str) -> str:
        failures: List[str] = []
        for station in chain:
            if station.paid and not _paid_station_allowed(station):
                failures.append(f"{station.name}: budget/persistence lock")
                continue
            try:
                if not station.probe():
                    failures.append(f"{station.name}: closed (probe failed)")
                    log.info("fuel: station %s closed (probe failed)", station.name)
                    continue
            except Exception as exc:
                failures.append(f"{station.name}: probe error: {exc}")
                log.info("fuel: station %s probe error: %s", station.name, exc)
                continue
            try:
                out = _call_station(station, prompt)
            except FuelUnavailableError as exc:
                failures.append(f"{station.name}: {exc}")
                continue
            except Exception as exc:
                # Provider failed AT GENERATION TIME (auth, missing model,
                # dead endpoint) — try the next eligible station instead of
                # returning a broken generator.
                failures.append(f"{station.name}: generate error: {exc}")
                log.info("fuel: station %s failed at generation: %s", station.name, exc)
                continue
            log.info("fuel: used station %s", station.name)
            return out
        log.warning("fuel: all stations failed at generation (%s) — daemon sleeps",
                    "; ".join(failures) or "no stations")
        raise FuelUnavailableError("; ".join(failures) or "no fuel station available")

    # Pre-flight: keep the historical None contract when nothing is even
    # reachable right now (the dispatcher still re-checks everything per call).
    for station in chain:
        if station.paid and not _paid_station_allowed(station):
            continue
        try:
            if station.probe():
                return dispatch
        except Exception:
            continue
    log.warning("fuel: all stations closed — daemon sleeps until next tick")
    return None