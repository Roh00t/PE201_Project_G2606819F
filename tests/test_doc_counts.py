"""Documents may not quote a stale test count.

This exists because the same defect recurred three times. Every document that
tells a marker "N tests pass" hardcodes N in prose, and adding a test silently
falsifies every one of them at once. It was 350, then 377, then 407, then 444,
while `README.md`, `commands.md`, four files under `docs/` and the video
narration each drifted at their own pace - and `docs/pitch_deck.md` carried the
wrong figure in a *spoken* line, which would have been read aloud on camera over
a terminal printing something else.

`tests/test_guardrails_doc.py` already pins the metrics in `guardrails.md`
against their artefacts. This does the same job for the one number that is a
property of the suite itself, so it cannot be pinned to a file on disk and has to
be discovered at runtime.

The count is read by running discovery, not asserted, so this test is correct on
the run that adds the test that changes the count.
"""

import re
import unittest
from pathlib import Path
from unittest import TestLoader

ROOT = Path(__file__).resolve().parent.parent

# Every document that states a suite size to a reader.
DOCUMENTS = (
    "README.md",
    "commands.md",
    "CLAUDE.md",
    "guardrails.md",
    "project_proposal.md",
    "docs/final_report_executive_summary.md",
    "docs/tradeoff_analysis.md",
    "docs/forensic_audit.md",
    "docs/pitch_deck.md",
    "docs/lowcode_arm.md",
)

# "480 tests", "Ran 480 tests", "all 480 tests". Deliberately narrow: a figure
# like "15 checks" or "30 tests in tests/test_live_redteam.py" is about one file
# and is not this number.
QUOTED = re.compile(r"\b(\d{3,4})\s+tests\b")

# Spelled out, for narration that gets read aloud.
SPOKEN = re.compile(r"\b((?:one|two|three|four|five|six|seven|eight|nine)\s+hundred"
                    r"(?:\s+and\s+[a-z-]+)?)\s+tests\b", re.I)

WORDS = {
    0: "zero", 10: "ten", 20: "twenty", 30: "thirty", 40: "forty", 50: "fifty",
    60: "sixty", 70: "seventy", 80: "eighty", 90: "ninety",
    1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six", 7: "seven",
    8: "eight", 9: "nine", 11: "eleven", 12: "twelve", 13: "thirteen",
    14: "fourteen", 15: "fifteen", 16: "sixteen", 17: "seventeen",
    18: "eighteen", 19: "nineteen",
}


def spell(n: int) -> str:
    """480 -> 'four hundred and eighty'. Enough range for a test suite."""
    hundreds, rest = divmod(n, 100)
    out = f"{WORDS[hundreds]} hundred"
    if not rest:
        return out
    if rest in WORDS:
        return f"{out} and {WORDS[rest]}"
    tens, ones = divmod(rest, 10)
    return f"{out} and {WORDS[tens * 10]}-{WORDS[ones]}"


def live_count() -> int:
    """How many tests discovery finds, right now, in this process."""
    suite = TestLoader().discover(str(ROOT / "tests"), top_level_dir=str(ROOT / "tests"))
    return suite.countTestCases()


class QuotedTestCountsAreCurrent(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.count = live_count()

    def test_discovery_finds_a_plausible_number(self):
        # Guards against the whole check passing vacuously if discovery breaks.
        self.assertGreater(self.count, 100)

    def test_no_document_quotes_a_different_figure(self):
        wrong = []
        for name in DOCUMENTS:
            path = ROOT / name
            if not path.is_file():
                continue
            for match in QUOTED.finditer(path.read_text(encoding="utf-8")):
                if int(match.group(1)) != self.count:
                    line = path.read_text(encoding="utf-8")[:match.start()].count("\n") + 1
                    wrong.append(f"{name}:{line} says {match.group(1)}, "
                                 f"discovery finds {self.count}")
        self.assertEqual(wrong, [], "stale test counts:\n  " + "\n  ".join(wrong))

    def test_no_document_speaks_a_different_figure(self):
        # The video narration is read aloud over a terminal showing the real
        # number, so a spelled-out count drifting is worse than a written one.
        expected = spell(self.count)
        wrong = []
        for name in DOCUMENTS:
            path = ROOT / name
            if not path.is_file():
                continue
            for match in SPOKEN.finditer(path.read_text(encoding="utf-8")):
                said = match.group(1).lower().replace(" and ", " and ")
                if said != expected:
                    wrong.append(f"{name}: says {said!r}, expected {expected!r}")
        self.assertEqual(wrong, [], "stale spoken counts:\n  " + "\n  ".join(wrong))

    def test_spell_handles_the_shapes_a_suite_reaches(self):
        self.assertEqual(spell(480), "four hundred and eighty")
        self.assertEqual(spell(407), "four hundred and seven")
        self.assertEqual(spell(444), "four hundred and forty-four")
        self.assertEqual(spell(500), "five hundred")
        self.assertEqual(spell(315), "three hundred and fifteen")

    def test_at_least_one_document_actually_quotes_it(self):
        # If nothing quotes the count, the two checks above are vacuous.
        quoting = [name for name in DOCUMENTS
                   if (ROOT / name).is_file()
                   and QUOTED.search((ROOT / name).read_text(encoding="utf-8"))]
        self.assertGreater(len(quoting), 3, f"only {quoting} quote a test count")


if __name__ == "__main__":
    unittest.main()
