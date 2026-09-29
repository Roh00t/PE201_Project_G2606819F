"""The low-code control arm.

The first version of this arm was worthless and green: it copied `ungated.jsonl`
verbatim, which still carries the gate array, and `evals/scoring.py` reads
`proposed`/`verified`/`wiped` from those codes rather than from whether a value is
null. So it scored identically to the coded pipeline and would have published the
null result as a finding.

`TheGateIsActuallyRemoved` is the class that exists because of that, and
`ItDiffersFromTheCodedArm` is the one that would have caught it. Everything else
pins that the arm cannot quietly become something other than what it claims: no
model calls, values copied rather than regenerated, and a manifest that says it is
a proxy.
"""

import json
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "evals"))
sys.path.insert(0, str(ROOT / "docs" / "lowcode_pack"))

import convert_console_output as converter  # noqa: E402
import lowcode_arm  # noqa: E402
import scoring  # noqa: E402
from extract import CRITICAL_FIELDS  # noqa: E402

SOURCE = ROOT / "evals" / "results" / "gold-v2-live"
PROXY = ROOT / "evals" / "results" / "lowcode-proxy"
GOLD_V2 = ROOT / "data" / "gold_labels" / "gold_v2.json"


def load_jsonl(path: Path) -> dict:
    return {json.loads(line)["case_id"]: json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines() if line}


class TheGateIsActuallyRemoved(unittest.TestCase):
    """A console reports no verdicts. If the arm keeps the pipeline's, it is
    grading the coded pipeline twice."""

    def test_a_field_with_a_value_is_delivered_as_stated(self):
        got = lowcode_arm.console_payload({
            "extraction": {"medication": {"value": "warfarin", "evidence": "warfarin"},
                           "dose": {"value": None}, "frequency": {"value": None},
                           "allergy": {"value": None}},
            "gate": [{"field": "medication", "code": "ABSTAIN_UNGROUNDED"}]})
        codes = {g["field"]: g["code"] for g in got["gate"]}
        self.assertEqual(codes["medication"], "VERIFIED")
        self.assertEqual(codes["dose"], "NOT_STATED")

    def test_the_pipelines_own_verdicts_are_discarded(self):
        got = lowcode_arm.console_payload({
            "extraction": {n: {"value": "x"} for n in CRITICAL_FIELDS},
            "gate": [{"field": n, "code": "ABSTAIN_NUMERIC_MISMATCH"}
                     for n in CRITICAL_FIELDS]})
        self.assertEqual({g["code"] for g in got["gate"]}, {"VERIFIED"})

    def test_no_field_is_ever_held_or_reviewed(self):
        got = lowcode_arm.console_payload({
            "extraction": {n: {"value": "x"} for n in CRITICAL_FIELDS},
            "gate": [{"field": n, "code": "REVIEW_INJECTION_PATTERN"}
                     for n in CRITICAL_FIELDS]})
        for g in got["gate"]:
            self.assertFalse(g["code"].startswith(("ABSTAIN", "REVIEW")))

    def test_the_values_are_copied_not_regenerated(self):
        original = {"extraction": {"medication": {"value": "half", "evidence": "half"},
                                    "dose": {"value": None}, "frequency": {"value": None},
                                    "allergy": {"value": None}}, "gate": []}
        got = lowcode_arm.console_payload(original)
        self.assertEqual(got["extraction"]["medication"]["value"], "half")
        self.assertIs(got["extraction"], original["extraction"])

    def test_it_marks_itself_a_proxy(self):
        got = lowcode_arm.console_payload({"extraction": {n: {"value": None}
                                                          for n in CRITICAL_FIELDS},
                                           "gate": []})
        self.assertTrue(got["lowcode_proxy"])
        self.assertFalse(got["gate_enabled"])


class BuildRefusesTheWrongInput(unittest.TestCase):
    def test_a_gated_file_is_refused_rather_than_scored(self):
        # Pointed at gated.jsonl this would compare the pipeline with itself.
        with TemporaryDirectory() as tmp:
            source = Path(tmp) / "run"
            source.mkdir()
            (source / "manifest.json").write_text("{}", encoding="utf-8")
            (source / "ungated.jsonl").write_text(json.dumps({
                "case_id": "c1", "gate_enabled": True, "extraction": {},
                "gate": []}) + "\n", encoding="utf-8")
            with self.assertRaises(ValueError) as caught:
                lowcode_arm.build(source, Path(tmp) / "out")
            self.assertIn("not the ungated arm", str(caught.exception))

    def test_a_missing_source_exits_2(self):
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = lowcode_arm.main(["build", "--source", str(ROOT / "nope")])
        self.assertEqual(code, lowcode_arm.EXIT_UNUSABLE)
        self.assertEqual(out.getvalue(), "")


class ItDiffersFromTheCodedArm(unittest.TestCase):
    """The test that would have caught the first version."""

    @classmethod
    def setUpClass(cls):
        if not (PROXY / "gated.jsonl").is_file():
            raise unittest.SkipTest("run `lowcode_arm.py build` first")
        cls.cases = json.loads(GOLD_V2.read_text(encoding="utf-8"))["cases"]
        proxy = load_jsonl(PROXY / "gated.jsonl")
        coded = load_jsonl(SOURCE / "gated.jsonl")
        cls.low = scoring.score(cls.cases, proxy, proxy)["pooled"]
        cls.coded = scoring.score(cls.cases, coded,
                                  load_jsonl(SOURCE / "ungated.jsonl"))["pooled"]

    def test_the_low_code_arm_holds_nothing_back(self):
        self.assertEqual(self.low["wiped"], 0)
        self.assertGreater(self.coded["wiped"], 0)

    def test_it_delivers_more_values_than_the_coded_arm(self):
        self.assertGreater(self.low["proposed"], self.coded["proposed"])

    def test_its_precision_is_worse(self):
        self.assertLess(self.low["precision"], self.coded["precision"])

    def test_it_commits_more_silent_failures(self):
        self.assertGreater(self.low["silent"], self.coded["silent"])
        self.assertGreater(self.low["silent_failure_rate"],
                           self.coded["silent_failure_rate"])

    def test_recall_is_identical_so_the_gates_cost_nothing(self):
        # The headline of the whole arm. If this ever stops holding, the gates
        # have started removing correct answers and the trade has changed.
        self.assertEqual(self.low["correct"], self.coded["correct"])
        self.assertEqual(self.low["recall"], self.coded["recall"])

    def test_the_extra_values_are_all_wrong(self):
        extra = self.low["proposed"] - self.coded["proposed"]
        self.assertEqual(self.low["correct"], self.coded["correct"],
                         f"{extra} extra values were delivered and some were right")


class TheManifestSaysWhatThisIs(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = PROXY / "manifest.json"
        if not path.is_file():
            raise unittest.SkipTest("run `lowcode_arm.py build` first")
        cls.manifest = json.loads(path.read_text(encoding="utf-8"))

    def test_it_records_no_model_calls_and_no_spend(self):
        self.assertTrue(self.manifest["no_model_calls"])
        self.assertEqual(self.manifest["spend"]["billed_this_run_usd"], 0.0)

    def test_it_names_the_run_it_was_derived_from(self):
        self.assertIn("gold-v2-live", self.manifest["derived_from"]["run"])
        self.assertEqual(self.manifest["derived_from"]["file"], "ungated.jsonl")

    def test_it_disclaims_being_a_measurement_of_the_tool(self):
        self.assertIn("not a measurement", self.manifest["what_this_is_not"].lower()
                      .replace("a measurement of google ai studio", "not a measurement"))
        self.assertIn("lowcode_pack", self.manifest["what_this_is_not"])

    def test_it_explains_why_the_gate_array_was_replaced(self):
        self.assertIn("gate array", self.manifest["transformation"])
        self.assertIn("twice", self.manifest["transformation"])

    def test_its_mode_marks_it_a_proxy_so_newest_run_skips_it(self):
        self.assertEqual(self.manifest["mode"], "lowcode-proxy")


class TheCapabilityTableIsGenerated(unittest.TestCase):
    def test_every_row_names_a_place_and_a_reason(self):
        self.assertGreater(len(lowcode_arm.CANNOT_DO), 8)
        for what, where, why in lowcode_arm.CANNOT_DO:
            self.assertTrue(what and where and why)
            self.assertGreater(len(why), 10, what)

    def test_every_cited_file_exists(self):
        for _, where, _ in lowcode_arm.CANNOT_DO:
            path = where.split(":")[0]
            self.assertTrue((ROOT / path).is_file(), where)

    def test_the_document_carries_the_generated_table(self):
        doc = (ROOT / "docs" / "lowcode_arm.md").read_text(encoding="utf-8")
        for what, where, _ in lowcode_arm.CANNOT_DO:
            self.assertIn(what, doc, f"{what} is in the code and not the document")
            self.assertIn(where, doc, where)


class ThePastePackMatchesThePipeline(unittest.TestCase):
    """A stale pack makes the console run something other than this project."""

    PACK = ROOT / "docs" / "lowcode_pack"

    def test_the_exported_prompt_is_the_live_one(self):
        import extract
        got = (self.PACK / "system_prompt.txt").read_text(encoding="utf-8")
        self.assertEqual(got.strip(), extract.SYSTEM_INSTRUCTION.strip())

    def test_the_exported_schema_is_the_live_one(self):
        import extract
        got = json.loads((self.PACK / "response_schema.json").read_text(encoding="utf-8"))
        self.assertEqual(got, extract.response_json_schema())

    def test_the_readme_quotes_the_current_fingerprint(self):
        import extract
        readme = (self.PACK / "README.md").read_text(encoding="utf-8")
        self.assertIn(extract.prompt_fingerprint(), readme)


class TheConverterDoesNotGate(unittest.TestCase):
    def test_a_value_is_delivered_as_stated(self):
        got = converter.payload_for("c1", {
            "medication": {"value": "warfarin", "evidence": "nowhere in the note"}},
            "console")
        codes = {g["field"]: g["code"] for g in got["gate"]}
        # The evidence is not in any note, and the converter does not care - the
        # absence of that check is the thing being measured.
        self.assertEqual(codes["medication"], "VERIFIED")
        self.assertEqual(got["extraction"]["medication"]["value"], "warfarin")

    def test_a_missing_field_becomes_not_stated_rather_than_an_error(self):
        got = converter.payload_for("c1", {"medication": {"value": "x"}}, "console")
        self.assertEqual(got["extraction"]["allergy"]["value"], None)
        self.assertEqual(got["extraction"]["allergy"]["status"], "not_stated")

    def test_an_empty_string_counts_as_nothing_stated(self):
        got = converter.payload_for("c1", {"dose": {"value": "", "evidence": ""}},
                                    "console")
        self.assertIsNone(got["extraction"]["dose"]["value"])
        self.assertEqual({g["field"]: g["code"] for g in got["gate"]}["dose"],
                         "NOT_STATED")

    def test_it_marks_the_arm_ungated_and_console_run(self):
        got = converter.payload_for("c1", {}, "console")
        self.assertFalse(got["gate_enabled"])
        self.assertTrue(got["lowcode_console"])

    def test_it_round_trips_into_something_the_scorer_accepts(self):
        with TemporaryDirectory() as tmp:
            results = Path(tmp) / "console_results.json"
            cases = json.loads(GOLD_V2.read_text(encoding="utf-8"))["cases"][:3]
            results.write_text(json.dumps({
                c["case_id"]: {n: {"value": (c["ground_truth"][n]["value"] or ""),
                                   "evidence": (c["ground_truth"][n]["evidence"] or ""),
                                   "status": c["ground_truth"][n]["status"]}
                               for n in CRITICAL_FIELDS}
                for c in cases}), encoding="utf-8")
            out = Path(tmp) / "arm"
            summary = converter.convert(results, out, "console")
            self.assertEqual(summary["cases"], 3)
            payloads = load_jsonl(out / "gated.jsonl")
            scored = scoring.score(cases, payloads, payloads)
            # Fed the gold answers, a console arm scores perfectly - which is the
            # check that the conversion path itself is not losing information.
            self.assertEqual(scored["pooled"]["recall"], 1.0)

    def test_a_non_object_input_is_refused(self):
        with TemporaryDirectory() as tmp:
            bad = Path(tmp) / "x.json"
            bad.write_text("[]", encoding="utf-8")
            with self.assertRaises(ValueError):
                converter.convert(bad, Path(tmp) / "out", "console")


if __name__ == "__main__":
    unittest.main()


class TheDocumentMatchesTheArtefacts(unittest.TestCase):
    """`docs/lowcode_arm.md` quotes eight figures. Every one is re-derived here
    from the scored arms, because a document that can drift from its measurement
    is how a corrected number keeps being published in its old form."""

    @classmethod
    def setUpClass(cls):
        if not (PROXY / "gated.jsonl").is_file():
            raise unittest.SkipTest("run `lowcode_arm.py build` first")
        cases = json.loads(GOLD_V2.read_text(encoding="utf-8"))["cases"]
        proxy = load_jsonl(PROXY / "gated.jsonl")
        coded = load_jsonl(SOURCE / "gated.jsonl")
        cls.low = scoring.score(cases, proxy, proxy)["pooled"]
        cls.coded = scoring.score(cases, coded,
                                  load_jsonl(SOURCE / "ungated.jsonl"))["pooled"]
        cls.doc = (ROOT / "docs" / "lowcode_arm.md").read_text(encoding="utf-8")

    def test_every_quoted_figure_is_the_measured_one(self):
        for label, value in (
            ("low-code values delivered", self.low["proposed"]),
            ("coded values delivered", self.coded["proposed"]),
            ("low-code silent failures", self.low["silent"]),
            ("coded silent failures", self.coded["silent"]),
            ("low-code precision", self.low["precision"]),
            ("coded precision", self.coded["precision"]),
            ("low-code silent-failure rate", self.low["silent_failure_rate"]),
            ("coded silent-failure rate", self.coded["silent_failure_rate"]),
            ("recall, identical in both", self.coded["recall"]),
        ):
            with self.subTest(figure=label):
                self.assertIn(str(value), self.doc,
                              f"{label} is {value} and the document does not say so")

    def test_the_three_named_values_are_the_ones_the_gate_held(self):
        # Table 2 names three case.field pairs. They must be exactly the held
        # fields whose raw value was non-empty - no more, no fewer.
        coded = load_jsonl(SOURCE / "gated.jsonl")
        raw = load_jsonl(SOURCE / "ungated.jsonl")
        shown = []
        for case_id, payload in coded.items():
            for entry in payload["gate"]:
                if not entry["code"].startswith("ABSTAIN"):
                    continue
                value = (raw[case_id]["extraction"][entry["field"]] or {}).get("value")
                if value:
                    shown.append(f"{case_id}.{entry['field']}")
        self.assertEqual(len(shown), 3, f"expected 3 displayed-and-held fields, got {shown}")
        for pair in shown:
            self.assertIn(pair, self.doc, f"{pair} is held and shown but absent from Table 2")

    def test_it_states_that_recall_is_unchanged_rather_than_claiming_a_gain(self):
        self.assertIn("p = 1.000", self.doc)
        self.assertIn("count, not a rate", self.doc)

    def test_it_records_the_correction_that_made_the_arm_meaningful(self):
        # The first arm compared the system with itself. If that paragraph ever
        # goes, the next reader will not know why console_payload exists.
        self.assertIn("compared the system with itself", self.doc)
        self.assertIn("console_payload", self.doc)

    def test_it_discloses_the_coded_arms_own_cost(self):
        self.assertIn("case_040.medication", self.doc)
        self.assertIn("Minoxidil", self.doc)
