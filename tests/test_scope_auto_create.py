#!/usr/bin/env python3
"""Tests: Auto-Scope folder creation in consolidation (Startlücken-Fix,
Nebo GO 07.09.) + fuel budget default $5 + one-shot chat notice."""
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from nexus_memory import consolidation as C


class _FakeStore:
    def __init__(self):
        self.client = mock.MagicMock()


class ScopeCreateTest(unittest.TestCase):
    def _mk(self, llm_responses):
        """Consolidator with injected LLM; no Qdrant touched in these tests."""
        c = C.Consolidator(_FakeStore(), "nexus", llm_fn=lambda p: llm_responses.pop(0))
        return c

    def test_founds_scope_when_cluster_clear(self):
        c = self._mk(['{"scope": "voice-pipeline"}'])
        with mock.patch.object(c, "_daily_creations_left", return_value=2), \
             mock.patch.object(c, "_record_daily_creation") as rec, \
             mock.patch.object(C, "_SCOPE_NAME_RE", C._SCOPE_NAME_RE):
            name = c._maybe_found_scope(
                ["fact a about voice", "fact b about voice", "fact c about voice"],
                {}, {"default"})
            self.assertEqual(name, "voice-pipeline")
            rec.assert_called_once()

    def test_refuses_below_min_cluster(self):
        c = self._mk([])
        name = c._maybe_found_scope(["only one fact"], {}, {"default"})
        self.assertIsNone(name)

    def test_refuses_when_any_fact_has_scope(self):
        c = self._mk([])
        name = c._maybe_found_scope(["a", "b", "c"], {}, {"default", "voice"})
        self.assertIsNone(name)

    def test_refuses_invalid_name(self):
        c = self._mk(['{"scope": "Bad Name!!"}'])
        name = c._maybe_found_scope(["a", "b", "c"], {}, {"default"})
        self.assertIsNone(name)

    def test_refuses_default_name(self):
        c = self._mk(['{"scope": "default"}'])
        name = c._maybe_found_scope(["a", "b", "c"], {}, {"default"})
        self.assertIsNone(name)

    def test_llm_refusal_returns_none(self):
        c = self._mk(['{"scope": null}'])
        name = c._maybe_found_scope(["a", "b", "c"], {}, {"default"})
        self.assertIsNone(name)

    def test_llm_failure_fails_open(self):
        def boom(prompt):
            raise RuntimeError("ollama down")
        c = C.Consolidator(_FakeStore(), "nexus", llm_fn=boom)
        name = c._maybe_found_scope(["a", "b", "c"], {}, {"default"})
        self.assertIsNone(name)  # fail-open: facts stay default

    def test_disabled_via_env(self):
        with mock.patch.object(C, "_SCOPE_CREATE_ENABLED", False):
            c = self._mk(['{"scope": "x"}'])
            name = c._maybe_found_scope(["a", "b", "c"], {}, {"default"})
            self.assertIsNone(name)

    def test_daily_cap_zero_blocks(self):
        c = self._mk(['{"scope": "x"}'])
        with mock.patch.object(c, "_daily_creations_left", return_value=0):
            name = c._maybe_found_scope(["a", "b", "c"], {}, {"default"})
            self.assertIsNone(name)


class FuelBudgetTest(unittest.TestCase):
    def test_default_budget_is_five(self):
        self.assertEqual(C.os.environ.get("NEXUS_FUEL_BUDGET_USD", "") or
                         __import__("nexus_memory.fuel_chain", fromlist=["x"]).DEFAULT_FUEL_BUDGET_USD,
                         5.0)

    def test_notice_pending_latches_once(self):
        from nexus_memory import fuel_chain as F
        with mock.patch.object(F, "budget_exhausted", return_value=True), \
             mock.patch.object(F, "_load_state_unlocked", return_value={}), \
             mock.patch.object(F, "_persist_state_unlocked") as persist:
            self.assertTrue(F.budget_notification_pending())
            # second call: marker was persisted → not pending anymore
            with mock.patch.object(F, "_load_state_unlocked",
                                   return_value={"notified_month": F._current_month()}):
                self.assertFalse(F.budget_notification_pending())

    def test_not_pending_when_budget_ok(self):
        from nexus_memory import fuel_chain as F
        with mock.patch.object(F, "budget_exhausted", return_value=False):
            self.assertFalse(F.budget_notification_pending())

    def test_exhausted_info_shape(self):
        from nexus_memory import fuel_chain as F
        with mock.patch.object(F, "budget_exhausted", return_value=True), \
             mock.patch.object(F, "_load_state_unlocked",
                               return_value={"spent_usd": 5.0}):
            info = F.fuel_exhausted_info()
            self.assertTrue(info["exhausted"])
            self.assertEqual(info["budget_usd"], 5.0)
            self.assertEqual(info["spent_usd"], 5.0)
            self.assertIn("month", info)
            self.assertIn("reset", info)


if __name__ == "__main__":
    unittest.main()