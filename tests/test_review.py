"""The verification screen. Temporary directories only; nothing is fetched and
no page is served. These tests exist because this is the first HTML the project
ships, and a review screen that renders clinical text is an output-handling
surface (guardrails.md S-07, OWASP LLM10:2026)."""

import json
import re
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "evals"))
sys.path.insert(0, str(ROOT / "demo"))

import build_review  # noqa: E402

FIELDS = ("medication", "dose", "frequency", "allergy")

# A note that is hostile in two different ways at once: a script tag a model
# could be talked into echoing, and the corpus's own <PII> placeholder, which
# naive interpolation swallows without any error.
HOSTILE = ('Patient <PII> was given Tab Dolo 650 twice a day. '
           '<script>alert("xss")</script> Ampersand & angle > bracket.')


def truth(value=None):
    if value is None:
        return {"value": None, "evidence": None, "status": "not_stated"}
    return {"value": value, "evidence": value, "status": "found"}


def gold_case(case_id, text):
    return {"case_id": case_id, "source_text": text,
            "ground_truth": {f: truth() for f in FIELDS},
            "labeller": "test", "labelled_at": "2026-09-20T00:00:00+00:00", "notes": None}


def payload(case_id, verified=None, blanked=None):
    """A finished payload: `verified` fields pass, `blanked` fields are wiped."""
    verified, blanked = verified or {}, blanked or {}
    extraction, gate = {}, []
    for name in FIELDS:
        if name in verified:
            extraction[name] = {"evidence": verified[name], "value": verified[name],
                                "status": "found"}
            code, outcome = "VERIFIED", "pass"
        elif name in blanked:
            extraction[name] = {"evidence": None, "value": None, "status": "unsure"}
            code, outcome = "ABSTAIN_UNGROUNDED", "blanked"
        else:
            extraction[name] = {"evidence": None, "value": None, "status": "not_stated"}
            code, outcome = "NOT_STATED", "skipped"
        gate.append({"field": name, "outcome": outcome, "code": code, "near_miss": False})
    return {
        "case_id": case_id, "status": "ok", "schema_version": "mediextract.output.v3",
        "provenance": {"called": True, "served_model": "google/gemini-2.5-flash"},
        "latency_ms": {"api": 1200.0, "total": 0.5}, "latency_budget_ms": 3000,
        "within_budget": True, "input_scan": {"flags": [], "review_required": False},
        "usage": {"called": True, "billed_usd": 0.0006, "attempts": 1,
                  "input_tokens": 700, "output_tokens": 140},
        "extraction": extraction, "gate": gate,
    }


def write_run(directory: Path, cases, gated, ungated=None):
    run_dir = directory / "20260920T000000Z-test"
    run_dir.mkdir(parents=True)
    (run_dir / "gated.jsonl").write_text(
        "".join(json.dumps(p) + "\n" for p in gated), encoding="utf-8")
    if ungated is not None:
        (run_dir / "ungated.jsonl").write_text(
            "".join(json.dumps(p) + "\n" for p in ungated), encoding="utf-8")
    (run_dir / "manifest.json").write_text(json.dumps({
        "run_id": "20260920T000000Z-test", "model": "google/gemini-2.5-flash",
        "mode": "mock", "experiment": None, "prompt_fingerprint": "0" * 16,
    }), encoding="utf-8")
    gold_path = directory / "gold.json"
    gold_path.write_text(json.dumps(
        {"schema_version": "gold-v1", "provenance": {}, "cases": cases}), encoding="utf-8")
    return run_dir, gold_path


class Escaping(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        cases = [gold_case("case_001", HOSTILE)]
        gated = [payload("case_001", verified={"medication": "Dolo 650",
                                               "frequency": "twice a day"})]
        self.run_dir, self.gold = write_run(Path(self.tmp.name), cases, gated)
        self.page, _ = build_review.build(self.run_dir, self.gold)

    def test_a_script_tag_in_the_note_never_reaches_the_page_as_markup(self):
        self.assertNotIn("<script", self.page.lower())
        self.assertIn("&lt;script&gt;", self.page)

    def test_the_pii_placeholder_survives_as_visible_text(self):
        self.assertIn("&lt;PII&gt;", self.page)
        self.assertNotIn("<PII>", self.page)

    def test_ampersands_are_escaped_once_and_not_twice(self):
        self.assertIn("Ampersand &amp; angle &gt; bracket", self.page)
        self.assertNotIn("&amp;amp;", self.page)

    def test_the_page_carries_no_script_no_handler_and_no_remote_origin(self):
        self.assertIsNone(re.search(r"<script", self.page, re.I))
        self.assertIsNone(re.search(r"\son[a-z]+\s*=", self.page, re.I))
        self.assertIsNone(re.search(r"<form", self.page, re.I))
        self.assertIsNone(re.search(r"https?://", self.page))

    def test_the_content_security_policy_forbids_everything_by_default(self):
        match = re.search(r'Content-Security-Policy"\s*\n?\s*content="([^"]+)"', self.page)
        self.assertIsNotNone(match)
        self.assertIn("default-src 'none'", match.group(1))


class Highlighting(unittest.TestCase):
    def test_a_quote_is_wrapped_where_it_occurs(self):
        out = build_review.highlight("take Dolo 650 now", [(5, 13, "medication")])
        self.assertIn('<mark class="q-medication"', out)
        self.assertIn("Dolo 650</mark>", out)

    def test_overlapping_quotes_stay_balanced(self):
        # "650" sits inside "Dolo 650": the dose overlaps the medication.
        out = build_review.highlight("take Dolo 650 now",
                                     [(5, 13, "medication"), (10, 13, "dose")])
        self.assertEqual(out.count("<mark"), out.count("</mark>"))
        self.assertIn("q-dose q-medication", out)

    def test_a_span_outside_the_note_cannot_run_off_the_end(self):
        out = build_review.highlight("short", [(2, 999, "dose")])
        self.assertEqual(out.count("<mark"), out.count("</mark>"))
        self.assertIn("ort</mark>", out)

    def test_an_empty_note_produces_nothing(self):
        self.assertEqual(build_review.highlight("", [(0, 3, "dose")]), "")

    def test_a_quote_that_is_not_in_the_note_is_simply_not_highlighted(self):
        spans, counts = build_review.spans_for("take Dolo 650",
                                               {"dose": {"evidence": "999", "value": "999"}})
        self.assertEqual((spans, counts), ([], {}))


class WhatThePhysicianSees(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cases = [gold_case("case_001", "Tab Dolo 650 twice a day"),
                      gold_case("case_002", "No medication was prescribed")]
        self.gated = [payload("case_001", verified={"medication": "Dolo 650"},
                              blanked={"dose"}),
                      payload("case_002")]

    def build(self, with_ungated=False):
        ungated = None
        if with_ungated:
            raw = payload("case_001", verified={"medication": "Dolo 650", "dose": "999 mg"})
            ungated = [raw, payload("case_002")]
        run_dir, gold = write_run(Path(self.tmp.name), self.cases, self.gated, ungated)
        return build_review.build(run_dir, gold)

    def test_a_blanked_field_is_shown_as_a_blank(self):
        page, cases = self.build()
        self.assertEqual(cases, 2)
        self.assertIn("&mdash; blank &mdash;", page)
        self.assertIn("BLANK (Abstained: Ungrounded)", page)

    def test_without_the_ungated_arm_no_rejected_value_appears_anywhere(self):
        page, _ = self.build()
        self.assertNotIn("999 mg", page)
        self.assertNotIn("the gate refused it", page)

    def test_with_the_ungated_arm_the_rejected_value_is_labelled_as_such(self):
        page, _ = self.build(with_ungated=True)
        self.assertIn("999 mg", page)
        self.assertIn("engineering detail", page)

    def test_every_case_gets_one_radio_one_label_and_one_panel(self):
        page, cases = self.build()
        self.assertEqual(page.count('type="radio"'), cases)
        self.assertEqual(page.count("<label for="), cases)
        self.assertEqual(page.count('class="panel"'), cases)
        self.assertEqual(page.count(" checked>"), 1)

    def test_a_case_missing_from_gold_is_skipped_rather_than_guessed(self):
        run_dir, gold = write_run(Path(self.tmp.name), self.cases[:1], self.gated)
        page, cases = build_review.build(run_dir, gold)
        self.assertEqual(cases, 1)
        self.assertNotIn("case_002", page)


class CommandLine(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.run_dir, self.gold = write_run(
            Path(self.tmp.name), [gold_case("case_001", "Tab Dolo 650")],
            [payload("case_001", verified={"medication": "Dolo 650"})])
        self.out = Path(self.tmp.name) / "review.html"

    def invoke(self, *argv):
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = build_review.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_it_writes_the_file_and_prints_only_its_path(self):
        code, out, err = self.invoke(str(self.run_dir), "--gold", str(self.gold),
                                     "--out", str(self.out))
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), str(self.out))
        self.assertTrue(self.out.is_file())
        self.assertIn("1 cases", err)

    def test_a_missing_run_directory_exits_2_and_writes_nothing(self):
        code, out, _ = self.invoke(str(self.run_dir / "nope"), "--gold", str(self.gold),
                                   "--out", str(self.out))
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertFalse(self.out.exists())

    def test_it_never_writes_into_the_run_directory(self):
        before = sorted(p.name for p in self.run_dir.iterdir())
        self.invoke(str(self.run_dir), "--gold", str(self.gold), "--out", str(self.out))
        self.assertEqual(sorted(p.name for p in self.run_dir.iterdir()), before)


if __name__ == "__main__":
    unittest.main()
