"""Two refused controls, and the tests that keep a refusal honest.

A refusal is a claim like any other and can rot in the same ways. Two failure
modes specifically:

* **The measurement passes vacuously.** If the population is empty, "caught 0"
  proves nothing. `ThePopulationIsRealBeforeAnythingIsConcluded` fails unless
  there are silent failures and correct fields to measure against.
* **A refused control gets quietly implemented.** `NeitherControlIsShipped`
  fails if `needs_second_pass` appears under `src/` or `evals/`, because that
  name turns the specification fence in `guardrails.md` into a claim of shipped
  code that `tests/test_guardrails_doc.py` then enforces.

The S-16 finding is the one worth protecting: its trigger word *is* the header
that stripping removes, so it fires where it is unnecessary and is silent where it
is needed. `TheTriggerIsCircular` pins that mechanism directly rather than
trusting the sensitivity number that follows from it.
"""

import ast
import json
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "evals"))

import refused_controls as rc  # noqa: E402

ARTEFACT = ROOT / "evals" / "results" / "refused-controls" / "measurement.json"


class ThePopulationIsRealBeforeAnythingIsConcluded(unittest.TestCase):
    """"Caught 0 of 0" is not a finding."""

    @classmethod
    def setUpClass(cls):
        cls.result = rc.measure_proximity()

    def test_there_are_silent_failures_to_catch(self):
        self.assertEqual(self.result["population"]["silent_failures"], 36)

    def test_there_are_correct_fields_to_endanger(self):
        self.assertGreater(self.result["population"]["correct_verified"], 100)

    def test_the_sweep_covers_more_than_one_window(self):
        self.assertGreater(len(self.result["sweep"]["literal"]), 3)
        self.assertIn(None, [r["window_words"] for r in self.result["sweep"]["literal"]])


class TheProximityGateCatchesNothing(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = rc.measure_proximity()

    def test_the_specified_lexicon_catches_nothing_at_any_window(self):
        for row in self.result["sweep"]["literal"]:
            with self.subTest(window=row["window_words"]):
                self.assertEqual(row["silent_caught"], 0)

    def test_most_of_the_specified_words_are_absent_from_the_corpus(self):
        prevalence = self.result["literal_cue_prevalence_in_67_notes"]
        absent = [cue for cue, n in prevalence.items() if n == 0]
        self.assertGreaterEqual(len(absent), 14)
        for cue in ("father", "mother", "stopped", "discontinued", "was on"):
            self.assertEqual(prevalence[cue], 0, f"{cue} was expected to be absent")

    def test_broadening_the_lexicon_destroys_more_than_it_catches(self):
        # The inversion is the finding: it only fires once it is wrong.
        for row in self.result["sweep"]["extended"]:
            with self.subTest(window=row["window_words"]):
                self.assertGreater(row["correct_destroyed"], row["silent_caught"])

    def test_it_would_wipe_correct_fields_whose_cue_is_the_prescription_itself(self):
        victims = self.result["correct_fields_a_5_word_extended_gate_would_wipe"]
        self.assertGreater(len(victims), 5)
        # case_053's note reads "has been taking Dolo 650" and gold is Dolo / 650.
        self.assertTrue(any(v.startswith("case_053") for v in victims),
                        f"case_053 is the decisive counter-example; got {victims}")

    def test_the_verdict_is_recorded_as_refused(self):
        self.assertEqual(self.result["verdict"], "REFUSED")


class TheTriggerIsCircular(unittest.TestCase):
    """The mechanism, pinned directly. The sensitivity number is a consequence."""

    @classmethod
    def setUpClass(cls):
        cls.result = rc.measure_s16()
        cls.arms = cls.result["arms"]

    def test_the_hint_matches_every_intact_note_and_a_minority_of_stripped_ones(self):
        intact = self.arms["leak-allergy-v1"]
        stripped = self.arms["leak-allergy-v1-stripped"]
        self.assertEqual(intact["hint_matches_note"], intact["cases"])
        self.assertLess(stripped["hint_matches_note"], stripped["cases"])

    def test_the_headers_the_hint_depends_on_are_what_stripping_removes(self):
        intact = self.arms["leak-allergy-v1"]["caps_headers_surviving"]
        stripped = self.arms["leak-allergy-v1-stripped"]["caps_headers_surviving"]
        self.assertGreater(intact, 100)
        self.assertLess(stripped, 5)

    def test_it_flags_none_of_the_allergies_that_were_missed(self):
        stripped = self.arms["leak-allergy-v1-stripped"]
        self.assertGreater(stripped["allergies_missed"], 0, "nothing was missed to flag")
        self.assertEqual(stripped["missed_and_flagged"], 0)
        self.assertEqual(self.result["sensitivity"]["flagged"], 0)

    def test_it_is_silent_where_the_model_already_succeeds(self):
        # Headers intact: the model finds the allergy, so the trigger's first
        # condition (an empty field) is false and it correctly does not fire.
        self.assertEqual(self.arms["leak-allergy-v1"]["trigger_fired"], 0)
        self.assertEqual(self.arms["leak-allergy-v1"]["allergies_missed"], 0)

    def test_the_deployment_corpus_cannot_measure_it_at_all(self):
        gold = self.arms["gold-v2-live"]
        self.assertEqual(gold["allergy_positives_in_gold"], 0)

    def test_the_reframing_of_6_6_is_recorded_with_its_evidence(self):
        why = self.result["reframes_6_6"]
        self.assertIn("carried over", why)
        self.assertIn("not derivable", why)
        self.assertIn("clinical signal", why)

    def test_the_verdict_is_recorded_as_refused(self):
        self.assertEqual(self.result["verdict"], "REFUSED")


class NeitherControlIsShipped(unittest.TestCase):
    """A refused control must not appear in the pipeline."""

    def shipped_symbols(self):
        names = set()
        for path in sorted([*(ROOT / "src").glob("*.py"), *(ROOT / "evals").glob("*.py"),
                            *(ROOT / "demo").glob("*.py")]):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    names.add(node.name)
        return names

    def test_needs_second_pass_is_not_defined_anywhere(self):
        # Defining it would turn the guardrails.md specification fence into a
        # claim of shipped code, which test_guardrails_doc.py then enforces -
        # and would assert the control is implemented when it is refused.
        self.assertNotIn("needs_second_pass", self.shipped_symbols())

    def test_the_pipeline_has_no_allergy_or_denial_pattern(self):
        source = (ROOT / "src" / "extract.py").read_text(encoding="utf-8")
        for banned in ("ALLERGY_HINTS", "DENIAL", "needs_second_pass", "recall_flags"):
            self.assertNotIn(banned, source,
                             f"{banned} is in the pipeline; S-16 was refused")

    def test_the_gate_codes_are_unchanged(self):
        import extract
        self.assertEqual(len([n for n in dir(extract)
                              if n.startswith(("ABSTAIN_", "REVIEW_"))]), 10)

    def test_the_measurement_script_is_not_imported_by_the_pipeline(self):
        tree = ast.parse((ROOT / "src" / "extract.py").read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                self.assertNotEqual(node.module, "refused_controls")
            if isinstance(node, ast.Import):
                self.assertNotIn("refused_controls", [a.name for a in node.names])


class TheScoredRatesAreUntouched(unittest.TestCase):
    """The whole point of refusing is that nothing moved. Prove it."""

    def test_the_headline_is_still_what_it_was(self):
        scores = json.loads((ROOT / "evals" / "results" / "gold-v2-live"
                             / "scores.json").read_text(encoding="utf-8"))["pooled"]
        self.assertEqual(scores["recall"], 0.7297)
        self.assertEqual(scores["precision"], 0.75)
        self.assertEqual(scores["silent_failure_rate"], 0.2517)

    def test_no_gate_removed_a_correct_value(self):
        scores = json.loads((ROOT / "evals" / "results" / "gold-v2-live"
                             / "scores.json").read_text(encoding="utf-8"))["pooled"]
        self.assertEqual(scores["gate_false_positive_rate"], 0.0)


class TheArtefactIsReproducible(unittest.TestCase):
    def setUp(self):
        if not ARTEFACT.is_file():
            self.skipTest("run `evals/refused_controls.py` first")
        self.saved = json.loads(ARTEFACT.read_text(encoding="utf-8"))

    def test_recomputing_gives_the_same_numbers(self):
        fresh = {"proximity_gate": rc.measure_proximity(), "s16": rc.measure_s16()}
        for key in ("proximity_gate", "s16"):
            with self.subTest(block=key):
                self.assertEqual(fresh[key], self.saved[key])

    def test_it_records_that_no_model_was_called(self):
        self.assertTrue(self.saved["no_model_calls"])

    def test_both_verdicts_are_refused(self):
        self.assertEqual(self.saved["proximity_gate"]["verdict"], "REFUSED")
        self.assertEqual(self.saved["s16"]["verdict"], "REFUSED")


class TheSpecifiedTriggerIsQuotedNotReinvented(unittest.TestCase):
    """The measurement must be of the control that was proposed."""

    def test_the_hint_pattern_matches_the_specification(self):
        fence = (ROOT / "guardrails.md").read_text(encoding="utf-8")
        self.assertIn(r"\ballerg\w*|\bintoleran\w*|\breaction to\b|\bsensitivity to\b",
                      fence)
        self.assertEqual(
            rc.ALLERGY_HINTS.pattern,
            r"\ballerg\w*|\bintoleran\w*|\breaction to\b|\bsensitivity to\b")

    def test_the_denial_pattern_matches_the_sealed_corpus_builder(self):
        # Quoted, not imported: src/ and evals/ do not import from data/, and the
        # measurement must use the pattern as it stood when the corpora were sealed.
        builder = (ROOT / "data" / "allergy_set" / "fetch_mtsamples.py").read_text(
            encoding="utf-8")
        head = r"\bno\s+(?:known\s+)?(?:drug\s+|medication\s+|food\s+|medicine\s+)?allerg\w*"
        self.assertIn(head, builder)
        self.assertIn(head, rc.DENIAL.pattern)

    def test_the_trigger_behaves_as_specified_on_a_denial(self):
        self.assertFalse(rc.s16_trigger("No known drug allergies.", None))
        self.assertTrue(rc.s16_trigger("Reports an allergy to penicillin.", None))

    def test_a_filled_field_never_triggers(self):
        self.assertFalse(rc.s16_trigger("Reports an allergy to penicillin.", "penicillin"))

    def test_the_sealed_pattern_still_misses_the_phrasing_the_spec_names(self):
        # "he has no specific allergies" - an adjective between "no" and
        # "allergies". Recorded because the specification proposes widening the
        # pattern to catch it, and that widening is deliberately NOT shipped:
        # with S-16 refused it would be dead code.
        self.assertIsNone(rc.DENIAL.search("he has no specific allergies"))
        widened = re.compile(
            r"\bno\s+(?:known\s+)?(?:\w+\s+)?allerg\w*", re.I)
        self.assertIsNotNone(widened.search("he has no specific allergies"))


if __name__ == "__main__":
    unittest.main()
