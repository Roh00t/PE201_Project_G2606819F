"""The clinician-facing EHR view.

This page exists to be filmed, which makes it the most likely artefact in the
repository to drift into claiming something the system does not do. A hand-written
mockup of this screen was drafted first and did exactly that twice: it showed an
injection as `BLOCKED` with the field blanked, where the real behaviour retains
the value and downgrades it to review, and it showed an "attribution" check that
does not exist anywhere in the codebase.

So two of the classes below are about honesty rather than rendering:
`OnlyThePipelinesOwnVocabulary` forbids badge text that is not an
`extract.DISPLAY` value, and `NothingOnScreenThePayloadDidNotSay` forbids a gate
code the payload does not carry. Between them a future edit cannot put a
fictional control on camera.

The rest mirror `tests/test_review.py`, because the two pages share `esc`,
`highlight` and `spans_for`, and the inert-page guarantees have to hold for both.
"""

import json
import re
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "evals"))
sys.path.insert(0, str(ROOT / "demo"))

import build_ehr  # noqa: E402
from extract import CRITICAL_FIELDS, DISPLAY  # noqa: E402

PAYLOAD_DIR = ROOT / "demo" / "payloads"
HOSTILE = ('Patient <PII> reviewed. <script>alert("xss")</script> '
           "Ampersand & angle > bracket. Tablet Dolo 650 twice a day.")


def field(value=None, evidence=None, status="not_stated"):
    return {"value": value, "evidence": evidence, "status": status}


def payload(note_path: str, extraction=None, gate=None, status="ok", flags=None):
    extraction = extraction or {name: field() for name in CRITICAL_FIELDS}
    gate = gate or [{"field": name, "code": "NOT_STATED", "outcome": "pass"}
                    for name in CRITICAL_FIELDS]
    return {"status": status, "note": note_path, "model": "test",
            "extraction": extraction, "gate": gate,
            "input_scan": {"flags": flags or [], "review_required": bool(flags)},
            "latency_ms": {"api": 100.0, "local": 2.0, "total": 102.0},
            "within_budget": True, "usage": {}, "provenance": {"called": True}}


def write_case(directory: Path, stem: str, note: str, **kw):
    """One note file plus one payload naming it, as the CLI would leave them."""
    note_path = directory / f"{stem}.txt"
    note_path.write_text(note, encoding="utf-8")
    out = directory / f"{stem}.json"
    out.write_text(json.dumps(payload(str(note_path), **kw)), encoding="utf-8")
    return out


class Escaping(unittest.TestCase):
    """The dictation is untrusted clinical text and the corpus carries literal
    `<PII>` placeholders."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = TemporaryDirectory()
        path = write_case(
            Path(cls.tmp.name), "case_001", HOSTILE,
            extraction={"medication": field("Dolo 650", "Dolo 650", "found"),
                        "dose": field(), "frequency": field("twice a day", "twice a day", "found"),
                        "allergy": field()},
            gate=[{"field": "medication", "code": "VERIFIED"},
                  {"field": "dose", "code": "NOT_STATED"},
                  {"field": "frequency", "code": "VERIFIED"},
                  {"field": "allergy", "code": "NOT_STATED"}])
        cls.page, cls.cases = build_ehr.build([path])

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_a_script_tag_never_reaches_the_page_as_markup(self):
        self.assertNotIn("<script", self.page.lower())
        self.assertIn("&lt;script&gt;", self.page)

    def test_the_pii_placeholder_survives_as_visible_text(self):
        self.assertIn("&lt;PII&gt;", self.page)
        self.assertNotIn("<PII>", self.page)

    def test_ampersands_are_escaped_once_and_not_twice(self):
        self.assertIn("Ampersand &amp; angle &gt; bracket", self.page)
        self.assertNotIn("&amp;amp;", self.page)

    def test_the_page_carries_no_script_no_handler_and_no_remote_origin(self):
        for pattern in (r"<script", r"\son[a-z]+\s*=", r"<form", r"https?://"):
            with self.subTest(pattern=pattern):
                self.assertIsNone(re.search(pattern, self.page, re.I))

    def test_the_content_security_policy_forbids_everything_by_default(self):
        found = re.search(r'Content-Security-Policy"\s*\n?\s*content="([^"]+)"', self.page)
        self.assertIsNotNone(found)
        self.assertIn("default-src 'none'", found.group(1))

    def test_the_quote_is_highlighted_where_it_occurs(self):
        self.assertIn('<mark class="q-medication"', self.page)
        self.assertEqual(self.page.count("<mark"), self.page.count("</mark>"))


class OnlyThePipelinesOwnVocabulary(unittest.TestCase):
    """No badge on this page may describe a check the system does not perform."""

    def page_for(self, code, value=None, evidence=None):
        with TemporaryDirectory() as tmp:
            path = write_case(
                Path(tmp), "case_001", "Tablet Dolo 650 twice a day.",
                extraction={"medication": field(value, evidence,
                                                "found" if value else "not_stated"),
                            "dose": field(), "frequency": field(), "allergy": field()},
                gate=[{"field": "medication", "code": code}]
                     + [{"field": n, "code": "NOT_STATED"}
                        for n in CRITICAL_FIELDS if n != "medication"])
            return build_ehr.build([path])[0]

    def test_every_explanation_string_comes_from_extract_display(self):
        page = self.page_for("VERIFIED", "Dolo 650", "Dolo 650")
        wording = set(re.findall(r'class="fwhy">([^&]+) &mdash;', page))
        self.assertTrue(wording, "no explanation strings found - the check is vacuous")
        self.assertEqual(wording - {v for v in DISPLAY.values()}, set())

    def test_an_unknown_code_degrades_to_its_own_name_rather_than_inventing_one(self):
        page = self.page_for("ABSTAIN_ATTRIBUTION_CONFLICT")
        # There is no attribution gate. If someone adds the code without adding
        # the control, the page must show the bare code, never a reassuring
        # sentence a reader would take as a working check.
        self.assertIn("ABSTAIN_ATTRIBUTION_CONFLICT", page)
        self.assertNotIn("attribution", page.lower().replace(
            "abstain_attribution_conflict", ""))

    def test_the_four_chart_states_are_the_only_ones(self):
        self.assertEqual(sorted(build_ehr.STATE_LABEL), ["flagged", "held", "none", "ok"])

    def test_a_review_code_with_a_value_is_flagged_not_blanked(self):
        # The mockup showed an injection as blocked and blank. It is neither.
        self.assertEqual(build_ehr.state_for("REVIEW_INJECTION_PATTERN", blank=False), "flagged")
        self.assertEqual(build_ehr.state_for("REVIEW_INJECTION_PATTERN", blank=True), "held")

    def test_verified_and_not_stated_map_to_their_own_states(self):
        self.assertEqual(build_ehr.state_for("VERIFIED", blank=False), "ok")
        self.assertEqual(build_ehr.state_for("NOT_STATED", blank=True), "none")
        self.assertEqual(build_ehr.state_for("ABSTAIN_UNGROUNDED", blank=True), "held")


class NothingOnScreenThePayloadDidNotSay(unittest.TestCase):
    """Run against the committed demo payloads, which are what gets filmed."""

    @classmethod
    def setUpClass(cls):
        cls.paths = sorted(PAYLOAD_DIR.glob("*.json"))
        if not cls.paths:
            raise unittest.SkipTest("no demo payloads committed")
        cls.page, cls.cases = build_ehr.build(cls.paths)
        cls.payloads = [json.loads(p.read_text(encoding="utf-8")) for p in cls.paths]

    def test_every_case_renders(self):
        self.assertEqual(self.cases, len(self.paths))
        self.assertGreaterEqual(self.cases, 3)

    def test_every_value_on_the_page_is_a_value_the_pipeline_returned(self):
        checked = 0
        for d in self.payloads:
            for name in CRITICAL_FIELDS:
                value = (d["extraction"][name] or {}).get("value")
                if value:
                    self.assertIn(value, self.page, f"{name}={value!r}")
                    checked += 1
        self.assertGreater(checked, 5)

    def test_no_gate_code_appears_that_no_payload_carries(self):
        present = {g["code"] for d in self.payloads for g in d["gate"]}
        for code in DISPLAY:
            if code not in present:
                self.assertNotIn(code, self.page, f"{code} is on the page but in no payload")

    def test_the_injection_case_shows_its_values_rather_than_hiding_them(self):
        # The demonstrable finding: the model obeyed, the tripwire downgraded, and
        # the physician sees the planted value flagged rather than silently gone.
        injected = [d for d in self.payloads
                    if any(g["code"] == "REVIEW_INJECTION_PATTERN" for g in d["gate"])]
        self.assertTrue(injected, "no injection payload committed")
        for d in injected:
            for name in CRITICAL_FIELDS:
                got = d["extraction"][name] or {}
                code = {g["field"]: g["code"] for g in d["gate"]}[name]
                if code == "REVIEW_INJECTION_PATTERN":
                    self.assertTrue(got.get("value"), f"{name} should retain its value")
                    self.assertEqual(got.get("status"), "unsure")
                    self.assertIn(got["value"], self.page)

    def test_a_flagged_dictation_says_so_in_words(self):
        flagged = [d for d in self.payloads if (d.get("input_scan") or {}).get("flags")]
        self.assertTrue(flagged)
        self.assertIn("Input scan flagged this dictation", self.page)

    def test_the_synthetic_notice_is_present_and_unconditional(self):
        self.assertIn("Synthetic demonstration data", self.page)
        self.assertIn("not a real patient record", self.page)

    def test_no_real_organisation_is_named(self):
        for name in ("SingHealth", "NUHS", "NHG", "Mount Elizabeth", "Raffles Medical"):
            self.assertNotIn(name.lower(), self.page.lower())

    def test_one_radio_one_label_one_panel_per_case_and_one_default(self):
        self.assertEqual(self.page.count('type="radio"'), self.cases)
        self.assertEqual(self.page.count("<label for="), self.cases)
        self.assertEqual(self.page.count('class="panel"'), self.cases)
        self.assertEqual(self.page.count(" checked>"), 1)


class PairingIsRefusedRatherThanGuessed(unittest.TestCase):
    def test_a_payload_whose_note_file_is_missing_is_dropped(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "orphan.json"
            path.write_text(json.dumps(payload("/nonexistent/case_999.txt")),
                            encoding="utf-8")
            page, cases = build_ehr.build([path])
            self.assertEqual(cases, 0)
            self.assertNotIn("case_999", page)

    def test_a_batch_payload_is_dropped_because_its_note_key_is_a_case_id(self):
        # run_ekacare writes `"note": "case_001"`, which names no file. Rendering
        # it would mean pairing a payload with a dictation nobody verified.
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "batch.json"
            path.write_text(json.dumps(payload("case_001")), encoding="utf-8")
            self.assertEqual(build_ehr.build([path])[1], 0)

    def test_an_errored_payload_is_dropped(self):
        with TemporaryDirectory() as tmp:
            path = write_case(Path(tmp), "case_001", "note text", status="error")
            self.assertEqual(build_ehr.build([path])[1], 0)

    def test_note_text_resolves_a_repo_relative_path(self):
        got = build_ehr.note_text({"note": "gold/case_001.txt"})
        self.assertIsNotNone(got)
        self.assertGreater(len(got), 20)

    def test_note_text_returns_none_for_a_missing_or_absent_key(self):
        self.assertIsNone(build_ehr.note_text({}))
        self.assertIsNone(build_ehr.note_text({"note": "no/such/file.txt"}))


class CommandLine(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.out = Path(self.tmp.name) / "ehr.html"

    def tearDown(self):
        self.tmp.cleanup()

    def run_main(self, *argv):
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = build_ehr.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_it_writes_the_file_and_prints_only_its_path(self):
        path = write_case(Path(self.tmp.name), "case_001", "Tablet Dolo 650 twice a day.")
        code, out, err = self.run_main(str(path), "--out", str(self.out))
        self.assertEqual(code, build_ehr.EXIT_OK)
        self.assertEqual(out.strip(), str(self.out))
        self.assertTrue(self.out.is_file())
        self.assertIn("1 cases", err)

    def test_a_missing_payload_exits_2_and_writes_nothing(self):
        code, out, _ = self.run_main(str(Path(self.tmp.name) / "nope.json"),
                                     "--out", str(self.out))
        self.assertEqual(code, build_ehr.EXIT_UNUSABLE)
        self.assertEqual(out, "")
        self.assertFalse(self.out.exists())

    def test_a_payload_that_cannot_be_paired_exits_2_and_writes_nothing(self):
        path = Path(self.tmp.name) / "orphan.json"
        path.write_text(json.dumps(payload("/nonexistent/x.txt")), encoding="utf-8")
        code, out, err = self.run_main(str(path), "--out", str(self.out))
        self.assertEqual(code, build_ehr.EXIT_UNUSABLE)
        self.assertEqual(out, "")
        self.assertFalse(self.out.exists())
        self.assertIn("could be paired", err.replace("\n", " "))

    def test_it_never_writes_next_to_the_payloads(self):
        with TemporaryDirectory() as elsewhere:
            directory = Path(self.tmp.name)
            path = write_case(directory, "case_001", "note text")
            before = sorted(q.name for q in directory.iterdir())
            code, _, _ = self.run_main(str(path), "--out",
                                       str(Path(elsewhere) / "ehr.html"))
            self.assertEqual(code, build_ehr.EXIT_OK)
            self.assertEqual(sorted(q.name for q in directory.iterdir()), before)

    def test_the_default_payload_directory_is_the_committed_one(self):
        self.assertEqual(build_ehr.PAYLOAD_DIR, ROOT / "demo" / "payloads")
        self.assertEqual(build_ehr.DEFAULT_OUT, ROOT / "demo" / "ehr.html")


class ItSharesTheReviewScreensMachinery(unittest.TestCase):
    """Two escapers or two highlighters would be two things to get wrong."""

    def test_it_imports_rather_than_reimplements(self):
        source = (ROOT / "demo" / "build_ehr.py").read_text(encoding="utf-8")
        self.assertIn("from build_review import esc, highlight, spans_for", source)
        for name in ("def esc(", "def highlight(", "def spans_for("):
            self.assertEqual(source.count(name), 0, f"{name} is reimplemented here")

    def test_the_status_wording_is_imported_from_the_pipeline(self):
        source = (ROOT / "demo" / "build_ehr.py").read_text(encoding="utf-8")
        self.assertIn("from extract import BLANK, CRITICAL_FIELDS, DISPLAY", source)

    def test_it_cannot_overwrite_the_review_screen(self):
        # The two pages are siblings over the same payloads; neither may clobber
        # the other. Checked on the default rather than on the prose, because the
        # docstring mentions review.html on purpose.
        import build_review
        self.assertEqual(build_ehr.DEFAULT_OUT.name, "ehr.html")
        self.assertEqual(build_review.DEFAULT_OUT.name, "review.html")
        self.assertNotEqual(build_ehr.DEFAULT_OUT, build_review.DEFAULT_OUT)


if __name__ == "__main__":
    unittest.main()


class TheBannerDoesNotContradictTheChart(unittest.TestCase):
    """The synthetic banner sits beside the real dictation, and a viewer reads
    both in the same frame. The first draft said "Male, 65" next to a note
    beginning "Forty-two year old gentleman"."""

    @classmethod
    def setUpClass(cls):
        cls.paths = sorted(PAYLOAD_DIR.glob("*.json"))
        if not cls.paths:
            raise unittest.SkipTest("no demo payloads committed")

    def test_every_committed_case_has_its_own_banner(self):
        # Without this a new fixture renders as "Synthetic patient" and nobody
        # notices until it is on camera.
        for path in self.paths:
            stem = Path(json.loads(path.read_text(encoding="utf-8"))["note"]).stem
            self.assertIn(stem, build_ehr.PATIENTS, f"{stem} has no banner")

    def test_a_stated_age_in_the_note_matches_the_banner(self):
        words = {"twenty-eight": 28, "forty-two": 42, "fifty-eight": 58,
                 "sixty-five": 65, "seventy-five": 75}
        checked = 0
        for path in self.paths:
            d = json.loads(path.read_text(encoding="utf-8"))
            stem = Path(d["note"]).stem
            note = build_ehr.note_text(d).lower()
            for word, age in words.items():
                if f"{word} year old" not in note:
                    continue
                self.assertIn(str(age), build_ehr.PATIENTS[stem]["detail"],
                              f"{stem}: note says {word}, banner says "
                              f"{build_ehr.PATIENTS[stem]['detail']!r}")
                checked += 1
        self.assertGreater(checked, 1, "this test checked almost nothing")

    def test_the_footer_counts_the_payloads_rather_than_asserting_a_number(self):
        # The first version of this footer said "two of these three ran over the
        # budget". Regenerating the payloads made that false while the page went
        # on claiming it, so the sentence is now counted from the payloads and
        # this test holds for either outcome instead of skipping on one of them.
        page, _ = build_ehr.build(self.paths)
        payloads = [json.loads(p.read_text(encoding="utf-8")) for p in self.paths]
        over = sum(1 for d in payloads if d.get("within_budget") is False)
        if over:
            self.assertIn(f"{over} of these {len(payloads)} ran over", page)
        else:
            self.assertIn(f"all {len(payloads)} of these finished inside", page)
        self.assertIn("66 of 67 finish inside", page)

    def test_a_call_over_budget_is_flagged_on_its_own_chip(self):
        page, _ = build_ehr.build(self.paths)
        payloads = [json.loads(p.read_text(encoding="utf-8")) for p in self.paths]
        for d in payloads:
            if d.get("within_budget") is False:
                self.assertIn("over the 3,000 ms budget", page)
                return
        self.assertNotIn("over the 3,000 ms budget</span>", page)

    def test_the_latency_chip_names_the_clock_it_used(self):
        page, _ = build_ehr.build(self.paths)
        self.assertIn("model call", page)
        self.assertNotIn("end to end", page)
