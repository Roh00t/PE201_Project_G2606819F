"""The failure-attribution tool. Temporary directories only: no network, no
spend, and the sealed gold set is never opened."""

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

import diagnose  # noqa: E402
import scoring  # noqa: E402

FIELDS = ("medication", "dose", "frequency", "allergy")


def truth(value=None, evidence=None):
    if value is None:
        return {"value": None, "evidence": None, "status": "not_stated"}
    return {"value": value, "evidence": evidence if evidence is not None else value,
            "status": "found"}


def case(case_id, text, **labels):
    ground = {f: labels.get(f) or truth() for f in FIELDS}
    return {"case_id": case_id, "source_text": text, "ground_truth": ground,
            "labeller": "test", "labelled_at": "2026-09-20T00:00:00+00:00", "notes": None}


def payload(case_id, **values):
    """A finished extract.py payload: every named field VERIFIED, the rest not."""
    extraction, gate = {}, []
    for name in FIELDS:
        value = values.get(name)
        extraction[name] = ({"evidence": value, "value": value, "status": "found"} if value
                            else {"evidence": None, "value": None, "status": "not_stated"})
        gate.append({"field": name, "outcome": "pass" if value else "skipped",
                     "code": "VERIFIED" if value else "NOT_STATED", "near_miss": False})
    return {
        "case_id": case_id, "status": "ok", "schema_version": "mediextract.output.v3",
        "provenance": {"called": True}, "latency_ms": {"api": 1200.0, "total": 0.5},
        "within_budget": True, "input_scan": {"flags": [], "review_required": False},
        "usage": {"called": True, "billed_usd": 0.0006, "attempts": 1},
        "extraction": extraction, "gate": gate,
    }


GOLD = [
    # the model names the same drug and appends its strength
    case("case_001", "Tablet Dolo 650 twice a day after food.",
         medication=truth("Dolo"), dose=truth("650"),
         frequency=truth("twice a day after food")),
    # the model names a different drug from the same note
    case("case_002", "Tab Ondem one at night. Tab Mefenamic 500 twice a day.",
         medication=truth("Ondem"), dose=truth("one"), frequency=truth("at night")),
    # nothing to argue about
    case("case_003", "Tablet Metformin 500 mg twice daily.",
         medication=truth("Metformin"), dose=truth("500 mg"), frequency=truth("twice daily")),
]
RUN = {
    "case_001": payload("case_001", medication="Dolo 650", dose="650", frequency="twice a day"),
    "case_002": payload("case_002", medication="Mefenamic", dose="500", frequency="twice a day"),
    "case_003": payload("case_003", medication="Metformin", dose="500 mg", frequency="twice daily"),
}


def write_run(directory: Path) -> tuple[Path, Path]:
    run_dir = directory / "20260920T000000Z-test"
    run_dir.mkdir(parents=True)
    for name in ("gated.jsonl", "ungated.jsonl"):
        (run_dir / name).write_text(
            "".join(json.dumps(RUN[c["case_id"]]) + "\n" for c in GOLD), encoding="utf-8")
    (run_dir / "manifest.json").write_text(json.dumps({
        "run_id": "20260920T000000Z-test", "model": "google/gemini-2.5-flash", "mode": "mock",
        "experiment": None, "prompt_fingerprint": "0000000000000000",
        "gold": {"sealed_prompt_fingerprint": "0000000000000000"},
    }), encoding="utf-8")
    gold_path = directory / "gold.json"
    gold_path.write_text(json.dumps({"schema_version": "gold-v1", "provenance": {},
                                     "cases": GOLD}), encoding="utf-8")
    return run_dir, gold_path


class Attribution(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.run_dir, self.gold_path = write_run(Path(self.tmp.name))
        self.report = diagnose.diagnose(self.run_dir, self.gold_path)
        self.addCleanup(self.tmp.cleanup)

    def test_every_field_decision_gets_exactly_one_cause(self):
        self.assertEqual(sum(self.report["causes"].values()), len(GOLD) * len(FIELDS))

    def test_a_strength_appended_to_the_name_is_not_a_wrong_drug(self):
        causes = self.report["causes_by_field"]["medication"]
        self.assertEqual(causes.get("MED_STRENGTH_APPENDED"), 1)
        self.assertEqual(causes.get("MED_DIFFERENT_DRUG"), 1)
        self.assertEqual(causes.get("CORRECT"), 1)

    def test_a_dose_belonging_to_another_drug_is_told_apart_from_a_silent_model(self):
        causes = self.report["causes_by_field"]["dose"]
        self.assertEqual(causes.get("DOSE_GOLD_NOT_A_STRENGTH"), 1)
        self.assertIsNone(causes.get("DOSE_MODEL_SILENT"))

    def test_every_cause_has_a_legend_entry(self):
        for cause in self.report["causes"]:
            self.assertIn(cause, self.report["cause_legend"])

    def test_the_first_arm_reproduces_the_pre_registered_score(self):
        pooled = scoring.score(GOLD, RUN, RUN)["pooled"]
        first = self.report["arms"][0]
        self.assertEqual(first["relaxes"], [])
        for key in ("recall", "silent_failure_rate", "verified", "silent", "gold_found"):
            self.assertEqual(first[key], pooled[key], key)

    def test_each_later_arm_names_what_it_relaxes(self):
        for arm in self.report["arms"][1:]:
            self.assertTrue(arm["relaxes"], arm["arm"])

    def test_the_split_is_reported_for_both_groups(self):
        self.assertEqual(set(self.report["agreement_split"]),
                         {"drug choice agreed", "drug choice disagreed"})
        self.assertEqual(self.report["agreement_split"]["drug choice disagreed"]["cases"], 1)


class GoldAudit(unittest.TestCase):
    def audit(self, **labels):
        text = "Tablet Augmentin 1 tablet one twice a day after food. Augmentin."
        return {(row["field"], row["check"]) for row in
                diagnose.gold_audit([case("case_x", text, **labels)])}

    def test_a_dose_that_is_really_a_count_is_flagged(self):
        self.assertIn(("dose", "GOLD_DOSE_IS_A_COUNT"), self.audit(dose=truth("1 tablet")))

    def test_a_dose_without_a_number_is_flagged(self):
        self.assertIn(("dose", "ABSTAIN_NUMERIC_MISMATCH"), self.audit(dose=truth("one")))

    def test_a_frequency_carrying_food_timing_is_flagged(self):
        rows = diagnose.gold_audit([case("case_x", "twice a day after food",
                                         frequency=truth("twice a day after food"))])
        self.assertEqual(rows[0]["check"], "GOLD_FREQUENCY_CARRIES_FOOD_OR_DURATION")
        self.assertEqual(rows[0]["suggested"], "twice a day")

    def test_a_frequency_naming_no_interval_is_flagged(self):
        self.assertIn(("frequency", "GOLD_FREQUENCY_HAS_NO_TEMPORAL_MARKER"),
                      self.audit(frequency=truth("Augmentin")))

    def test_a_value_its_own_evidence_does_not_support_is_flagged(self):
        rows = diagnose.gold_audit([case("case_x", "Tablet paracetamol. Tablet chymoral forte.",
                                         medication=truth("chymoral forte", "paracetamol"))])
        self.assertEqual([r["check"] for r in rows], ["ABSTAIN_VALUE_UNGROUNDED"])

    def test_a_label_that_keeps_its_own_rules_is_not_flagged(self):
        self.assertEqual(diagnose.gold_audit([GOLD[2]]), [])

    def test_nothing_in_the_audit_writes_to_the_gold_file(self):
        before = (ROOT / "data" / "gold_labels" / "gold_v1.json").stat()
        diagnose.gold_audit(GOLD)
        after = (ROOT / "data" / "gold_labels" / "gold_v1.json").stat()
        self.assertEqual((before.st_mtime_ns, before.st_size), (after.st_mtime_ns, after.st_size))


class FrequencyCore(unittest.TestCase):
    def test_food_timing_and_duration_come_off(self):
        self.assertEqual(diagnose.frequency_core("twice a day after meal for 3 days"),
                         diagnose.frequency_core("twice a day"))

    def test_a_leading_count_comes_off_a_time_of_day(self):
        self.assertTrue(diagnose.frequency_matches_rule("one at night", "at night"))

    def test_a_count_in_front_of_times_stays(self):
        self.assertFalse(diagnose.frequency_matches_rule("two times a day", "three times a day"))

    def test_an_empty_side_never_matches(self):
        self.assertFalse(diagnose.frequency_matches_rule("", "twice a day"))


class CommandLine(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.run_dir, self.gold_path = write_run(Path(self.tmp.name))
        self.addCleanup(self.tmp.cleanup)

    def invoke(self, *argv):
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = diagnose.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_stdout_carries_one_json_document_and_nothing_else(self):
        code, out, err = self.invoke(str(self.run_dir), "--gold", str(self.gold_path))
        self.assertEqual(code, 0)
        report = json.loads(out)
        self.assertEqual(report["run"]["cases_scored"], len(GOLD))
        self.assertIn("DIAGNOSTIC ARMS", err)

    def test_quiet_still_prints_the_json_and_no_table(self):
        code, out, err = self.invoke(str(self.run_dir), "--gold", str(self.gold_path), "--quiet")
        self.assertEqual(code, 0)
        json.loads(out)
        self.assertEqual(err, "")

    def test_a_missing_run_directory_exits_2_with_empty_stdout(self):
        code, out, err = self.invoke(str(self.run_dir / "nope"), "--gold", str(self.gold_path))
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertTrue(err.strip())

    def test_the_seal_check_is_none_for_a_gold_file_that_is_not_the_sealed_one(self):
        _, out, _ = self.invoke(str(self.run_dir), "--gold", str(self.gold_path), "--quiet")
        self.assertIsNone(json.loads(out)["run"]["gold_sha256_matches_seal"])


if __name__ == "__main__":
    unittest.main()
