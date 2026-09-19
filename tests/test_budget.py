"""The spend ledger and ceiling. Temporary files only."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from extract import BudgetExceeded, SpendLedger, worst_case_cost  # noqa: E402


class WorstCase(unittest.TestCase):
    def test_overestimates_input_tokens(self):
        # 3,000 chars -> 1,000 tokens at the 3-chars-per-token floor.
        cost = worst_case_cost(3000, 1024, 0.30, 2.50)
        self.assertAlmostEqual(cost, 1000 / 1e6 * 0.30 + 1024 / 1e6 * 2.50)


class Ledger(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "cache" / "ledger.json"
        self.ledger = SpendLedger(self.path)

    def tearDown(self):
        self.dir.cleanup()

    def test_empty_ledger_has_spent_nothing(self):
        self.assertEqual(self.ledger.spent(), 0.0)

    def test_records_accumulate_per_model(self):
        self.ledger.record(0.0012, "gemini-2.5-flash")
        self.ledger.record(0.0008, "gemini-2.5-flash")
        data = json.loads(self.path.read_text())
        self.assertAlmostEqual(data["spent_usd"], 0.002)
        self.assertEqual(data["calls"], 2)
        self.assertAlmostEqual(data["by_model"]["gemini-2.5-flash"], 0.002)

    def test_ledger_holds_totals_only(self):
        self.ledger.record(0.001, "gemini-2.5-flash")
        keys = set(json.loads(self.path.read_text()))
        self.assertEqual(keys, {"spent_usd", "calls", "by_model", "updated_at"})

    def test_refuses_when_the_worst_case_would_cross_the_ceiling(self):
        self.ledger.record(7.999, "gemini-2.5-flash")
        with self.assertRaises(BudgetExceeded):
            self.ledger.ensure_room(0.003, 8.00)
        self.ledger.ensure_room(0.0009, 8.00)  # still room for this one

    def test_unreadable_ledger_fails_closed(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text("{not json")
        with self.assertRaises(BudgetExceeded):
            self.ledger.ensure_room(0.001, 8.00)


if __name__ == "__main__":
    unittest.main()
