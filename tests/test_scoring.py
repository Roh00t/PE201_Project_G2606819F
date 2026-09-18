"""The scorer: pre-registered matching, metric arithmetic, CSV safety."""

import csv
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "evals"))

from scoring import csv_safe, score, value_matches, wilson, write_rows_csv  # noqa: E402


class Matching(unittest.TestCase):
    def test_dose_compares_number_unit_pairs(self):
        self.assertTrue(value_matches("dose", "500 mg", "500mg"))
        self.assertTrue(value_matches("dose", "0.5 mg", ".5 mg"))
        self.assertFalse(value_matches("dose", "500 mcg", "500 mg"))
        self.assertFalse(value_matches("dose", "50 mg", "500 mg"))

    def test_frequency_equivalence(self):
        self.assertTrue(value_matches("frequency", "twice daily", "BD"))
        self.assertTrue(value_matches("frequency", "1-0-1", "twice a day"))
        self.assertFalse(value_matches("frequency", "once daily", "twice daily"))

    def test_names_ignore_dosage_forms_and_punctuation(self):
        self.assertTrue(value_matches("medication", "Tab Dolo 650", "Dolo 650"))
        self.assertTrue(value_matches("medication", "Pan-D", "pan d"))
        self.assertFalse(value_matches("medication", "Metformin", "Glimepiride"))

    def test_empty_never_matches(self):
        self.assertFalse(value_matches("medication", None, "Metformin"))
        self.assertFalse(value_matches("medication", "Metformin", None))


class Wilson(unittest.TestCase):
    def test_known_interval(self):
        low, high = wilson(46, 54)
        self.assertTrue(0.72 < low < 0.74 and 0.91 < high < 0.93, (low, high))

    def test_empty(self):
        self.assertEqual(wilson(0, 0), (0.0, 1.0))


def payload(codes, values, status="ok"):
    if status != "ok":
        return {"status": "error", "code": "ERR_UPSTREAM_TIMEOUT"}
    fields = ("medication", "dose", "frequency", "allergy")
    return {
        "status": "ok",
        "gate": [{"field": f, "code": c} for f, c in zip(fields, codes)],
        "extraction": {f: {"value": v, "evidence": v, "status": "found"} for f, v in zip(fields, values)},
    }


GOLD = [{"case_id": "c1", "ground_truth": {
    "medication": {"status": "found", "value": "Metformin"},
    "dose": {"status": "found", "value": "500 mg"},
    "frequency": {"status": "found", "value": "twice daily"},
    "allergy": {"status": "not_stated", "value": None},
}}, {"case_id": "c2", "ground_truth": {
    "medication": {"status": "found", "value": "Amlodipine"},
    "dose": {"status": "found", "value": "5 mg"},
    "frequency": {"status": "found", "value": "once daily"},
    "allergy": {"status": "not_stated", "value": None},
}}]


class Scoring(unittest.TestCase):
    def test_arithmetic_on_a_hand_built_run(self):
        gated = {
            # c1: medication right, dose wiped (ungated value was wrong), frequency
            # REVIEW and right, allergy not stated.
            "c1": payload(["VERIFIED", "ABSTAIN_NUMERIC_MISMATCH", "REVIEW_MODEL_UNSURE", "NOT_STATED"],
                          ["Metformin", None, "BD", None]),
            # c2: medication VERIFIED but wrong (a silent failure), dose right,
            # frequency wiped although the ungated value was right (a gate false
            # positive), allergy not stated.
            "c2": payload(["VERIFIED", "VERIFIED", "ABSTAIN_UNGROUNDED", "NOT_STATED"],
                          ["Losartan", "5 mg", None, None]),
        }
        ungated = {
            "c1": payload(["VERIFIED"] * 3 + ["NOT_STATED"], ["Metformin", "50 mg", "BD", None]),
            "c2": payload(["VERIFIED"] * 3 + ["NOT_STATED"], ["Losartan", "5 mg", "once daily", None]),
        }
        result = score(GOLD, gated, ungated, slices={"attribution": {"c2"}})
        p = result["pooled"]
        self.assertEqual((p["gold_found"], p["proposed"], p["correct"]), (6, 4, 3))
        self.assertEqual(p["recall"], 0.5)
        self.assertEqual(p["precision"], 0.75)
        self.assertEqual((p["wiped"], p["wiped_would_be_wrong"]), (2, 1))
        self.assertEqual(p["abstention_precision"], 0.5)
        self.assertEqual(p["gate_false_positive_rate"], 0.5)
        self.assertEqual((p["verified"], p["silent"]), (3, 1))
        self.assertAlmostEqual(p["silent_failure_rate"], 0.3333, places=4)
        self.assertEqual(result["per_slice"]["attribution"]["silent"], 1)
        self.assertEqual(result["per_field"]["dose"]["wiped"], 1)

    def test_forced_abstentions_are_counted_apart_from_gate_decisions(self):
        forced = payload(["ABSTAIN_ENCODING_ANOMALY"] * 4, [None] * 4)
        result = score(GOLD[:1], {"c1": forced}, {"c1": forced})
        p = result["pooled"]
        self.assertEqual((p["forced"], p["wiped"], p["abstention_rate"]), (4, 0, None))
        self.assertEqual((p["gold_found"], p["correct"], p["recall"]), (3, 0, 0.0))

    def test_errored_cases_are_not_abstentions(self):
        gated = {"c1": payload(None, None, status="error"),
                 "c2": payload(["VERIFIED", "VERIFIED", "VERIFIED", "NOT_STATED"],
                               ["Amlodipine", "5 mg", "once daily", None])}
        ungated = {"c1": payload(None, None, status="error"), "c2": gated["c2"]}
        result = score(GOLD, gated, ungated)
        self.assertEqual(result["errored_cases"], ["c1"])
        self.assertEqual(result["pooled"]["wiped"], 0)
        self.assertEqual(result["coverage"], 0.5)
        self.assertEqual(result["pooled"]["recall"], 1.0)


class CsvSafety(unittest.TestCase):
    def test_formulas_are_neutralised(self):
        cells = ["=HYPERLINK(1)", "+1", "-2", "@SUM(A1)", "metformin", None]
        self.assertEqual([csv_safe(c) for c in cells],
                         ["'=HYPERLINK(1)", "'+1", "'-2", "'@SUM(A1)", "metformin", ""])

    def test_rows_file_is_neutralised(self):
        row = {"case_id": "c1", "field": "medication", "code": "VERIFIED", "gold_status": "found",
               "gold_value": "=cmd|' /C calc'!A0", "value": "Metformin", "ungated_value": "@x",
               "correct": True, "silent_failure": False}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rows.csv"
            write_rows_csv(path, [row])
            with open(path, newline="") as handle:
                written = list(csv.reader(handle))[1]
        self.assertTrue(written[4].startswith("'="))
        self.assertTrue(written[6].startswith("'@"))


if __name__ == "__main__":
    unittest.main()
