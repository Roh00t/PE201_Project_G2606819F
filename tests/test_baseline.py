"""The non-AI baseline and the generic arm scorer. No model, no network, no
spend: that is the whole point of the baseline, so these tests need nothing."""

import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "evals"))

import baseline  # noqa: E402
import diagnose  # noqa: E402
import score_arm  # noqa: E402
from extract import CRITICAL_FIELDS, Status, verify_evidence  # noqa: E402

FIELDS = CRITICAL_FIELDS


def gold_case(case_id, text, **labels):
    ground = {f: labels.get(f) or {"value": None, "evidence": None, "status": "not_stated"}
              for f in FIELDS}
    return {"case_id": case_id, "source_text": text, "ground_truth": ground,
            "labeller": "test", "labelled_at": "2026-09-20T00:00:00+00:00", "notes": None}


def truth(value):
    return {"value": value, "evidence": value, "status": "found"}


class Medication(unittest.TestCase):
    def pick(self, note, gazetteer=frozenset()):
        field, _ = baseline.find_medication(note, gazetteer)
        return field.value, field.status

    def test_the_first_form_mention_wins(self):
        self.assertEqual(self.pick("Tablet Dolo twice a day. Tablet Pan D once."),
                         ("Dolo", Status.FOUND))

    def test_a_trailing_strength_is_split_off_the_name(self):
        # FIELD_RULES puts the strength in `dose`, so the name must not carry it.
        self.assertEqual(self.pick("Tab Dolo 650 twice a day")[0], "Dolo")

    def test_no_form_word_and_no_gazetteer_means_not_stated(self):
        self.assertEqual(self.pick("Patient feels better, continue as before.")[1],
                         Status.NOT_STATED)

    def test_the_gazetteer_is_only_a_fallback(self):
        note = "Continue metformin as before"
        self.assertEqual(self.pick(note)[1], Status.NOT_STATED)
        self.assertEqual(self.pick(note, frozenset({"metformin"}))[0], "metformin")

    def test_a_form_mention_beats_a_gazetteer_hit_later_in_the_note(self):
        note = "Tablet Dolo now, and metformin continues"
        self.assertEqual(self.pick(note, frozenset({"metformin"}))[0], "Dolo")


class Dose(unittest.TestCase):
    def pick(self, note):
        medication, after = baseline.find_medication(note, frozenset())
        field = baseline.find_dose(note, after, medication)
        return field.value, field.status

    def test_a_strength_with_a_unit_is_a_dose(self):
        self.assertEqual(self.pick("Tab Amoxicillin 500 mg twice a day")[0], "500 mg")

    def test_a_count_of_tablets_is_not_a_dose(self):
        # FIELD_RULES: a quantity per administration is not a dose.
        self.assertEqual(self.pick("Tab Combiflam 1 tablet three times a day")[1],
                         Status.NOT_STATED)

    def test_a_bare_strength_inside_the_product_name_counts(self):
        self.assertEqual(self.pick("Tab Dolo 650 twice a day")[0], "650")

    def test_no_strength_at_all_is_not_stated(self):
        self.assertEqual(self.pick("Tab Dolo twice a day")[1], Status.NOT_STATED)


class Frequency(unittest.TestCase):
    def pick(self, note):
        _, after = baseline.find_medication(note, frozenset())
        return baseline.find_frequency(note, after).value

    def test_shorthand_is_found(self):
        self.assertEqual(self.pick("Tab Dolo 650 TDS after food"), "TDS")

    def test_a_digit_regimen_is_found(self):
        self.assertEqual(self.pick("Tablet Cogre P forte 1-0-1 for 15 days"), "1-0-1")

    def test_a_phrase_is_found(self):
        self.assertEqual(self.pick("Tab Dolo 650 twice a day"), "twice a day")


class Allergy(unittest.TestCase):
    def test_a_denial_is_not_an_allergy(self):
        for note in ("No known allergies to any drug", "Patient denies allergies to penicillin"):
            with self.subTest(note=note):
                self.assertEqual(baseline.find_allergy(note).status, Status.NOT_STATED)

    def test_a_stated_allergy_is_captured(self):
        self.assertEqual(baseline.find_allergy("She is allergic to penicillin").value,
                         "penicillin")


class StructuralProperties(unittest.TestCase):
    NOTES = [
        "Tablet Dolo 650 twice a day after food. Tablet Pan D before food.",
        "Tab Amoxicillin 500 mg TDS for 5 days. Allergic to sulfa drugs.",
        "Patient reviewed, no medication issued today.",
        "Syrup Augmentin 5ml 1-0-1, she is allergic to penicillin",
    ]

    def test_every_span_the_baseline_emits_is_verbatim(self):
        # A regex extractor cannot fail the grounding gate: its evidence is a
        # slice of the note by construction. Pinned because it is the reason the
        # abstention machinery measures nothing on this arm.
        for note in self.NOTES:
            extraction = baseline.extract_one(note, frozenset())
            for name, field in extraction:
                with self.subTest(note=note[:20], field=name):
                    self.assertTrue(verify_evidence(name, field, note).ok)

    def test_it_never_claims_to_be_unsure(self):
        for note in self.NOTES:
            for _, field in baseline.extract_one(note, frozenset()):
                self.assertIn(field.status, (Status.FOUND, Status.NOT_STATED))


class Splits(unittest.TestCase):
    def test_the_tuning_subsample_is_deterministic(self):
        ids = [f"case_{i:03d}" for i in range(1, 68)]
        first = baseline.tuning_subsample(ids)
        self.assertEqual(first, baseline.tuning_subsample(ids))
        self.assertEqual(len(first), baseline.TUNE_N)

    def test_a_different_seed_draws_a_different_subsample(self):
        ids = [f"case_{i:03d}" for i in range(1, 68)]
        self.assertNotEqual(baseline.tuning_subsample(ids, seed=1),
                            baseline.tuning_subsample(ids, seed=2))


class EndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        cases = [
            gold_case("case_001", "Tablet Dolo 650 twice a day after food.",
                      medication=truth("Dolo"), dose=truth("650"),
                      frequency=truth("twice a day")),
            gold_case("case_002", "Reviewed today, no medication issued."),
        ]
        self.gold = self.root / "gold.json"
        self.gold.write_text(json.dumps(
            {"schema_version": "gold-v1", "provenance": {}, "cases": cases}), encoding="utf-8")
        self.out = self.root / "arm"

    def run_baseline(self, *extra):
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = baseline.main(["--gold", str(self.gold), "--out", str(self.out), *extra])
        return code, out.getvalue(), err.getvalue()

    def score(self, *extra):
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = score_arm.main([str(self.out), "--gold", str(self.gold), *extra])
        return code, out.getvalue(), err.getvalue()

    def test_it_writes_a_run_shaped_directory(self):
        code, out, _ = self.run_baseline()
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), str(self.out))
        for name in ("manifest.json", "gated.jsonl", "ungated.jsonl"):
            self.assertTrue((self.out / name).is_file(), name)

    def test_the_manifest_records_the_rule_and_the_tuning_draw(self):
        self.run_baseline()
        manifest = json.loads((self.out / "manifest.json").read_text())
        self.assertEqual(manifest["extractor"]["selection_rule"],
                         "first <form> <Name> mention in the note")
        self.assertEqual(manifest["extractor"]["gazetteer"], None)
        self.assertEqual(manifest["split"]["tuning_seed"], baseline.TUNE_SEED)
        self.assertEqual(manifest["spend"]["billed_this_run_usd"], 0.0)

    def test_the_scorer_reports_the_majority_class_beside_the_arm(self):
        self.run_baseline()
        code, out, err = self.score()
        self.assertEqual(code, 0)
        summary = json.loads(out)
        base = summary["majority_class_baseline"]
        # case_001 has 3 found fields and 1 not_stated; case_002 has 4 not_stated.
        self.assertEqual((base["field_decisions"], base["agrees_with_gold"]), (8, 5))
        self.assertEqual(base["recall"], 0.0)
        self.assertIn("majority class", err)

    def test_the_scorer_refuses_to_hold_out_what_was_never_tuned(self):
        self.run_baseline()
        manifest_path = self.out / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["split"]["tuning_case_ids"] = []
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        code, out, err = self.score("--split", "report")
        self.assertEqual(code, 2)
        self.assertIn("REFUSED", err)
        self.assertEqual(out, "")

    def test_the_diagnosis_tool_reads_the_baseline_without_a_special_case(self):
        # The arms are run-shaped on purpose: one diagnosis tool for all of them.
        self.run_baseline()
        self.score("--quiet")
        report = diagnose.diagnose(self.out, self.gold)
        self.assertEqual(report["run"]["cases_scored"], 2)
        self.assertEqual(report["operations"]["billed_usd"], 0.0)
        self.assertEqual(report["arms"][0]["recall"],
                         json.loads((self.out / "scores.json").read_text())["pooled"]["recall"])


if __name__ == "__main__":
    unittest.main()
