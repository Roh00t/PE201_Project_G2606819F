"""Measurement over finished runs: populations, layer ownership, and whether a
gap between two runs is real. Pure functions, so these tests need no network, no
key and no fixtures beyond a few hand-built payloads."""

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "evals"))

import metrics  # noqa: E402
import scoring  # noqa: E402
from extract import CRITICAL_FIELDS  # noqa: E402

REAL_RUNS = sorted(p for p in (ROOT / "evals" / "results").glob("*")
                   if (p / "scores.json").is_file() and (p / "gated.jsonl").is_file())
GOLD = ROOT / "data" / "gold_labels" / "gold_v1.json"


def truth(value=None):
    if value is None:
        return {"value": None, "evidence": None, "status": "not_stated"}
    return {"value": value, "evidence": value, "status": "found"}


def case(case_id, text="Tab Dolo 650 twice a day", **labels):
    ground = {f: labels.get(f) or truth() for f in CRITICAL_FIELDS}
    return {"case_id": case_id, "source_text": text, "ground_truth": ground,
            "labeller": "t", "labelled_at": "2026-09-20T00:00:00+00:00", "notes": None}


def payload(case_id, verified=None, code_override=None, status="ok"):
    verified = verified or {}
    extraction, gate = {}, []
    for name in CRITICAL_FIELDS:
        value = verified.get(name)
        extraction[name] = ({"evidence": value, "value": value, "status": "found"} if value
                            else {"evidence": None, "value": None, "status": "not_stated"})
        code = code_override or ("VERIFIED" if value else "NOT_STATED")
        gate.append({"field": name, "outcome": "pass" if value else "skipped",
                     "code": code, "near_miss": False})
    return {"case_id": case_id, "status": status, "provenance": {"called": True},
            "latency_ms": {"api": 1200.0, "local": 0.5, "total": 1200.5},
            "latency_budget_ms": 3000, "within_budget": True,
            "input_scan": {"flags": [], "review_required": False},
            "usage": {"called": True, "billed_usd": 0.0006, "input_tokens": 700,
                      "output_tokens": 140, "cached_tokens": 0, "attempts": 1,
                      "est_cost_usd": 0.0006, "provider_cost_usd": 0.0006},
            "extraction": extraction, "gate": gate}


class Fractions(unittest.TestCase):
    def test_a_rate_with_no_denominator_is_not_invented(self):
        empty = metrics.frac(0, 0)
        self.assertIsNone(empty["rate"])
        self.assertEqual(empty["text"], "0/0 (n/a)")

    def test_a_real_fraction_carries_its_counts(self):
        f = metrics.frac(39, 56)
        self.assertEqual((f["n"], f["d"]), (39, 56))
        self.assertAlmostEqual(f["rate"], 0.6964, places=4)
        self.assertEqual(f["text"], "39/56 (69.6%)")

    def test_an_empty_percentile_is_none_not_zero(self):
        self.assertIsNone(metrics.percentile([], 90))

    def test_the_percentile_convention_is_nearest_rank_round_half_up(self):
        # Pinned because three conventions are in common use and they disagree
        # at n = 10: this one puts the 90th percentile at 10, not 9.
        values = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
        self.assertEqual(metrics.percentile(values, 90), 10)
        self.assertEqual(metrics.percentile(values, 100), 10)
        self.assertEqual(metrics.percentile([7], 90), 7)
        # And the reason run_shape reports `median` rather than `p50`: under
        # this convention the 50th percentile of an even sample is the upper
        # middle, 6, while statistics.median is 5.5. Two different numbers
        # with one name is how a latency claim goes wrong.
        self.assertEqual(metrics.percentile(values, 50), 6)

    def test_enforcement_and_measurement_agree_on_the_percentile(self):
        import spend_guard
        guard = spend_guard.CostGuard(run_id="p", cap_usd=1.0, verbose=False)
        latencies = [100.0, 200.0, 300.0, 400.0, 500.0, 600.0, 700.0, 800.0, 900.0, 1000.0]
        for i, ms in enumerate(latencies):
            guard.records.append(spend_guard.CallRecord.from_result(
                "p", f"c{i}", "m", {"billed_usd": 0.0}, {}, ms))
        self.assertEqual(guard.summary()["latency_ms"]["p95"],
                         metrics.percentile(latencies, 95))


class Populations(unittest.TestCase):
    def test_the_three_populations_sum_to_the_cases_present(self):
        cases = [case("case_001", medication=truth("Dolo")), case("case_002"),
                 case("case_003")]
        gated = {"case_001": payload("case_001", {"medication": "Dolo"}),
                 "case_002": payload("case_002", status="error"),
                 "case_003": payload("case_003", {"medication": "x"},
                                     code_override="ABSTAIN_ENCODING_ANOMALY")}
        pops = metrics.split(cases, gated, gated)
        self.assertEqual(pops["scored"], ["case_001"])
        self.assertEqual(pops["errored"], ["case_002"])
        self.assertEqual(pops["refused"], ["case_003"])
        self.assertEqual(sum(len(pops[k]) for k in ("scored", "errored", "refused")),
                         pops["total"])

    def test_a_note_our_own_code_refused_is_not_a_model_abstention(self):
        # Every field wiped by the encoding gate means no call was ever made.
        cases = [case("case_001", medication=truth("Dolo"))]
        gated = {"case_001": payload("case_001", {"medication": "Dolo"},
                                     code_override="ABSTAIN_ENCODING_ANOMALY")}
        self.assertEqual(metrics.split(cases, gated, gated)["scored"], [])


class OneImplementationOfEachMetric(unittest.TestCase):
    """The point of the whole module: it may add numbers, never contradict them."""

    def test_per_field_precision_and_recall_match_the_pre_registered_scorer(self):
        self.assertTrue(REAL_RUNS, "no finished run on disk to check against")
        for run_dir in REAL_RUNS:
            with self.subTest(run=run_dir.name):
                run = metrics.load_run(run_dir)
                # Each run is scored against its OWN gold version. Comparing an
                # allergy run against gold-v1 shares no case ids, so the check
                # would pass on emptiness rather than on agreement.
                recorded = (run["manifest"].get("gold") or {}).get("path")
                gold_path = (ROOT / recorded) if recorded else GOLD
                if not gold_path.is_file():
                    self.skipTest(f"{recorded} is not on disk")
                gold_cases = json.loads(gold_path.read_text(encoding="utf-8"))["cases"]
                stored = json.loads((run_dir / "scores.json").read_text())["per_field"]
                gated = run["gated"]
                mine = metrics.field_quality(gold_cases, gated)["per_field"]
                for field in CRITICAL_FIELDS:
                    self.assertEqual(mine[field]["recall"]["rate"] is None,
                                     stored[field]["recall"] is None, field)
                    if stored[field]["recall"] is not None:
                        self.assertAlmostEqual(mine[field]["recall"]["rate"],
                                               stored[field]["recall"], places=4, msg=field)
                        self.assertAlmostEqual(mine[field]["precision"]["rate"],
                                               stored[field]["precision"], places=4, msg=field)

    def test_the_confidence_interval_is_the_scorer_s_own(self):
        self.assertEqual(metrics.wilson_interval(93, 155), scoring.wilson(93, 155))
        self.assertEqual(metrics.wilson_interval(0, 0), (None, None))


class Mcnemar(unittest.TestCase):
    """Paired, exact, and computed by hand in the comments so the arithmetic is
    checkable without trusting the implementation."""

    def test_no_change_means_nothing_to_separate(self):
        self.assertEqual(metrics.mcnemar(0, 0)["p"], 1.0)

    def test_one_single_flip_proves_nothing(self):
        # min(0,1)=0, n=1: 2 * C(1,0)/2^1 = 1.0
        self.assertEqual(metrics.mcnemar(0, 1)["p"], 1.0)

    def test_the_observed_medication_flips_are_suggestive_not_separated(self):
        # 9 wrong->right, 2 right->wrong. min=2, n=11:
        # 2 * (C(11,0)+C(11,1)+C(11,2)) / 2^11 = 2 * 67/2048 = 0.0654
        result = metrics.mcnemar(9, 2)
        self.assertAlmostEqual(result["p"], 0.0654, places=4)
        self.assertEqual(result["net"], 7)
        self.assertGreater(result["p"], 0.05)

    def test_the_observed_pooled_flips_are_not_separated(self):
        # 12 wrong->right, 5 right->wrong. min=5, n=17: 2 * 9402/131072 = 0.1435
        self.assertAlmostEqual(metrics.mcnemar(12, 5)["p"], 0.1435, places=4)

    def test_a_lopsided_result_does_separate(self):
        # 12 to right, 0 to wrong: 2 * 1/4096 = 0.00049
        self.assertLess(metrics.mcnemar(12, 0)["p"], 0.05)

    def test_only_discordant_fields_count(self):
        self.assertEqual(metrics.mcnemar(3, 2)["discordant"], 5)


class TwoProportion(unittest.TestCase):
    def test_identical_rates_cannot_be_separated(self):
        _, p = metrics.two_proportion_p(30, 60, 30, 60)
        self.assertEqual(p, 1.0)

    def test_an_empty_arm_is_refused_rather_than_divided_by(self):
        self.assertEqual(metrics.two_proportion_p(1, 0, 1, 10), (0.0, 1.0))

    def test_a_large_clear_gap_separates(self):
        _, p = metrics.two_proportion_p(90, 100, 40, 100)
        self.assertLess(p, 0.001)


class Comparison(unittest.TestCase):
    def setUp(self):
        self.cases = [case("case_001", medication=truth("Dolo")),
                      case("case_002", medication=truth("Omez")),
                      case("case_003", medication=truth("Pan D"))]

    def test_a_run_against_itself_shows_no_difference(self):
        gated = {c["case_id"]: payload(c["case_id"], {"medication": "Dolo"})
                 for c in self.cases}
        result = metrics.compare(self.cases, gated, gated, "a", "b")
        pooled = result["scopes"]["pooled"]
        self.assertEqual(pooled["delta_pp"], 0.0)
        self.assertEqual(pooled["mcnemar"]["discordant"], 0)
        self.assertFalse(pooled["separated"])

    def test_only_gold_found_fields_are_paired(self):
        gated = {c["case_id"]: payload(c["case_id"], {"medication": "Dolo"})
                 for c in self.cases}
        # Three cases, one gold-found field each: three paired units, not twelve.
        self.assertEqual(metrics.compare(self.cases, gated, gated)["paired_fields"], 3)

    def test_an_improvement_is_reported_with_its_flip_counts(self):
        before = {c["case_id"]: payload(c["case_id"]) for c in self.cases}
        after = {"case_001": payload("case_001", {"medication": "Dolo"}),
                 "case_002": payload("case_002", {"medication": "Omez"}),
                 "case_003": payload("case_003")}
        pooled = metrics.compare(self.cases, before, after, "old", "new")["scopes"]["pooled"]
        self.assertEqual(pooled["mcnemar"]["flipped_to_right"], 2)
        self.assertEqual(pooled["mcnemar"]["flipped_to_wrong"], 0)
        self.assertEqual(pooled["a"]["text"], "0/3 (0.0%)")
        self.assertEqual(pooled["b"]["text"], "2/3 (66.7%)")

    def test_two_runs_of_the_same_prompt_do_not_collapse_into_one_column(self):
        # Keying the columns by label lost one of them whenever both runs shared
        # a prompt fingerprint, which is exactly the leakage comparison's shape.
        before = {c["case_id"]: payload(c["case_id"]) for c in self.cases}
        after = {c["case_id"]: payload(c["case_id"], {"medication": "Dolo"})
                 for c in self.cases}
        pooled = metrics.compare(self.cases, before, after, "same", "same")["scopes"]["pooled"]
        self.assertEqual(pooled["a"]["n"], 0)
        self.assertEqual(pooled["b"]["n"], 1)
        self.assertEqual(pooled["delta_pp"], 33.3)


class Ownership(unittest.TestCase):
    def test_every_cause_the_diagnosis_can_produce_has_an_owning_layer(self):
        import diagnose
        for cause in diagnose.CAUSES:
            self.assertIn(cause, metrics.LAYER, cause)

    def test_the_taxonomy_groups_the_real_run_by_layer(self):
        self.assertTrue(REAL_RUNS)
        gold_cases = json.loads(GOLD.read_text(encoding="utf-8"))["cases"]
        run = metrics.load_run(REAL_RUNS[0])
        tax = metrics.failure_taxonomy(gold_cases, run["gated"],
                                       run["ungated"] or run["gated"])
        self.assertEqual(sum(b["count"] for b in tax["buckets"]), tax["failed_fields"])
        self.assertEqual(sum(tax["by_layer"].values()), tax["failed_fields"])
        self.assertNotIn("unclassified", tax["by_layer"])


class CostShape(unittest.TestCase):
    def test_cost_per_correct_field_is_none_when_nothing_was_correct(self):
        cases = [case("case_001", medication=truth("Dolo"))]
        gated = {"case_001": payload("case_001")}
        shape = metrics.run_shape(cases, gated, 0)
        self.assertIsNone(shape["cost_per_correct_field"])
        self.assertIsNotNone(shape["cost_per_call"])

    def test_a_correct_field_costs_more_than_a_call_when_the_system_is_wrong(self):
        cases = [case(f"case_{i:03d}", medication=truth("Dolo")) for i in range(1, 5)]
        gated = {c["case_id"]: payload(c["case_id"], {"medication": "Dolo"})
                 for c in cases}
        shape = metrics.run_shape(cases, gated, 2)       # 4 calls, 2 correct
        self.assertAlmostEqual(shape["cost_per_correct_field"],
                               2 * shape["cost_per_call"], places=6)

    def test_the_budget_count_uses_api_plus_local_time(self):
        cases = [case("case_001", medication=truth("Dolo"))]
        slow = payload("case_001", {"medication": "Dolo"})
        slow["latency_ms"] = {"api": 11993.3, "local": 0.5, "total": 11993.8}
        shape = metrics.run_shape(cases, {"case_001": slow}, 1)
        self.assertEqual(shape["inside_budget"]["n"], 0)


class Provenance(unittest.TestCase):
    def test_a_call_with_no_provider_figure_makes_the_total_unquotable(self):
        good = payload("case_001", {"medication": "Dolo"})
        bad = payload("case_002", {"medication": "Dolo"})
        bad["usage"]["provider_cost_usd"] = None
        prov = metrics.cost_provenance({"case_001": good, "case_002": bad})
        self.assertFalse(prov["all_measured"])
        self.assertEqual(prov["calls_priced_by_provider"]["text"], "1/2 (50.0%)")


class CommandLine(unittest.TestCase):
    def test_it_describes_one_run_and_prints_json_on_stdout(self):
        from contextlib import redirect_stderr, redirect_stdout
        from io import StringIO
        self.assertTrue(REAL_RUNS)
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = metrics.main([str(REAL_RUNS[0]), "--gold", str(GOLD)])
        self.assertEqual(code, 0)
        report = json.loads(out.getvalue())
        self.assertEqual(len(report["runs"]), 1)
        self.assertIn("WHAT HAPPENED TO", err.getvalue())
        self.assertNotIn("comparison", report)

    def test_two_runs_are_compared_and_the_verdict_is_stated(self):
        from contextlib import redirect_stderr, redirect_stdout
        from io import StringIO
        if len(REAL_RUNS) < 2:
            self.skipTest("needs two finished runs")
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = metrics.main([str(REAL_RUNS[0]), str(REAL_RUNS[1]), "--gold", str(GOLD)])
        self.assertEqual(code, 0)
        self.assertIn("comparison", json.loads(out.getvalue()))
        self.assertIn("IS THE DIFFERENCE REAL?", err.getvalue())

    def test_a_directory_with_no_payloads_exits_2(self):
        from contextlib import redirect_stderr, redirect_stdout
        from io import StringIO
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = metrics.main([str(ROOT / "docs"), "--gold", str(GOLD)])
        self.assertEqual(code, 2)
        self.assertEqual(out.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
