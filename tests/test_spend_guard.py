"""Per-call cost accounting and the per-run cap. No network: the records are
built by hand from the shape extract.py actually produces."""

import json
import sys
import tempfile
import unittest
from io import StringIO
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "evals"))

import spend_guard  # noqa: E402
from extract import BudgetExceeded, OPENROUTER_BASE_URL, SpendLedger  # noqa: E402

USAGE = {
    "called": True, "input_tokens": 777, "output_tokens": 145, "reasoning_tokens": 0,
    "cached_tokens": 128, "est_cost_usd": 0.000596, "provider_cost_usd": 0.000594,
    "billed_usd": 0.000594, "attempts": 1,
}
PROVENANCE = {
    "called": True, "requested_model": "google/gemini-2.5-flash",
    "served_model": "google/gemini-2.5-flash", "provider": "Google",
    "response_id": "gen-123", "sdk": "openai 3.15.0",
}


def record(case_id="case_001", **overrides):
    usage = {**USAGE, **overrides.pop("usage", {})}
    provenance = {**PROVENANCE, **overrides.pop("provenance", {})}
    return spend_guard.CallRecord.from_result(
        "run-1", case_id, "google/gemini-2.5-flash", usage, provenance,
        overrides.pop("api_ms", 1216.0), **overrides)


class Mapping(unittest.TestCase):
    def test_it_carries_the_whole_bill_and_the_destination(self):
        r = record()
        self.assertEqual((r.input_tokens, r.output_tokens, r.cached_tokens), (777, 145, 128))
        self.assertEqual((r.est_cost_usd, r.provider_cost_usd, r.billed_usd),
                         (0.000596, 0.000594, 0.000594))
        self.assertEqual((r.provider, r.model_served, r.response_id),
                         ("Google", "google/gemini-2.5-flash", "gen-123"))
        self.assertEqual(r.endpoint, OPENROUTER_BASE_URL)
        self.assertEqual((r.latency_ms, r.attempts, r.ok), (1216.0, 1, True))

    def test_a_provider_that_reports_no_cost_is_not_recorded_as_free(self):
        r = record(usage={"provider_cost_usd": None})
        self.assertIsNone(r.provider_cost_usd)
        self.assertEqual(r.billed_usd, 0.000594)

    def test_missing_provider_fields_do_not_crash_the_audit(self):
        r = spend_guard.CallRecord.from_result("run-1", "c", "m", {}, {}, 0.0)
        self.assertEqual((r.input_tokens, r.billed_usd, r.model_served), (0, 0.0, None))
        self.assertEqual(r.model_requested, "m")


class RunCap(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.log_dir = Path(self.tmp.name)

    def guard(self, cap=0.001, ledger=None, ceiling=None):
        return spend_guard.CostGuard(run_id="run-1", cap_usd=cap, ledger=ledger,
                                 ceiling_usd=ceiling, log_dir=self.log_dir, verbose=False)

    def test_a_call_that_would_cross_the_run_cap_is_refused_before_it_is_made(self):
        guard = self.guard(cap=0.001)
        guard.check_before(0.0005)                      # room for this one
        guard.record(record())
        with self.assertRaises(spend_guard.RunCapExceeded) as caught:
            guard.check_before(0.0009)
        self.assertIn("0.00", str(caught.exception))
        self.assertIn("Raise the cap", str(caught.exception))

    def test_the_run_cap_refusal_is_a_budget_refusal(self):
        # Callers that already handle a spend refusal must keep working, and the
        # exit-code mapping must not fork.
        self.assertTrue(issubclass(spend_guard.RunCapExceeded, BudgetExceeded))

    def test_the_global_ceiling_is_still_the_ledger_s_to_enforce(self):
        ledger_path = self.log_dir / "ledger.json"
        ledger_path.write_text(json.dumps({"spent_usd": 7.999, "calls": 1, "by_model": {}}))
        guard = self.guard(cap=1.00, ledger=SpendLedger(ledger_path), ceiling=8.00)
        with self.assertRaises(BudgetExceeded):
            guard.check_before(0.01)                    # under the run cap, over the ceiling

    def test_without_a_ledger_only_the_run_cap_applies(self):
        self.guard(cap=1.00).check_before(0.5)          # does not raise

    def test_spending_accumulates_across_calls(self):
        guard = self.guard(cap=1.00)
        self.assertEqual(guard.record(record()), 0.000594)
        self.assertEqual(guard.record(record("case_002")), 0.001188)


class LedgerOwnership(unittest.TestCase):
    """Exactly one writer per call, or the $8 ceiling lies in one direction or
    the other. This caught a real hole: the v4 probe calls the endpoint directly,
    so its spend never reached the ledger until records_to_ledger existed."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "ledger.json"
        self.ledger = SpendLedger(self.path)

    def guard(self, records_to_ledger):
        return spend_guard.CostGuard(run_id="run-1", cap_usd=1.00, ledger=self.ledger,
                                 ceiling_usd=8.00, log_dir=Path(self.tmp.name),
                                 verbose=False, records_to_ledger=records_to_ledger)

    def test_by_default_the_guard_does_not_touch_the_ledger(self):
        # extract.call_model already recorded it; writing again would double count.
        self.guard(False).record(record())
        self.assertEqual(self.ledger.spent(), 0.0)

    def test_a_direct_caller_can_make_the_guard_the_writer(self):
        guard = self.guard(True)
        guard.record(record())
        guard.record(record("case_002"))
        self.assertAlmostEqual(self.ledger.spent(), 2 * 0.000594)
        self.assertEqual(json.loads(self.path.read_text())["calls"], 2)

    def test_a_free_or_refused_call_adds_nothing_to_the_ledger(self):
        guard = self.guard(True)
        guard.record(record(usage={"billed_usd": 0.0}, ok=False, code="400"))
        self.assertEqual(self.ledger.spent(), 0.0)
        self.assertEqual(guard.summary()["failed_calls"], 1)

    def test_the_ledger_is_credited_under_the_model_that_was_asked_for(self):
        self.guard(True).record(record(provenance={"served_model": "other/model"}))
        by_model = json.loads(self.path.read_text())["by_model"]
        self.assertEqual(list(by_model), ["google/gemini-2.5-flash"])


class Money(unittest.TestCase):
    def test_a_cap_of_two_tenths_of_a_cent_is_not_printed_as_zero(self):
        self.assertEqual(spend_guard.money(0.002), "$0.002000")
        self.assertEqual(spend_guard.money(0.0), "$0.00")
        self.assertEqual(spend_guard.money(1.0), "$1.00")
        self.assertEqual(spend_guard.money(8.0), "$8.00")


class AuditLog(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.guard = spend_guard.CostGuard(run_id="run-1", cap_usd=1.00,
                                       log_dir=Path(self.tmp.name), verbose=False)

    def test_one_json_line_per_call(self):
        self.guard.record(record("case_001"))
        self.guard.record(record("case_002"))
        lines = self.guard.log_path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual([json.loads(line)["case_id"] for line in lines],
                         ["case_001", "case_002"])
        self.assertEqual(json.loads(lines[0])["cached_tokens"], 128)

    def test_the_live_line_shows_the_running_total_against_the_cap(self):
        line = self.guard.live_line(record())
        self.guard.record(record())
        self.assertIn("case_001", line)
        self.assertIn("777+145tok", line)
        self.assertIn("$1.00", line)

    def test_a_served_model_that_differs_is_called_out_on_the_line(self):
        line = self.guard.live_line(record(provenance={"served_model": "google/gemini-flash-lite"}))
        self.assertIn("served:google/gemini-flash-lite", line)


class Summary(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.guard = spend_guard.CostGuard(run_id="run-1", cap_usd=1.00,
                                       log_dir=Path(self.tmp.name), verbose=False)
        for i in range(3):
            self.guard.record(record(f"case_{i:03d}", api_ms=1000.0 + i * 100))

    def test_tokens_dollars_and_latency_are_totalled(self):
        s = self.guard.summary()
        self.assertEqual(s["calls"], 3)
        self.assertEqual(s["tokens"], {"input": 2331, "output": 435, "cached": 384,
                                       "reasoning": 0})
        self.assertEqual(s["billed_usd"], 0.001782)
        self.assertEqual(s["latency_ms"]["p50"], 1100.0)
        self.assertEqual(s["latency_ms"]["max"], 1200.0)

    def test_the_price_table_is_checked_against_the_provider_s_own_charge(self):
        check = self.guard.summary()["price_check"]
        self.assertEqual(check["calls_with_provider_cost"], 3)
        self.assertEqual(check["estimate_total_usd"], 0.001788)
        self.assertEqual(check["provider_total_usd"], 0.001782)
        self.assertEqual(check["drift_usd"], -0.000006)

    def test_the_destination_is_recorded_for_the_audit(self):
        destinations = self.guard.summary()["destinations"]
        self.assertEqual(len(destinations), 1)
        self.assertEqual(destinations[0]["endpoint"], OPENROUTER_BASE_URL)
        self.assertEqual(destinations[0]["provider"], "Google")

    def test_model_drift_is_surfaced_rather_than_averaged_away(self):
        self.guard.record(record("case_009", provenance={"served_model": "other/model"}))
        drift = self.guard.summary()["model_drift"]
        self.assertEqual(drift, [{"requested": "google/gemini-2.5-flash",
                                  "served": "other/model"}])

    def test_the_summary_is_written_as_json_and_names_the_per_call_log(self):
        path = Path(self.tmp.name) / "metrics.json"
        self.guard.write_summary(path)
        written = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(written["calls"], 3)
        self.assertIn("run-1.jsonl", written["per_call_log"])

    def test_an_empty_run_summarises_without_dividing_by_zero(self):
        empty = spend_guard.CostGuard(run_id="r", cap_usd=1.0, log_dir=Path(self.tmp.name),
                                  verbose=False)
        s = empty.summary()
        self.assertEqual((s["calls"], s["billed_usd"], s["cost_per_call_usd"]), (0, 0.0, 0.0))

    def test_the_printed_summary_names_the_cap_and_the_destination(self):
        out = StringIO()
        self.guard.print_summary(out)
        text = out.getvalue()
        self.assertIn("$1.00 run cap", text)
        self.assertIn("routed to", text)
        self.assertIn("price check", text)


if __name__ == "__main__":
    unittest.main()
