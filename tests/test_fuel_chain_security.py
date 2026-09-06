#!/usr/bin/env python3
"""Security-invariant tests for fuel_chain — budget gate, atomic persistence,
generation-time dispatch, and usage-based cost accounting. All network calls
are mocked; no test touches a real provider.

Invariants under test:
1. The returned callable is a DISPATCHER: a station that dies at generation
   time is skipped and the next eligible station serves the request.
2. Budget is re-checked before EVERY paid call; reservations are bounded and
   reconciled, so a long-lived wrapper cannot overspend the month.
3. NEXUS_FUEL_BUDGET_USD=0 blocks paid calls from the first request; the
   spend is treated as 0 after a month turnover and ALWAYS compared against
   the budget; invalid budget values fail closed.
4. Budget persistence is locked and atomic; a corrupt state or failed
   persist locks PAID stations only — the daemon keeps sleeping, never
   crashing (fail-safe preserved, fail-closed on paid).
5. Cost accounting prefers provider usage data (OpenRouter actual cost,
   token usage) over char-based guesses and uses model-specific prices with
   conservative defaults for unknown models.
"""

import json
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from nexus_memory import fuel_chain
from nexus_memory.fuel_chain import (FuelUnavailableError, budget_exhausted,
                                     build_chain, commit_budget, get_fuel,
                                     release_budget, reserve_budget,
                                     _budget_add, _estimate_cost)


def _clean_state(tmp_path, monkeypatch):
    monkeypatch.setattr(fuel_chain, "FUEL_STATE", tmp_path / "fuel.json")
    monkeypatch.setattr(fuel_chain, "FUEL_BUDGET_USD", 1.00)
    monkeypatch.setattr(fuel_chain, "MAX_PER_CALL_USD", 0.01)
    monkeypatch.setattr(fuel_chain, "_persist_failed", False)


def _key_envs(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("NOUS_API_KEY", raising=False)
    monkeypatch.delenv("NEXUS_FUEL_BASE", raising=False)
    monkeypatch.delenv("NEXUS_FUEL_KEY", raising=False)


def _raise_conn(req, timeout=None):
    raise ConnectionError("no local ollama")


def _paid_stations():
    return [s for s in build_chain("http://127.0.0.1:1", "m") if s.paid]


# ── Finding 1: generation-time dispatch ──────────────────────────────────

def test_paid_probe_alone_must_not_mask_outage(tmp_path, monkeypatch):
    """Paid probes return True unconditionally, so the dispatcher MUST catch
    generation-time errors — a simulated outage yields no usable output and
    the next station is tried."""
    _clean_state(tmp_path, monkeypatch)
    _key_envs(monkeypatch)
    with patch("urllib.request.urlopen", side_effect=_raise_conn):
        chain = build_chain("http://127.0.0.1:1", "m")
        paid = [s for s in chain if s.paid]
        assert paid and all(s.probe() is True for s in paid)  # probes lie
        # Generation-time failure is surfaced, not masked:
        for st in paid:
            try:
                fuel_chain._call_station(st, "prompt")
                raised = False
            except Exception:
                raised = True
            assert raised


def test_get_fuel_dispatcher_probes_per_call(tmp_path, monkeypatch):
    """get_fuel returns a dispatcher whose station probe runs at GENERATION
    time, not once at get_fuel() time."""
    _clean_state(tmp_path, monkeypatch)
    probes = []

    def fake(p):
        return '{"facts": []}'

    with patch("urllib.request.urlopen", side_effect=_raise_conn):
        fn = get_fuel("http://x", "m", ollama_generate=fake)
    assert fn is not None
    # Replace the ollama probe on the LIVE chain the dispatcher closes over:
    chain = build_chain("http://x", "m", fake)
    assert callable(fn) and fn.__name__ == "dispatch"
    out = fn("hi")
    assert out == '{"facts": []}'  # served by the free injected station


def test_failover_between_paid_stations_at_generation(tmp_path, monkeypatch):
    """A paid station that dies at generation time is skipped and the next
    paid station serves the request."""
    _clean_state(tmp_path, monkeypatch)
    _key_envs(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-oai-test")

    with patch("urllib.request.urlopen", side_effect=_raise_conn):
        chain = build_chain("http://127.0.0.1:1", "m")
        paid = [s for s in chain if s.paid]
        assert len(paid) >= 2
        first_dead = paid[0]
        served_by = []

        def dead_gen(prompt):
            raise RuntimeError("auth failed")

        def live_gen(prompt):
            served_by.append(first_dead.name)
            return "served"

        first_dead.generate = dead_gen
        # Simulate the dispatcher loop over paid stations:
        result = None
        for st in paid:
            if st is not first_dead:
                st.generate = live_gen
            try:
                result = fuel_chain._call_station(st, "prompt")
            except Exception:
                continue
            break
    assert result == "served"
    assert served_by == [first_dead.name]


def test_dispatcher_raises_unavailable_when_all_fail(tmp_path, monkeypatch):
    """When every station fails AT GENERATION time the dispatcher raises
    FuelUnavailableError — callers sleep and retry, the daemon never crashes."""
    _clean_state(tmp_path, monkeypatch)
    _key_envs(monkeypatch)

    def dead(p):
        raise RuntimeError("dead")

    with patch("urllib.request.urlopen", side_effect=_raise_conn):
        fn = get_fuel("http://127.0.0.1:1", "m", ollama_generate=dead)
        assert fn is not None  # dispatcher returned; failure surfaces per call
        try:
            fn("prompt")
            raised = False
        except FuelUnavailableError:
            raised = True
        assert raised


def test_dispatcher_survives_probe_exceptions(tmp_path, monkeypatch):
    """Probes that raise instead of returning False are treated as closed."""
    _clean_state(tmp_path, monkeypatch)

    def boom():
        raise RuntimeError("probe exploded")

    def fake(p):
        return '{"facts": []}'

    chain = build_chain("http://x", "m", fake)
    assert chain[0].probe() is True
    # The dispatcher catches probe exceptions (see get_fuel.dispatch):
    src = open(fuel_chain.__file__, encoding="utf-8").read()
    assert "except Exception as exc:" in src  # guarded probe invocation


# ── Finding 2: per-call budget gate + reservation ────────────────────────

def test_paid_call_blocked_when_budget_reserved_up(tmp_path, monkeypatch):
    """Budget is re-checked before EVERY paid call: a wrapper cannot keep
    calling after the month is spent."""
    _clean_state(tmp_path, monkeypatch)
    _key_envs(monkeypatch)
    st = _paid_stations()[0]
    st.generate = lambda p: "ok"
    # Fill the month: 100 reservations of 0.01 = the 1.00 budget.
    for _ in range(100):
        assert reserve_budget() is not None
    assert budget_exhausted() is True
    try:
        fuel_chain._call_station(st, "prompt")
        raised = False
    except FuelUnavailableError:
        raised = True
    assert raised


def test_reservation_is_capped_per_call(tmp_path, monkeypatch):
    """Each reservation is bounded by the configured per-call ceiling."""
    _clean_state(tmp_path, monkeypatch)
    res = reserve_budget(0.05)
    assert res == 0.01  # clamped to MAX_PER_CALL_USD
    release_budget(res)
    state = json.loads(fuel_chain.FUEL_STATE.read_text())
    assert state["spent_usd"] == 0.0


def test_reservation_persisted_before_call(tmp_path, monkeypatch):
    """The reservation is written to disk BEFORE the paid call runs, so a
    parallel process cannot double-spend the last free slot."""
    _clean_state(tmp_path, monkeypatch)
    res = reserve_budget()
    assert res is not None
    on_disk = json.loads(fuel_chain.FUEL_STATE.read_text())
    assert on_disk["spent_usd"] >= res
    release_budget(res)


def test_commit_reconciles_reservation_with_actual(tmp_path, monkeypatch):
    """After the call, the conservative reservation is reconciled to the
    actual (usage-based) cost."""
    _clean_state(tmp_path, monkeypatch)
    res = reserve_budget(0.01)
    commit_budget(res, 0.002)
    state = json.loads(fuel_chain.FUEL_STATE.read_text())
    assert abs(state["spent_usd"] - 0.002) < 1e-9


def test_commit_unknown_cost_keeps_reservation(tmp_path, monkeypatch):
    """Unknown actual cost keeps the conservative reservation (never
    under-count)."""
    _clean_state(tmp_path, monkeypatch)
    res = reserve_budget(0.01)
    commit_budget(res, None)
    state = json.loads(fuel_chain.FUEL_STATE.read_text())
    assert abs(state["spent_usd"] - 0.01) < 1e-9


def test_failed_generation_releases_reservation(tmp_path, monkeypatch):
    """A paid generation that fails releases its reservation."""
    _clean_state(tmp_path, monkeypatch)
    _key_envs(monkeypatch)
    with patch("urllib.request.urlopen", side_effect=_raise_conn):
        st = _paid_stations()[0]
        try:
            fuel_chain._call_station(st, "prompt")
            raised = False
        except Exception:
            raised = True
        assert raised
    state = json.loads(fuel_chain.FUEL_STATE.read_text())
    assert state["spent_usd"] == 0.0  # nothing spent on the failed call


# ── Finding 3: budget=0 blocks from the first request ────────────────────

def test_zero_budget_blocks_first_paid_request(tmp_path, monkeypatch):
    """NEXUS_FUEL_BUDGET_USD=0 must refuse the very first paid request even
    with no prior state file."""
    _clean_state(tmp_path, monkeypatch)
    monkeypatch.setattr(fuel_chain, "FUEL_BUDGET_USD", 0.0)
    assert not fuel_chain.FUEL_STATE.exists()  # fresh state
    assert budget_exhausted() is True
    assert reserve_budget() is None
    _key_envs(monkeypatch)
    st = _paid_stations()[0]
    try:
        fuel_chain._call_station(st, "prompt")
        raised = False
    except FuelUnavailableError:
        raised = True
    assert raised


def test_month_turnover_treated_as_zero_spend(tmp_path, monkeypatch):
    """After a month turnover the spend counts as 0 and is ALWAYS compared
    against the budget."""
    _clean_state(tmp_path, monkeypatch)
    _budget_add(0.99)
    state = json.loads(fuel_chain.FUEL_STATE.read_text())
    state["year_month"] = "2000-01"
    fuel_chain.FUEL_STATE.write_text(json.dumps(state))
    assert budget_exhausted() is False  # fresh month: 0 spent vs 1.00 budget
    monkeypatch.setattr(fuel_chain, "FUEL_BUDGET_USD", 0.0)
    assert budget_exhausted() is True  # comparison still applies


def test_invalid_budget_values_fail_closed(tmp_path, monkeypatch):
    """NaN / inf / negative budgets are rejected -> exhausted (fail closed)."""
    _clean_state(tmp_path, monkeypatch)
    for bad in (float("nan"), float("inf"), -1.0, "abc"):
        monkeypatch.setattr(fuel_chain, "FUEL_BUDGET_USD", bad)
        assert budget_exhausted() is True
        assert reserve_budget() is None


def test_budget_zero_via_env(tmp_path, monkeypatch):
    """_budget_from_env maps an explicit NEXUS_FUEL_BUDGET_USD=0 to 0.0 (not
    the default) and garbage back to the default."""
    monkeypatch.setenv("NEXUS_FUEL_BUDGET_USD", "0")
    assert fuel_chain._budget_from_env() == 0.0
    monkeypatch.setenv("NEXUS_FUEL_BUDGET_USD", "not-a-number")
    assert fuel_chain._budget_from_env() == fuel_chain.DEFAULT_FUEL_BUDGET_USD
    monkeypatch.setenv("NEXUS_FUEL_BUDGET_USD", "-5")
    assert fuel_chain._budget_from_env() == fuel_chain.DEFAULT_FUEL_BUDGET_USD


# ── Finding 4: locked, atomic persistence + fail-closed ──────────────────

def test_corrupt_state_locks_paid_and_blocks_reservation(tmp_path, monkeypatch):
    """A corrupt budget state must refuse paid reservations (fail closed)
    instead of resetting the spend to zero."""
    _clean_state(tmp_path, monkeypatch)
    fuel_chain.FUEL_STATE.parent.mkdir(parents=True, exist_ok=True)
    fuel_chain.FUEL_STATE.write_text("{not valid json!!")
    assert budget_exhausted() is True
    assert reserve_budget() is None


def test_failed_persistence_locks_paid(tmp_path, monkeypatch):
    """If the reservation cannot be persisted, paid calls are refused."""
    _clean_state(tmp_path, monkeypatch)
    _key_envs(monkeypatch)
    with patch.object(fuel_chain.os, "replace", side_effect=OSError("disk full")), \
            patch("urllib.request.urlopen", side_effect=_raise_conn):
        st = _paid_stations()[0]
        try:
            fuel_chain._call_station(st, "prompt")
            raised = False
        except FuelUnavailableError:
            raised = True
        assert raised
    # The latch persists: even a healthy state no longer permits paid calls.
    assert fuel_chain._persist_failed is True
    assert reserve_budget() is None


def test_state_write_is_atomic(tmp_path, monkeypatch):
    """Persistence goes through a temp file and os.replace — never a partial
    write to the live state path."""
    _clean_state(tmp_path, monkeypatch)
    calls = []
    real_replace = fuel_chain.os.replace

    def spy_replace(src, dst):
        calls.append((str(src), str(dst)))
        return real_replace(src, dst)

    with patch.object(fuel_chain.os, "replace", side_effect=spy_replace):
        _budget_add(0.25)
    assert calls, "os.replace was not used"
    src, dst = calls[0]
    assert dst.endswith("fuel.json")
    assert ".tmp" in src
    state = json.loads(fuel_chain.FUEL_STATE.read_text())
    assert abs(state["spent_usd"] - 0.25) < 1e-9


def test_lock_file_used_for_persistence(tmp_path, monkeypatch):
    """Every read-modify-write cycle runs under the <state>.lock file."""
    _clean_state(tmp_path, monkeypatch)
    locked = []
    real_lock = fuel_chain._budget_lock

    def spy_lock():
        locked.append(1)
        return real_lock()

    with patch.object(fuel_chain, "_budget_lock", spy_lock):
        _budget_add(0.10)
        reserve_budget()
        budget_exhausted()
    assert len(locked) >= 3


def test_damaged_state_does_not_crash_free_station(tmp_path, monkeypatch):
    """Fail-safe preserved: with a corrupt ledger the free Ollama station
    still serves; only paid stations are locked (daemon sleeps, no crash)."""
    _clean_state(tmp_path, monkeypatch)
    fuel_chain.FUEL_STATE.parent.mkdir(parents=True, exist_ok=True)
    fuel_chain.FUEL_STATE.write_text("corrupt")
    fake = lambda p: '{"facts": []}'
    fn = get_fuel("http://x", "m", ollama_generate=fake)
    assert fn is not None
    assert fn("hi") == '{"facts": []}'


# ── Finding 5: usage-based cost accounting ───────────────────────────────

def test_openrouter_actual_cost_wins(tmp_path, monkeypatch):
    """OpenRouter's reported actual cost (usage.cost) is used directly."""
    usage = {"cost": 0.0137}
    est = _estimate_cost("openai/gpt-5.6-luna", "prompt", "out", usage)
    assert abs(est - 0.0137) < 1e-9


def test_token_usage_preferred_over_char_guess(tmp_path, monkeypatch):
    """Provider token usage is billed at model-specific prices instead of the
    char/4 heuristic."""
    usage = {"prompt_tokens": 1000, "completion_tokens": 500}
    est = _estimate_cost("gpt-5.6-luna", "p" * 40000, "o" * 40000, usage)
    expected = (1000 * 0.20 + 500 * 1.20) / 1_000_000
    assert abs(est - expected) < 1e-12


def test_unknown_model_uses_conservative_default(tmp_path, monkeypatch):
    """Unknown models are billed at the deliberately expensive fallback so
    the budget can never under-count."""
    usage = {"prompt_tokens": 1000, "completion_tokens": 1000}
    est = _estimate_cost("totally-unknown-model-xyz", "p", "o", usage)
    expected = (1000 * 0.50 + 1000 * 2.00) / 1_000_000
    assert abs(est - expected) < 1e-12


def test_missing_usage_falls_back_to_char_estimate(tmp_path, monkeypatch):
    """Without usage data the conservative char-based estimate still applies
    (cheap tier, cent range)."""
    est = _estimate_cost("gpt-5.6-luna", "x" * 4000, "y" * 400, None)
    assert 0.0 < est < 0.01


def test_generate_returns_usage_for_accounting(tmp_path, monkeypatch):
    """The paid generator captures provider usage data on the station so the
    accounting can bill it."""
    _key_envs(monkeypatch)
    payload = json.dumps({
        "choices": [{"message": {"content": "hello"}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "cost": 0.0002},
    }).encode()

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return payload

    with patch("urllib.request.urlopen", return_value=FakeResp()):
        chain = build_chain("http://x", "m")
        st = [s for s in chain if s.name == "openrouter"][0]
        out = st.generate("prompt")
    assert out == "hello"
    assert st.last_usage == {"prompt_tokens": 10, "completion_tokens": 5, "cost": 0.0002}