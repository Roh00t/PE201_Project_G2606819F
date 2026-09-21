"""gold-v2 corrections and the allergy set. Both tools write sealed ground
truth, so what is pinned here is that they refuse to do it carelessly: gold-v1 is
verified before it is read, a correction nobody signed blocks the seal, and a
label must be verbatim in every variant it will be scored against."""

import json
import re
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "evals"))

import diagnose  # noqa: E402
import label_allergy_v1 as allergy  # noqa: E402
import label_gold_v2 as v2  # noqa: E402
from extract import CRITICAL_FIELDS, Status  # noqa: E402


class CorrectionsMatchTheAudit(unittest.TestCase):
    """The corrections table is hand-written; the audit is computed. If they
    drift apart, the table is either fixing something that is not broken or
    missing something that is."""

    @classmethod
    def setUpClass(cls):
        gold = json.loads((ROOT / "data" / "gold_labels" / "gold_v1.json")
                          .read_text(encoding="utf-8"))
        cls.audit = diagnose.gold_audit(gold["cases"])
        cls.declared = {(c, f) for c, f, *_ in v2.CORRECTIONS}

    def test_every_field_the_audit_flags_is_corrected(self):
        flagged = {(row["case_id"], row["field"]) for row in self.audit}
        self.assertEqual(flagged - self.declared, set(),
                         "the audit flags labels the corrections table ignores")

    def test_no_correction_invents_a_problem(self):
        # Every corrected field is either audit-flagged or a declared cascade of
        # one, and a cascade must name the case that caused it.
        flagged = {(row["case_id"], row["field"]) for row in self.audit}
        cascades = {("case_001", "dose"), ("case_010", "medication")}
        self.assertEqual(self.declared - flagged, cascades)

    def test_each_correction_carries_the_rule_that_requires_it(self):
        for case_id, field, _, kind, why in v2.CORRECTIONS:
            self.assertIn(kind, (v2.RULE, v2.REVIEW), f"{case_id}.{field}")
            self.assertGreater(len(why), 30, f"{case_id}.{field}: rule text too thin")


class SealingDiscipline(unittest.TestCase):
    def build(self, confirmed=None):
        return v2.build(confirmed or {})

    def test_review_corrections_are_withheld_until_signed(self):
        _, applied, unconfirmed = self.build()
        self.assertTrue(unconfirmed, "the review corrections should wait on a human")
        self.assertEqual({e["correction"] for e in unconfirmed},
                         {"case_010.medication", "case_010.frequency"})
        self.assertNotIn("case_010.medication",
                         {f"{a['case_id']}.{a['field']}" for a in applied
                          if a["kind"] == v2.REVIEW})

    def test_a_signature_lets_them_through_and_is_recorded(self):
        _, applied, unconfirmed = self.build(
            {"case_010.medication": "someone", "case_010.frequency": "someone"})
        self.assertEqual(unconfirmed, [])
        signed = [a for a in applied if a["kind"] == v2.REVIEW]
        self.assertEqual(len(signed), 2)
        self.assertEqual({a["confirmed_by"] for a in signed}, {"someone"})

    def test_rule_corrections_record_that_no_human_was_needed(self):
        _, applied, _ = self.build()
        for entry in applied:
            self.assertEqual(entry["confirmed_by"], "rule-derived")

    def test_sealing_without_a_labeller_is_refused(self):
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = v2.main(["validate", "--seal"])
        self.assertEqual(code, v2.EXIT_REFUSED)
        self.assertIn("requires --labeller", err.getvalue())

    def test_sealing_is_refused_while_a_correction_is_unsigned(self):
        # The invariant is that a refused seal changes nothing on disk - not that
        # gold-v2 is absent. It is present now, legitimately sealed, and a test
        # that asserted absence was coupled to the repository's data state rather
        # than to the behaviour it meant to pin.
        before = (v2.V2_PATH.read_bytes() if v2.V2_PATH.exists() else None)
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = v2.main(["validate", "--seal", "--labeller", "someone"])
        self.assertEqual(code, v2.EXIT_NOT_READY)
        self.assertIn("refusing to seal", err.getvalue())
        after = (v2.V2_PATH.read_bytes() if v2.V2_PATH.exists() else None)
        self.assertEqual(before, after, "a refused seal must not touch gold-v2")

    def test_sealing_over_an_existing_version_is_refused_outright(self):
        # Write-once. With every correction signed, the only thing standing
        # between a rerun and a clobbered ground truth is this refusal.
        if not v2.V2_PATH.exists():
            self.skipTest("gold-v2 is not sealed yet")
        before = v2.V2_PATH.read_bytes()
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = v2.main(["validate", "--seal", "--labeller", "someone",
                            "--confirm", "case_010.medication",
                            "--confirm", "case_010.frequency"])
        self.assertEqual(code, v2.EXIT_REFUSED)
        self.assertIn("already exists", err.getvalue())
        self.assertEqual(before, v2.V2_PATH.read_bytes())


class WhatGoldV2Contains(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.goldset, cls.applied, _ = v2.build(
            {"case_010.medication": "t", "case_010.frequency": "t"})
        cls.by_id = {c.case_id: c for c in cls.goldset.cases}

    def test_it_declares_itself_as_gold_v2(self):
        # The registry refuses a file that does not say which version it is.
        self.assertEqual(self.goldset.schema_version, "gold-v2")

    def test_every_case_from_gold_v1_survives(self):
        self.assertEqual(len(self.goldset.cases), 67)

    def test_the_quantities_the_dose_rule_excludes_are_now_not_stated(self):
        for case_id in ("case_003", "case_004", "case_009", "case_015", "case_016",
                        "case_034"):
            field = self.by_id[case_id].ground_truth["dose"]
            self.assertIs(field.status, Status.NOT_STATED, case_id)
            self.assertIsNone(field.value, case_id)

    def test_the_span_that_did_not_support_its_value_is_repaired(self):
        field = self.by_id["case_001"].ground_truth["medication"]
        self.assertEqual((field.value, field.evidence), ("chymoral forte", "chymoral forte"))

    def test_meal_timing_is_out_of_the_frequency_values(self):
        for case_id in ("case_012", "case_050", "case_057", "case_061", "case_063"):
            value = self.by_id[case_id].ground_truth["frequency"].value
            self.assertNotRegex(value, r"(?i)\b(?:after|before)\s+(?:food|meal|breakfast)")

    def test_the_evidence_keeps_the_whole_dictated_phrase(self):
        # The value is the fact; the evidence is where it came from, and it stays
        # verbatim so the physician can still see the sentence.
        field = self.by_id["case_050"].ground_truth["frequency"]
        self.assertEqual(field.value, "twice a day")
        self.assertEqual(field.evidence, "twice a day after food")

    def test_slice_tags_are_derivable_from_the_note(self):
        for case in self.goldset.cases:
            for tag in re.findall(r"#(\w+)", case.notes or ""):
                if tag == "multidrug":
                    continue
                # The tagger matches case-insensitively, so the check must too:
                # "No vomiting" is a negation and assertRegex is case-sensitive.
                self.assertTrue(
                    re.search(v2.SLICE_PATTERNS[tag], case.source_text, re.I),
                    f"{case.case_id}: #{tag} is not derivable from the note")

    def test_the_provenance_names_what_it_was_derived_from(self):
        derived = self.goldset.provenance["derived_from"]
        self.assertEqual(derived["version"], "gold-v1")
        self.assertEqual(len(derived["sha256"]), 64)
        self.assertEqual(len(self.goldset.provenance["corrections"]), len(v2.CORRECTIONS))


class HeaderStripping(unittest.TestCase):
    def test_an_all_caps_header_is_removed(self):
        self.assertNotIn("ALLERGIES:", allergy.strip_headers("ALLERGIES:, Penicillin."))

    def test_the_substance_survives_the_strip(self):
        self.assertIn("Penicillin", allergy.strip_headers("ALLERGIES:, Penicillin."))

    def test_an_all_caps_substance_is_not_mistaken_for_a_header(self):
        # "SULFA AND LATEX" has no colon, so it is a value and not a section.
        self.assertIn("SULFA AND LATEX",
                      allergy.strip_headers("ALLERGIES:, SULFA AND LATEX."))

    def test_lower_case_prose_is_untouched(self):
        text = "He is allergic to penicillin, which causes a rash."
        self.assertEqual(allergy.strip_headers(text), text)


class TheAllergySet(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.intact, _ = allergy.build(strip=False)
        cls.stripped, _ = allergy.build(strip=True)

    def test_both_variants_validate(self):
        self.assertEqual(allergy.validate(self.intact), [])
        self.assertEqual(allergy.validate(self.stripped), [])

    def test_the_two_variants_are_the_same_cases_with_the_same_labels(self):
        # The leakage comparison is only paired if this holds.
        self.assertEqual([c.case_id for c in self.intact.cases],
                         [c.case_id for c in self.stripped.cases])
        for a, b in zip(self.intact.cases, self.stripped.cases):
            for name in CRITICAL_FIELDS:
                self.assertEqual(a.ground_truth[name].value, b.ground_truth[name].value,
                                 f"{a.case_id}.{name}")

    def test_only_the_note_text_differs(self):
        for a, b in zip(self.intact.cases, self.stripped.cases):
            self.assertNotEqual(a.source_text, b.source_text, a.case_id)
            self.assertLess(len(b.source_text), len(a.source_text), a.case_id)

    def test_it_supplies_the_allergy_positives_gold_v1_has_none_of(self):
        positives = sum(1 for c in self.intact.cases
                        if c.ground_truth["allergy"].status is Status.FOUND)
        self.assertEqual(positives, len(self.intact.cases))
        self.assertGreaterEqual(positives, 20)

    def test_each_variant_declares_its_own_version(self):
        self.assertEqual(self.intact.schema_version, "allergy-v1")
        self.assertEqual(self.stripped.schema_version, "allergy-v1-stripped")

    def test_the_provenance_says_what_this_set_cannot_measure(self):
        for goldset in (self.intact, self.stripped):
            self.assertIn("medication selection", goldset.provenance["does_not_measure"])
            self.assertIn("not_comparable_with", goldset.provenance["labelling_rules"])

    def test_the_selection_is_seeded_and_recorded(self):
        selection = self.intact.provenance["selection"]
        self.assertEqual(selection["seed"], allergy.SEED)
        self.assertEqual(len(selection["row_idx"]), len(self.intact.cases))
        self.assertEqual(len(selection["text_md5"]), len(self.intact.cases))

    def test_rebuilding_gives_the_same_set(self):
        again, _ = allergy.build(strip=False)
        self.assertEqual([c.case_id for c in again.cases],
                         [c.case_id for c in self.intact.cases])
        self.assertEqual([c.source_text for c in again.cases],
                         [c.source_text for c in self.intact.cases])


class SealedFilesAreReadOnly(unittest.TestCase):
    def test_every_sealed_gold_file_matches_its_hash_and_cannot_be_written(self):
        import hashlib
        import os
        gold_dir = ROOT / "data" / "gold_labels"
        sealed = [(gold_dir / f"{name}.json", gold_dir / f"{name}.sha256")
                  for name in ("gold_v1", "allergy_v1", "allergy_v1_stripped")]
        for path, sha_path in sealed:
            if not path.is_file():
                continue
            with self.subTest(path=path.name):
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                self.assertEqual(digest, sha_path.read_text(encoding="utf-8").split()[0])
                self.assertFalse(os.access(path, os.W_OK), "sealed gold is writable")


if __name__ == "__main__":
    unittest.main()
