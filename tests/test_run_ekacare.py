"""The batch loop and the gold-v1 seal. Temporary directories and a fake
OpenRouter client only: no network, no spend, nothing written to the repo."""

import hashlib
import io
import json
import stat
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import httpx2
import openai

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "evals"))

import extract  # noqa: E402
import label_gold_v1  # noqa: E402
import run_ekacare  # noqa: E402
from budget import SpendLedger  # noqa: E402
from schema import GoldCase, GoldField, GoldSet, Status  # noqa: E402

CASE_001 = (ROOT / "gold" / "case_001.txt").read_text()
CASE_002 = (ROOT / "gold" / "case_002_traps.txt").read_text()


def field(status, value=None, evidence=None):
    return GoldField(status=Status(status), value=value, evidence=evidence)


NOT_STATED = {name: field("not_stated") for name in ("medication", "dose", "frequency", "allergy")}
LABELS = {
    "case_001": (CASE_001, {
        "medication": field("found", "Metformin", "metformin"),
        "dose": field("found", "500 mg", "500 mg"),
        "frequency": field("found", "twice daily", "po bid"),
        "allergy": field("not_stated"),
    }),
    "case_002": (CASE_002, {**NOT_STATED, "allergy": field("unsure", "Penicillin", "rash to penicillin")}),
}


def response(fields: dict) -> str:
    blank = {"evidence": "", "value": "", "status": "not_stated"}
    return json.dumps({n: fields.get(n, blank) for n in ("medication", "dose", "frequency", "allergy")})


RESPONSES = {
    "case_001": response({
        "medication": {"evidence": "metformin", "value": "Metformin", "status": "found"},
        "dose": {"evidence": "500 mg", "value": "500 mg", "status": "found"},
        "frequency": {"evidence": "po bid", "value": "twice daily", "status": "found"},
    }),
    "case_002": response({}),
}


def completion(content, cost=0.00055):
    return SimpleNamespace(
        id="gen-test", model="google/gemini-2.5-flash", model_extra={"provider": "Google AI Studio"},
        choices=[SimpleNamespace(message=SimpleNamespace(content=content), finish_reason="stop")],
        usage=SimpleNamespace(prompt_tokens=1000, completion_tokens=100,
                              completion_tokens_details=SimpleNamespace(reasoning_tokens=0),
                              model_extra={"cost": cost}),
    )


class FakeClient:
    """Answers each create() with the next item (a completion or an exception)."""

    def __init__(self, items):
        self.calls = 0
        outer = self

        class Completions:
            def create(self, **kwargs):
                outer.calls += 1
                item = items.pop(0)
                if isinstance(item, Exception):
                    raise item
                return item

        self.chat = SimpleNamespace(completions=Completions())


def timeout():
    return openai.APITimeoutError(request=httpx2.Request("POST", "https://openrouter.ai/api/v1"))


class Batch(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.tmp = Path(self.dir.name)
        self.sealed = self.tmp / "gold_v1.json"
        self.sha = self.tmp / "gold_v1.sha256"

    def tearDown(self):
        self.dir.cleanup()

    def seal(self, labels=LABELS):
        cases = [GoldCase(case_id=cid, source_text=text, ground_truth=gt)
                 for cid, (text, gt) in labels.items()]
        goldset = GoldSet(provenance={
            "dataset": run_ekacare.DATASET,
            "revision": "662c58a1d03255e26461519be4c9c4e597fdd3fd",
            "row_map": {cid: {"text_md5": hashlib.md5(text.encode()).hexdigest()}
                        for cid, (text, _) in labels.items()},
        }, cases=cases)
        with redirect_stdout(io.StringIO()):
            label_gold_v1.seal_goldset(goldset, self.sealed, self.sha)

    def config(self, **overrides):
        settings = dict(
            gold_path=self.sealed, run_id="t1", ledger_path=self.tmp / "ledger.json",
            results_dir=self.tmp / "results", cache_dir=self.tmp / "cache",
            sealed_path=self.sealed, sealed_sha_path=self.sha,
        )
        settings.update(overrides)
        return run_ekacare.BatchConfig(**settings)

    def run_batch(self, cfg, client):
        with redirect_stderr(io.StringIO()):
            return run_ekacare.run_batch(cfg, client=client)

    def fake(self):
        return FakeClient([completion(RESPONSES[cid]) for cid in LABELS])

    def test_live_run_scores_against_the_sealed_gold(self):
        self.seal()
        summary, code = self.run_batch(self.config(), self.fake())
        self.assertEqual((code, summary["status"], summary["reportable"]), (0, "done", True))
        pooled = summary["scores"]["pooled"]
        self.assertEqual((pooled["gold_found"], pooled["correct"], pooled["recall"]), (3, 3, 1.0))
        self.assertEqual(pooled["silent_failure_rate"], 0.0)
        self.assertAlmostEqual(SpendLedger(self.tmp / "ledger.json").spent(), 2 * 0.00055)
        out = self.tmp / "results" / "t1"
        for name in ("manifest.json", "gated.jsonl", "ungated.jsonl", "scores.json", "rows.csv"):
            self.assertTrue((out / name).is_file(), name)
        manifest = json.loads((out / "manifest.json").read_text())
        self.assertEqual(manifest["gold"]["sha256"], hashlib.sha256(self.sealed.read_bytes()).hexdigest())

    def test_resume_never_pays_twice(self):
        self.seal()
        self.run_batch(self.config(), self.fake())
        again = FakeClient([])  # any call would raise IndexError
        summary, code = self.run_batch(self.config(), again)
        self.assertEqual((code, again.calls), (0, 0))
        manifest = json.loads((self.tmp / "results" / "t1" / "manifest.json").read_text())
        self.assertEqual(manifest["cached_cases"], 2)

    def test_cache_from_another_prompt_is_refused(self):
        self.seal()
        self.run_batch(self.config(), self.fake())
        with mock.patch.object(extract, "prompt_fingerprint", return_value="ffffffffffffffff"):
            with self.assertRaises(run_ekacare.Refused) as caught:
                self.run_batch(self.config(experiment="exp-2"), self.fake())
        self.assertEqual(caught.exception.code, "ERR_CACHE_MISMATCH")

    def test_changed_prompt_needs_an_experiment_name(self):
        self.seal()
        with mock.patch.object(extract, "prompt_fingerprint", return_value="ffffffffffffffff"):
            with self.assertRaises(run_ekacare.Refused) as caught:
                self.run_batch(self.config(), self.fake())
            self.assertIn("prompt changed", caught.exception.message)
            summary, code = self.run_batch(self.config(experiment="exp-2", run_id="t2"), self.fake())
        self.assertEqual((code, summary["experiment"]), (0, "exp-2"))

    def test_edited_gold_is_refused(self):
        self.seal()
        self.sealed.chmod(0o644)
        data = json.loads(self.sealed.read_text())
        data["cases"][0]["ground_truth"]["dose"]["value"] = "50 mg"
        self.sealed.write_text(json.dumps(data))
        with self.assertRaises(run_ekacare.Refused) as caught:
            self.run_batch(self.config(), self.fake())
        self.assertIn("edited after sealing", caught.exception.message)

    def test_unsealed_template_is_refused_in_live_mode(self):
        cfg = self.config(gold_path=ROOT / "data" / "gold_labels" / "gold_v1_template.json")
        with self.assertRaises(run_ekacare.Refused) as caught:
            self.run_batch(cfg, FakeClient([]))
        self.assertEqual((caught.exception.code, caught.exception.exit_code), ("ERR_GOLD_REFUSED", 2))

    def test_budget_is_checked_before_any_call(self):
        self.seal()
        client = FakeClient([])
        with self.assertRaises(run_ekacare.Refused) as caught:
            self.run_batch(self.config(budget_usd=0.0001), client)
        self.assertEqual((caught.exception.exit_code, client.calls), (5, 0))

    def test_breaker_halts_after_three_consecutive_failures(self):
        labels = {f"case_{i:03d}": (f"note {i}: start metformin 500 mg po bid", NOT_STATED)
                  for i in range(1, 6)}
        self.seal(labels)
        client = FakeClient([timeout() for _ in range(5)])
        summary, code = self.run_batch(self.config(), client)
        self.assertEqual((code, summary["status"], client.calls), (3, "halted", 3))
        self.assertFalse(summary["reportable"])
        self.assertFalse(summary["scored"])
        # Codes on stdout; the human-readable reason goes to stderr and the manifest.
        self.assertEqual(summary["breaker"]["reason_code"], "CONSECUTIVE_FAILURES")
        self.assertEqual(summary["unscored_code"], "HALTED")
        self.assertNotIn("reason", summary["breaker"])
        self.assertEqual(set(summary["error_codes"].values()), {"ERR_UPSTREAM_TIMEOUT"})

    def test_upstream_failures_are_not_counted_as_abstentions(self):
        self.seal()
        client = FakeClient([completion(RESPONSES["case_001"]), timeout()])
        summary, code = self.run_batch(self.config(), client)
        self.assertEqual(code, 0)
        self.assertEqual(summary["scores"]["errored_cases"], ["case_002"])
        self.assertEqual(summary["scores"]["pooled"]["wiped"], 0)
        self.assertEqual(summary["exit_counts"], {"0": 1, "3": 1})

    def test_an_anomalous_note_is_never_sent(self):
        labels = {**LABELS, "case_003": ("Plan: m\u0435tformin 500 mg po bid", NOT_STATED)}
        self.seal(labels)
        client = FakeClient([completion(RESPONSES["case_001"]), completion(RESPONSES["case_002"])])
        summary, code = self.run_batch(self.config(), client)
        self.assertEqual((code, client.calls), (0, 2))
        self.assertEqual(summary["scores"]["pooled"]["forced"], 4)
        # case_001 verified and case_002 all not stated exit 0; the
        # anomalous note is a safe abstention, exit 1.
        self.assertEqual(summary["exit_counts"], {"0": 2, "1": 1})

    def test_main_prints_exactly_one_json_document(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = run_ekacare.main([
                "--mock", "--gold", str(ROOT / "data" / "gold_labels" / "gold_v1_template.json"),
                "--limit", "1", "--run-id", "m1",
                "--results-dir", str(self.tmp / "results"), "--cache-dir", str(self.tmp / "cache")])
        summary = json.loads(out.getvalue())
        self.assertEqual((code, summary["mode"], summary["reportable"]), (0, "mock", False))
        self.assertEqual(summary["unscored_code"], "GOLD_INCOMPLETE")
        self.assertIn("run m1", err.getvalue())
        self.assertIn("not scored: GOLD_INCOMPLETE", err.getvalue())

    def test_a_refusal_prints_a_code_on_stdout_and_the_reason_on_stderr(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = run_ekacare.main([
                "--gold", str(ROOT / "data" / "gold_labels" / "gold_v1_template.json"),
                "--results-dir", str(self.tmp / "results"), "--cache-dir", str(self.tmp / "cache")])
        self.assertEqual((code, json.loads(out.getvalue())), (2, {"status": "refused", "code": "ERR_GOLD_REFUSED"}))
        self.assertIn("live runs score only the sealed", err.getvalue())


class Seal(unittest.TestCase):
    def test_seal_is_write_once_and_read_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            sealed, sha = Path(tmp) / "gold_v1.json", Path(tmp) / "gold_v1.sha256"
            goldset = GoldSet(provenance={"dataset": run_ekacare.DATASET}, cases=[
                GoldCase(case_id="c1", source_text="metformin", ground_truth=NOT_STATED)])
            label_gold_v1.seal_goldset(goldset, sealed, sha)
            for path in (sealed, sha):
                self.assertFalse(path.stat().st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))
            recorded = json.loads(sealed.read_text())["provenance"]
            self.assertEqual(recorded["prompt_fingerprint"], extract.prompt_fingerprint())
            self.assertEqual(sha.read_text().split()[0], hashlib.sha256(sealed.read_bytes()).hexdigest())
            with self.assertRaises(SystemExit):
                label_gold_v1.seal_goldset(goldset, sealed, sha)


if __name__ == "__main__":
    unittest.main()
