#!/usr/bin/env python3
"""fuel_chain.py — multi-provider LLM fuel discovery for the consolidation daemon.

Nebo's tank-station design (2026-09-06): the daemon is a MITFAHRER — it never
needs its own account or setup step. At fuel time it tries stations in
cheapness order, skipping dead ones, waiting (fail-safe) if all are closed:

  1. local Ollama            (free,   NEXUS_OLLAMA_BASE,  default 127.0.0.1:11434)
  2. OpenRouter              (user's existing OPENROUTER_API_KEY, cheapest model)
  3. OpenAI-compatible keys  (OPENAI_API_KEY / NOUS_API_KEY / NEXUS_FUEL_BASE+KEY)
  4. all closed              -> None; caller sleeps and retries next tick

Cost guard: NEXUS_FUEL_BUDGET_USD (default 1.00/month). The tracker counts
estimated spend per month in ~/.nexus-memory/fuel_spend.json; when the cap is
hit only paid stations pause (free Ollama keeps working) and the daemon logs
one clear line. Cheapest available model per station is always chosen — never
the user's flagship.
"""
import json
import logging
import os
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

log = logging.getLogger("nexus.fuel")

FUEL_BASE = os.environ.get("NEXUS_FUEL_BASE", "")           # optional explicit endpoint
FUEL_KEY = os.environ.get("NEXUS_FUEL_KEY", "")             # optional explicit key
FUEL_MODEL = os.environ.get("NEXUS_FUEL_MODEL", "")         # optional explicit model
FUEL_BUDGET_USD = float(os.environ.get("NEXUS_FUEL_BUDGET_USD", "1.00"))
FUEL_STATE = Path(os.environ.get(
    "NEXUS_FUEL_STATE", str(Path.home() / ".nexus-memory" / "fuel_spend.json")))

# Cheap chat tier per provider (input/output USD per 1M tokens, Sept 2026 list prices).
# The chain always picks the CHEAPEST station first; within a station these are
# reference prices for the budget tracker, not hardcoded model names.
STATION_PRICES: Dict[str, Dict[str, tuple]] = {
    "openrouter": {"in": 0.10, "out": 0.50},   # mini tier ballpark
    "openai": {"in": 0.20, "out": 1.20},       # luna tier
    "nous": {"in": 0.10, "out": 0.50},
}

def _budget_state() -> Dict[str, Any]:
    try:
        return json.loads(FUEL_STATE.read_text(encoding="utf-8"))
    except Exception:
        return {"year_month": "", "spent_usd": 0.0}

def budget_exhausted() -> bool:
    """True when the monthly paid-fuel budget is spent (free stations unaffected)."""
    ym = time.strftime("%Y-%m")
    s = _budget_state()
    return s.get("year_month") == ym and float(s.get("spent_usd", 0.0)) >= FUEL_BUDGET_USD

def _budget_add(usd: float) -> None:
    ym = time.strftime("%Y-%m")
    s = _budget_state()
    if s.get("year_month") != ym:
        s = {"year_month": ym, "spent_usd": 0.0}
    s["spent_usd"] = float(s.get("spent_usd", 0.0)) + usd
    try:
        FUEL_STATE.parent.mkdir(parents=True, exist_ok=True)
        FUEL_STATE.write_text(json.dumps(s), encoding="utf-8")
    except Exception:
        pass

class Station:
    """One fuel station: name + (probe, generate, cost_estimator)."""

    def __init__(self, name: str, probe: Callable[[], bool],
                 generate: Callable[[str], str],
                 estimate: Optional[Callable[[str, str], float]] = None,
                 paid: bool = False):
        self.name = name
        self.probe = probe
        self.generate = generate
        self.estimate = estimate
        self.paid = paid

def _openai_compat_generate(base: str, key: str, model: str, prompt: str,
                            timeout: int = 120) -> str:
    """Minimal OpenAI-compatible /v1/chat/completions call (station #2 and #3)."""
    body = json.dumps({"model": model, "temperature": 0.1,
                       "messages": [{"role": "user", "content": prompt}]}).encode()
    req = urllib.request.Request(
        base.rstrip("/") + "/chat/completions", data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read())
        usage = data.get("usage") or {}
        # rough actual-cost accounting (per-call estimate is subtracted on tick end)
        return data["choices"][0]["message"]["content"] or ""

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
        paid=False))

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

def _default_ollama_generate(base: str, model: str, prompt: str) -> str:
    body = json.dumps({"model": model, "prompt": prompt, "stream": False,
                       "options": {"temperature": 0.1}}).encode()
    req = urllib.request.Request(base + "/api/generate", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read()).get("response", "")

def _openai_compat_station(name: str, base: str, key: str, model: str) -> Station:
    ref = STATION_PRICES.get(name, {"in": 0.2, "out": 1.0})
    def _estimate(prompt: str, out: str) -> float:
        in_tok, out_tok = max(len(prompt) // 4, 1), max(len(out) // 4, 1)
        return (in_tok * ref["in"] + out_tok * ref["out"]) / 1_000_000
    def _gen(prompt: str) -> str:
        return _openai_compat_generate(base, key, model, prompt)
    return Station(name, probe=lambda: True, generate=_gen, estimate=_estimate, paid=True)

def get_fuel(ollama_base: str, ollama_model: str,
             ollama_generate: Optional[Callable[[str], str]] = None
             ) -> Optional[Callable[[str], str]]:
    """Return a working generate function from the cheapest open station,
    or None when every station is closed / budget spent. Never raises."""
    chain = build_chain(ollama_base, ollama_model, ollama_generate)
    for st in chain:
        if st.paid and budget_exhausted():
            log.info("fuel: budget exhausted — skipping paid station %s", st.name)
            continue
        try:
            if not st.probe():
                log.info("fuel: station %s closed (probe failed)", st.name)
                continue
        except Exception as exc:
            log.info("fuel: station %s probe error: %s", st.name, exc)
            continue
        log.info("fuel: using station %s", st.name)
        if st.paid and st.estimate:
            def wrapped(prompt: str, _st=st) -> str:
                out = _st.generate(prompt)
                _budget_add(_st.estimate(prompt, out))
                return out
            return wrapped
        return st.generate
    log.warning("fuel: all stations closed — daemon sleeps until next tick")
    return None
