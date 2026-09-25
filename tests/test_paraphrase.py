"""The near-distribution arm.

This corpus is only worth running if three things hold, and each is a test rather
than an intention:

* the gold values are byte-identical to gold-v2 (`PairingInvariant`) - otherwise
  the McNemar against the gold-v2 run is not paired and the headline is wrong;
* no transform ever edits inside a value span (`ProtectionIsEnforced`) - which is
  what makes the first property true by construction rather than by luck;
* rebuilding produces the same corpus (`Determinism`) - a sealed set that cannot
  be regenerated cannot be checked by anyone else.
"""

import json
import random
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "evals"))

import paraphrase as P  # noqa: E402
from extract import CRITICAL_FIELDS, Status  # noqa: E402


class ProtectedSpans(unittest.TestCase):
    def test_every_occurrence_is_protected_whatever_its_case(self):
        text = "Tablet Vernace in the afternoon. Continue vernace."
        spans = P.protected_spans(text, ["Vernace"])
        self.assertEqual(len(spans), 2)
        self.assertEqual([text[a:b] for a, b in spans], ["Vernace", "vernace"])

    def test_overlapping_values_merge_into_one_range(self):
        text = "Gabantin GRS 300 milligram"
        spans = P.protected_spans(text, ["Gabantin GRS", "GRS 300"])
        self.assertEqual(len(spans), 1)

    def test_a_value_absent_from_the_note_contributes_nothing(self):
        self.assertEqual(P.protected_spans("no drugs here", ["warfarin"]), [])

    def test_segments_reconstruct_the_original_exactly(self):
        text = "Tablet paracetamol 500 three times a day."
        spans = P.protected_spans(text, ["paracetamol", "three times a day"])
        self.assertEqual("".join(c for _, c in P.segments(text, spans)), text)


class ProtectionIsEnforced(unittest.TestCase):
    """The load-bearing property: a transform may rewrite anything except a value."""

    def test_no_transform_touches_a_protected_span(self):
        text = ("Tablet paracetamol 500 three times a day. After food  Tablet "
                "chymoral forte three times a day after food.")
        values = ["chymoral forte", "three times a day"]
        out, fired = P.paraphrase(text, values, random.Random(0))
        self.assertTrue(fired)
        for value in values:
            self.assertIn(value, out)

    def test_a_transform_that_would_hit_a_value_is_neutralised_by_protection(self):
        # `Tab.` is a dosage form the transform strips - but here it is part of
        # the drug name itself, so protection must save it.
        text = "Give Tab. Forte now. Tablet aspirin later."
        out, _ = P.paraphrase(text, ["Tab. Forte"], random.Random(0))
        self.assertIn("Tab. Forte", out)
        self.assertNotIn("Tablet aspirin", out)      # the unprotected one is stripped

    def test_meal_timing_inside_a_value_survives(self):
        out, _ = P.paraphrase("twice a day after food, and after food again",
                              ["twice a day after food"], random.Random(0))
        self.assertIn("twice a day after food", out)
        self.assertIn("post meals", out)             # the unprotected one moved

    def test_a_lost_value_is_refused(self):
        self.assertTrue(P.refuse_if_value_lost("c1", "nothing here", ["aspirin"]))
        self.assertFalse(P.refuse_if_value_lost("c1", "give aspirin", ["aspirin"]))

    def test_a_corpus_that_barely_moved_is_refused(self):
        self.assertTrue(P.refuse_if_corpus_barely_moved(40, 67))
        self.assertFalse(P.refuse_if_corpus_barely_moved(66, 67))


class Transforms(unittest.TestCase):
    def test_the_dosage_form_crutch_is_removed(self):
        self.assertEqual(P.drop_dosage_form("Tablet paracetamol", None), "paracetamol")
        self.assertEqual(P.drop_dosage_form("Cap. amoxicillin", None), "amoxicillin")

    def test_meal_timing_is_rephrased_not_deleted(self):
        moved = P.rephrase_meal_timing("take after food please", None)
        self.assertNotIn("after food", moved)
        self.assertIn("post meals", moved)

    def test_punctuation_drift_removes_a_sentence_boundary(self):
        self.assertNotIn(".", P.punctuation_drift("fever. breathless", None))

    def test_every_transform_is_named_in_the_provenance(self):
        goldset, _ = P.build()
        self.assertEqual(sorted(goldset.provenance["transforms"]),
                         sorted(name for name, _ in P.TRANSFORMS))


class PairingInvariant(unittest.TestCase):
    """If this fails, the McNemar against the gold-v2 run is not paired."""

    @classmethod
    def setUpClass(cls):
        cls.goldset, cls.report = P.build()
        cls.source = json.loads(P.SOURCE_PATH.read_text(encoding="utf-8"))

    def test_the_same_cases_in_the_same_order(self):
        self.assertEqual([c.case_id for c in self.goldset.cases],
                         [c["case_id"] for c in self.source["cases"]])

    def test_every_value_label_is_byte_identical_to_gold_v2(self):
        self.assertEqual(P.labels_match_source(self.goldset), [])

    def test_the_notes_really_differ(self):
        by_id = {c["case_id"]: c["source_text"] for c in self.source["cases"]}
        differ = sum(1 for c in self.goldset.cases if c.source_text != by_id[c.case_id])
        self.assertEqual(differ, self.report["varied"])
        self.assertGreaterEqual(differ, 60)

    def test_it_declares_its_own_version(self):
        self.assertEqual(self.goldset.schema_version, "paraphrase-v1")

    def test_it_passes_the_production_gate(self):
        self.assertEqual(P.validate(self.goldset), [])

    def test_the_status_pattern_is_unchanged(self):
        by_id = {c["case_id"]: c for c in self.source["cases"]}
        for case in self.goldset.cases:
            for name in CRITICAL_FIELDS:
                mine = case.ground_truth[name].status is Status.FOUND
                theirs = by_id[case.case_id]["ground_truth"][name]["status"] == "found"
                self.assertEqual(mine, theirs, f"{case.case_id}.{name}")

    def test_the_provenance_names_what_it_cannot_measure(self):
        cannot = " ".join(self.goldset.provenance["does_not_measure"])
        self.assertIn("same words", cannot)
        self.assertIn("gold-v2", self.goldset.provenance["derived_from"]["version"])
        self.assertEqual(len(self.goldset.provenance["derived_from"]["sha256"]), 64)


class Determinism(unittest.TestCase):
    def test_rebuilding_gives_the_same_corpus(self):
        first, _ = P.build()
        again, _ = P.build()
        self.assertEqual([c.source_text for c in first.cases],
                         [c.source_text for c in again.cases])

    def test_the_seed_is_recorded(self):
        goldset, _ = P.build()
        self.assertEqual(goldset.provenance["seed"], P.SEED)


class SealingDiscipline(unittest.TestCase):
    def build_argv(self, *extra):
        from contextlib import redirect_stderr, redirect_stdout
        from io import StringIO
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = P.main(["validate", *extra])
        return code, out.getvalue(), err.getvalue()

    def test_sealing_without_a_labeller_is_refused(self):
        # The invariant is that an unsigned seal changes nothing on disk - not
        # that paraphrase-v1 is absent. Skipping once the corpus exists would
        # couple the test to the repository's data state, which this project has
        # had to un-couple twice before.
        before = P.V1_PATH.read_bytes() if P.V1_PATH.exists() else None
        code, _, err = self.build_argv("--seal")
        self.assertEqual(code, P.EXIT_REFUSED)
        self.assertIn("requires --labeller", err)
        after = P.V1_PATH.read_bytes() if P.V1_PATH.exists() else None
        self.assertEqual(before, after, "a refused seal must not touch the corpus")

    def test_sealing_over_an_existing_version_is_refused(self):
        if not P.V1_PATH.exists():
            self.skipTest("paraphrase-v1 is not sealed yet")
        before = P.V1_PATH.read_bytes()
        with self.assertRaises(SystemExit):
            P.seal(P.build()[0], "someone")
        self.assertEqual(before, P.V1_PATH.read_bytes())

    def test_report_writes_nothing(self):
        from contextlib import redirect_stderr, redirect_stdout
        from io import StringIO
        before = P.V1_PATH.exists()
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            code = P.main(["report"])
        self.assertEqual(code, P.EXIT_OK)
        self.assertEqual(P.V1_PATH.exists(), before)


class SourceIsUntouched(unittest.TestCase):
    """CLAUDE.md §2.4 and §1.3: this builds a derived corpus and must not edit the
    dictation it derives from."""

    def test_gold_v2_still_matches_its_hash_and_stays_read_only(self):
        import hashlib
        import os
        digest = hashlib.sha256(P.SOURCE_PATH.read_bytes()).hexdigest()
        recorded = (P.GOLD_DIR / "gold_v2.sha256").read_text(encoding="utf-8").split()[0]
        self.assertEqual(digest, recorded)
        self.assertFalse(os.access(P.SOURCE_PATH, os.W_OK))

    def test_the_pipeline_does_not_import_this_module(self):
        import ast
        tree = ast.parse((ROOT / "src" / "extract.py").read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "paraphrase":
                self.fail("src/extract.py imports the paraphrase builder")
            if isinstance(node, ast.Import):
                self.assertNotIn("paraphrase", [a.name for a in node.names])


if __name__ == "__main__":
    unittest.main()
