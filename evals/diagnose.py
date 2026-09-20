#!/usr/bin/env python3
"""Attribute a batch run's failures to root causes. Read-only, no model calls.

    ./.venv/bin/python evals/diagnose.py                       # newest run
    ./.venv/bin/python evals/diagnose.py evals/results/<run_id> > diagnosis.json

run_ekacare.py answers "how good is the system"; this answers "why is that
number what it is". It re-reads the cached gated/ungated payloads of a finished
run and sorts every field decision into one cause, then reports four
counter-factual arms.

The arms are DIAGNOSTICS, never the headline. scoring.value_matches() is the
pre-registered rule and it is not touched here: each arm relaxes one specific
convention so the cost of that convention can be read off, and each is labelled
with the convention it relaxes. A reader can therefore separate three things
the single recall number mixes together: a model that named the wrong fact, a
model that named the right fact in the wrong format, and a gold label that
contradicts its own field rule.

Gold labels that fail extract.verify_value() - the same gate the pipeline
applies to the model - are listed as gold-v2 candidates. gold_v1.json is never
written to; per CLAUDE.md 1.3 corrections belong in a new version.

stdout: one JSON document. stderr: the tables.
Exit codes: 0 report written; 2 run directory unusable; 6 internal error.
"""

import argparse
import hashlib
import json
import re
import statistics
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "evals"))

import extract  # noqa: E402
from extract import (  # noqa: E402
    CRITICAL_FIELDS, FREQUENCY_EQUIVALENTS, ExtractedField, Status, _norm_phrase,
    _quantities, _tokens, verify_value,
)
from scoring import DOSAGE_FORMS, value_matches, wilson  # noqa: E402

GOLD_DIR = ROOT / "data" / "gold_labels"
SEALED_PATH = GOLD_DIR / "gold_v1.json"
SEALED_SHA_PATH = GOLD_DIR / "gold_v1.sha256"
RESULTS_DIR = ROOT / "evals" / "results"

EXIT_OK = 0
EXIT_UNUSABLE = 2
EXIT_INTERNAL = 6

CAUSES = {
    "CORRECT": "gold found, gate proposed it, values match",
    "CORRECT_NEGATIVE": "gold not_stated and the field was not proposed",
    "MED_STRENGTH_APPENDED": "same drug as gold, with the strength appended to the name",
    "MED_DIFFERENT_DRUG": "a different drug from the same note",
    "MED_MODEL_SILENT": "gold named a drug, the model said not_stated",
    "MED_GOLD_SILENT": "gold says not_stated, the model named a drug",
    "DOSE_GOLD_NOT_A_STRENGTH": "gold dose carries no numeric strength (its own rule says not_stated)",
    "DOSE_OTHER_DRUG": "a strength that belongs to a different drug in the note",
    "DOSE_MODEL_SILENT": "gold had a strength, the model said not_stated",
    "DOSE_GATE_BLANKED": "a gate blanked a dose gold had",
    "DOSE_GOLD_SILENT": "gold says not_stated, the model proposed a strength",
    "FREQ_PHRASE_LENGTH": "same interval, one phrase contains the other",
    "FREQ_DIFFERENT_REGIMEN": "a different regimen",
    "FREQ_MODEL_SILENT": "gold had a frequency, the model said not_stated",
    "FREQ_GOLD_SILENT": "gold says not_stated, the model proposed a frequency",
    "ALLERGY_MISMATCH": "allergy disagreement",
}

# Arm 1. FIELD_RULES tells the model that 'Dolo 650' is a medication name and
# that the 650 in 'Dolo 650' is the dose. Both cannot hold. This arm ignores
# strength tokens inside a medication value.
_NUMBER = re.compile(r"\d+(?:\.\d+)?")


def medication_core(text: str) -> set[str]:
    return {t for t in _tokens(text) if not _NUMBER.fullmatch(t)} - DOSAGE_FORMS


# Arm 2. The frequency field rule says timing relative to food is not a
# frequency, and the dose rule says a quantity per administration is not a
# dose. This arm holds both sides of the comparison to those two sentences.
_MEAL = r"(?:food|meals?|breakfast|lunch|dinner)"
FOOD_OR_DURATION = re.compile(
    rf"\b(?:after|before)\s+{_MEAL}(?:\s*(?:,|and)\s*{_MEAL})*\b"
    r"|\bfor\s+\d+\s+(?:day|days|week|weeks)\b"
    r"|\bempty\s+stomach\b",
    re.I,
)
LEADING_QUANTITY = re.compile(r"^(?:one|two|three|four|1|2|3|4|a)\s+(?=\w)", re.I)

# A frequency has to say something about time. Used only to audit the labels.
TEMPORAL = re.compile(
    r"\b(?:daily|day|days|night|nightly|morning|noon|afternoon|evening|bedtime|"
    r"times?|hourly|hours?|weekly|week|month|od|bd|bid|tds|tid|qid|qds|hs|sos|prn|"
    r"stat|alternate|once|twice|thrice|every)\b|\d-\d",
    re.I,
)


def strip_food_and_duration(text: str) -> str:
    """The frequency with the parts its own field rule excludes removed."""
    return re.sub(r"\s+", " ", FOOD_OR_DURATION.sub(" ", text or "")).strip(" .,;")


def frequency_core(text: str) -> str:
    core = _norm_phrase(strip_food_and_duration(text))
    # "one at night" and "at night" are the same instruction. "two times a day"
    # and "three times a day" are not, so a count in front of "times" stays.
    if "time" not in core:
        core = LEADING_QUANTITY.sub("", core)
    return re.sub(r"\s+", " ", core).strip()


def frequency_matches_rule(predicted: str, gold: str) -> bool:
    p, g = frequency_core(predicted), frequency_core(gold)
    if not p or not g:
        return False
    return p == g or any(p in group and g in group for group in FREQUENCY_EQUIVALENTS)


def load_jsonl(path: Path) -> dict:
    return {rec["case_id"]: rec for rec in
            (json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line)}


def gold_audit(cases: list[dict]) -> list[dict]:
    """Gold labels that contradict a rule the gold set itself states.

    Four deterministic checks, in the order they were written:
      1. extract.verify_value rejects the (value, evidence) pair - the same
         gate the pipeline applies to the model;
      2. a frequency carrying food timing or a duration, which
         FIELD_RULES['frequency'] excludes;
      3. a frequency with no temporal marker at all;
      4. a dose naming a dosage form ('1 tablet'), which
         FIELD_RULES['dose'] calls a quantity and not a dose.

    Nothing here writes to gold_v1.json: the list is the input to a gold-v2
    labelling pass (CLAUDE.md 1.3), and every row waits on the labeller.
    """
    rows = []
    for case in cases:
        for field in CRITICAL_FIELDS:
            truth = case["ground_truth"][field]
            if truth["status"] != "found":
                continue
            value, evidence = truth["value"] or "", truth["evidence"] or ""
            probe = ExtractedField(evidence=evidence, value=value, status=Status.FOUND)
            result = verify_value(field, probe)
            found = []
            if result is not None:
                found.append((result.code, _rule_hint(field, result.code),
                              "not_stated" if field == "dose" else "review"))
            if field == "frequency" and FOOD_OR_DURATION.search(value):
                found.append((
                    "GOLD_FREQUENCY_CARRIES_FOOD_OR_DURATION",
                    "FIELD_RULES['frequency']: timing relative to food is not a frequency",
                    strip_food_and_duration(value) or "review"))
            if field == "frequency" and value and not TEMPORAL.search(_norm_phrase(value)):
                found.append((
                    "GOLD_FREQUENCY_HAS_NO_TEMPORAL_MARKER",
                    "a frequency value that names no interval cannot be a frequency",
                    "review"))
            if field == "dose" and set(_tokens(value)) & DOSAGE_FORMS:
                found.append((
                    "GOLD_DOSE_IS_A_COUNT",
                    "FIELD_RULES['dose']: a quantity per administration is NOT a dose",
                    "not_stated"))
            for code, rule, suggestion in found:
                rows.append({
                    "case_id": case["case_id"], "field": field, "check": code,
                    "gold_value": truth["value"], "gold_evidence": truth["evidence"],
                    "rule_violated": rule, "suggested": suggestion, "confirmed_by": None,
                })
    return rows


def defect_keys(audit: list[dict]) -> set:
    """The (case_id, field) pairs arm D3 removes from the denominator."""
    return {(row["case_id"], row["field"]) for row in audit}


def attribute(case: dict, field: str, gated: dict, ungated: dict,
              drug_disagreed: bool) -> str:
    truth = case["ground_truth"][field]
    gold_value = truth["value"] or ""
    gold_found = truth["status"] == "found"
    code = {g["field"]: g["code"] for g in gated["gate"]}[field]
    value = gated["extraction"][field]["value"] or ""
    raw = ungated["extraction"][field]["value"] or ""
    proposed = code == "VERIFIED" or code.startswith("REVIEW")

    if gold_found and proposed and value_matches(field, value, gold_value):
        return "CORRECT"
    if not gold_found and not proposed:
        return "CORRECT_NEGATIVE"

    if field == "medication":
        if not gold_found:
            return "MED_GOLD_SILENT"
        if not proposed:
            return "MED_MODEL_SILENT"
        if medication_core(value) and medication_core(value) == medication_core(gold_value):
            return "MED_STRENGTH_APPENDED"
        return "MED_DIFFERENT_DRUG"

    if field == "dose":
        if gold_found and not _quantities(gold_value):
            return "DOSE_GOLD_NOT_A_STRENGTH"
        if not gold_found:
            return "DOSE_GOLD_SILENT"
        if code.startswith("ABSTAIN") and raw:
            return "DOSE_GATE_BLANKED"
        if drug_disagreed:
            return "DOSE_OTHER_DRUG"
        return "DOSE_MODEL_SILENT" if not proposed else "DOSE_OTHER_DRUG"

    if field == "frequency":
        if not gold_found:
            return "FREQ_GOLD_SILENT"
        if not proposed:
            return "FREQ_MODEL_SILENT"
        p, g = _norm_phrase(value), _norm_phrase(gold_value)
        if p and g and (p in g or g in p):
            return "FREQ_PHRASE_LENGTH"
        return "FREQ_DIFFERENT_REGIMEN"

    return "ALLERGY_MISMATCH"


def recall_arm(cases: list[dict], gated: dict, defects: set, *,
               ignore_strength: bool = False, rule_faithful_frequency: bool = False,
               drop_defects: bool = False) -> dict:
    """Recall and the silent-failure rate under one relaxation.

    With every flag off this reproduces scoring.score(), which is what the
    first arm is for: a diagnostic that cannot reproduce the pre-registered
    number is not measuring the same run.
    """
    counts = {f: {"correct": 0, "gold_found": 0, "verified": 0, "silent": 0}
              for f in CRITICAL_FIELDS}
    for case in cases:
        run = gated.get(case["case_id"])
        if run is None:
            continue
        codes = {g["field"]: g["code"] for g in run["gate"]}
        for field in CRITICAL_FIELDS:
            if drop_defects and (case["case_id"], field) in defects:
                continue
            truth = case["ground_truth"][field]
            gold_value = truth["value"] or ""
            gold_found = truth["status"] == "found"
            code = codes[field]
            counts[field]["gold_found"] += gold_found
            value = run["extraction"][field]["value"] or ""
            right = gold_found and value_matches(field, value, gold_value)
            if not right and gold_found and ignore_strength and field == "medication":
                right = bool(medication_core(value)) and \
                    medication_core(value) == medication_core(gold_value)
            if not right and gold_found and rule_faithful_frequency and field == "frequency":
                right = frequency_matches_rule(value, gold_value)
            if code == "VERIFIED" or code.startswith("REVIEW"):
                counts[field]["correct"] += right
            if code == "VERIFIED":
                counts[field]["verified"] += 1
                counts[field]["silent"] += not right
    correct = sum(c["correct"] for c in counts.values())
    found = sum(c["gold_found"] for c in counts.values())
    verified = sum(c["verified"] for c in counts.values())
    silent = sum(c["silent"] for c in counts.values())
    low, high = wilson(correct, found)
    return {
        "correct": correct, "gold_found": found, "verified": verified, "silent": silent,
        "recall": round(correct / found, 4) if found else None,
        "recall_ci95": [low, high],
        "silent_failure_rate": round(silent / verified, 4) if verified else None,
        "per_field": {f: {**c, "recall": round(c["correct"] / c["gold_found"], 4)
                          if c["gold_found"] else None}
                      for f, c in counts.items()},
    }


def diagnose(run_dir: Path, gold_path: Path) -> dict:
    gold = json.loads(gold_path.read_text(encoding="utf-8"))
    cases = gold["cases"]
    gated = load_jsonl(run_dir / "gated.jsonl")
    ungated = load_jsonl(run_dir / "ungated.jsonl")
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    audit = gold_audit(cases)
    defects = defect_keys(audit)

    disagreed = set()
    for case in cases:
        case_id = case["case_id"]
        if case_id not in gated:
            continue
        truth = case["ground_truth"]["medication"]
        if truth["status"] != "found":
            continue
        value = gated[case_id]["extraction"]["medication"]["value"] or ""
        gold_value = truth["value"] or ""
        if value and not value_matches("medication", value, gold_value) and \
                medication_core(value) != medication_core(gold_value):
            disagreed.add(case_id)

    causes, by_field, examples = {}, {f: {} for f in CRITICAL_FIELDS}, {}
    for case in cases:
        case_id = case["case_id"]
        if case_id not in gated or case_id not in ungated:
            continue
        for field in CRITICAL_FIELDS:
            cause = attribute(case, field, gated[case_id], ungated[case_id],
                              case_id in disagreed)
            causes[cause] = causes.get(cause, 0) + 1
            by_field[field][cause] = by_field[field].get(cause, 0) + 1
            if not cause.startswith("CORRECT"):
                examples.setdefault(cause, []).append({
                    "case_id": case_id, "field": field,
                    "gold": case["ground_truth"][field]["value"],
                    "extracted": gated[case_id]["extraction"][field]["value"],
                    "model_said": ungated[case_id]["extraction"][field]["value"],
                })

    arms = [
        ("pre-registered (the headline, unchanged)", {}),
        ("D1 medication matched without its strength token", {"ignore_strength": True}),
        ("D2 frequency matched by the gold set's own field rule",
         {"rule_faithful_frequency": True}),
        ("D3 gold fields the pipeline's value gate rejects removed",
         {"drop_defects": True}),
        ("D4 D1 + D2 + D3 together",
         {"ignore_strength": True, "rule_faithful_frequency": True, "drop_defects": True}),
    ]
    arm_rows = [{"arm": name, "relaxes": sorted(kw), **recall_arm(cases, gated, defects, **kw)}
                for name, kw in arms]

    split = {}
    for label, wanted in (("drug choice disagreed", True), ("drug choice agreed", False)):
        subset = [c for c in cases if (c["case_id"] in disagreed) == wanted]
        split[label] = {"cases": len(subset),
                        **recall_arm(subset, gated, defects, ignore_strength=True,
                                     rule_faithful_frequency=True, drop_defects=True)}

    served = [g for g in gated.values() if g.get("provenance", {}).get("called")]
    api = [g["latency_ms"]["api"] for g in served] or [0.0]
    return {
        "run": {
            "run_id": manifest.get("run_id"), "model": manifest.get("model"),
            "mode": manifest.get("mode"), "experiment": manifest.get("experiment"),
            "cases_scored": len(gated),
            "run_prompt_fingerprint": manifest.get("prompt_fingerprint"),
            "sealed_prompt_fingerprint": manifest.get("gold", {}).get("sealed_prompt_fingerprint"),
            "current_prompt_fingerprint": extract.prompt_fingerprint(),
            "gold_path": _rel(gold_path),
            "gold_sha256_matches_seal": _seal_ok(gold_path),
        },
        "operations": {
            "calls": len(served),
            "api_ms_p50": round(statistics.median(api), 1),
            "api_ms_p95": round(sorted(api)[min(len(api) - 1, int(0.95 * len(api)))], 1),
            "api_ms_max": round(max(api), 1),
            "within_latency_budget": sum(bool(g.get("within_budget")) for g in served),
            "billed_usd": round(sum(g["usage"]["billed_usd"] for g in served), 6),
            "retried_calls": sum(g["usage"]["attempts"] > 1 for g in served),
            "input_scan_flags": sum(bool(g["input_scan"]["flags"]) for g in served),
        },
        "causes": dict(sorted(causes.items(), key=lambda kv: -kv[1])),
        "causes_by_field": by_field,
        "cause_legend": CAUSES,
        "examples": examples,
        "arms": arm_rows,
        "agreement_split": split,
        "gold_v2_candidates": audit,
    }


def _rule_hint(field: str, code: str) -> str:
    if field == "dose" and code == "ABSTAIN_NUMERIC_MISMATCH":
        return ("FIELD_RULES['dose']: a quantity per administration is NOT a dose; "
                "if only a quantity is given, dose is not_stated")
    if code == "ABSTAIN_VALUE_UNGROUNDED":
        return "the labelled value is not supported by the labelled evidence span"
    return f"extract.verify_value rejected it: {code}"


def _seal_ok(gold_path: Path) -> bool | None:
    if not SEALED_SHA_PATH.exists() or gold_path.resolve() != SEALED_PATH.resolve():
        return None
    digest = hashlib.sha256(gold_path.read_bytes()).hexdigest()
    return digest == SEALED_SHA_PATH.read_text(encoding="utf-8").split()[0]


def _rel(path: Path) -> str:
    """Repo-relative when it can be, absolute otherwise: a temporary directory
    is not under ROOT and printing a path must never raise."""
    try:
        return str(Path(path).resolve().relative_to(ROOT))
    except ValueError:
        return str(path)


def newest_run(results_dir: Path, exclude_modes: tuple = ("baseline",)) -> Path | None:
    """The most recently written run of the system, by modification time.

    Two corrections live here. Sorting by name worked while every run id began
    with a timestamp and broke as soon as the baseline arms appeared, because
    "baseline-pattern-all" sorts after "20260920T133717Z-...". And a baseline arm
    is not a run of the system - it has no model, no provenance and no cost - so
    it is not what "the newest run" should mean by default. Point any tool at a
    baseline directory explicitly to look at one.
    """
    runs = []
    for path in results_dir.glob("*"):
        manifest = path / "manifest.json"
        if not manifest.is_file():
            continue
        try:
            mode = json.loads(manifest.read_text(encoding="utf-8")).get("mode")
        except ValueError:
            mode = None
        if mode in exclude_modes:
            continue
        runs.append(path)
    if not runs:
        return None
    return max(runs, key=lambda p: (p / "manifest.json").stat().st_mtime)


def print_report(report: dict, out) -> None:
    run, ops = report["run"], report["operations"]
    print(f"\nrun {run['run_id']}  model {run['model']}  cases {run['cases_scored']}", file=out)
    print(f"prompt fingerprint: run {run['run_prompt_fingerprint']} | "
          f"sealed {run['sealed_prompt_fingerprint']} | now {run['current_prompt_fingerprint']}",
          file=out)
    print(f"gold {run['gold_path']} sha256 matches seal: {run['gold_sha256_matches_seal']}", file=out)
    print(f"{ops['calls']} calls  ${ops['billed_usd']:.6f}  api p50 {ops['api_ms_p50']:.0f} ms  "
          f"p95 {ops['api_ms_p95']:.0f} ms  max {ops['api_ms_max']:.0f} ms  "
          f"within budget {ops['within_latency_budget']}/{ops['calls']}", file=out)

    print("\nWHY (every field decision, one cause each)", file=out)
    for cause, n in report["causes"].items():
        print(f"  {n:4}  {cause:26} {report['cause_legend'].get(cause, '')}", file=out)

    print("\nDIAGNOSTIC ARMS - the headline is the first row; the rest are not scores", file=out)
    for arm in report["arms"]:
        per = " ".join(f"{f[:4]} {v['correct']}/{v['gold_found']}"
                       for f, v in arm["per_field"].items() if f != "allergy")
        recall, silent = arm["recall"], arm["silent_failure_rate"]
        print(f"  {arm['arm']:54} recall {arm['correct']:3}/{arm['gold_found']:3} "
              f"{recall if recall is None else format(recall, '.3f')} "
              f"[{arm['recall_ci95'][0]:.3f},{arm['recall_ci95'][1]:.3f}]  "
              f"silent {silent if silent is None else format(silent, '.3f')}  {per}", file=out)

    print("\nWHERE THE RESIDUAL GAP SITS (under arm D4)", file=out)
    for label, row in report["agreement_split"].items():
        recall = row["recall"]
        print(f"  {label:24} {row['cases']:3} notes  {row['correct']:3}/{row['gold_found']:3} "
              f"recall {recall if recall is None else format(recall, '.3f')}", file=out)

    candidates = report["gold_v2_candidates"]
    print(f"\nGOLD-V2 CANDIDATES ({len(candidates)} labels that contradict a rule the gold "
          "set states; gold_v1.json is not modified)", file=out)
    for row in candidates:
        print(f"  {row['case_id']} {row['field']:10} value={row['gold_value']!r:44} "
              f"{row['check']}", file=out)
    print("", file=out)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("run_dir", nargs="?", type=Path, default=None,
                        help="a directory under evals/results (default: the newest)")
    parser.add_argument("--gold", type=Path, default=SEALED_PATH, help="gold file to score against")
    parser.add_argument("--quiet", action="store_true", help="JSON on stdout only")
    parser.add_argument("--debug", action="store_true", help="traceback on an internal error")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        run_dir = args.run_dir or newest_run(RESULTS_DIR)
        if run_dir is None:
            print(f"no run found under {_rel(RESULTS_DIR)}", file=sys.stderr)
            return EXIT_UNUSABLE
        missing = [name for name in ("manifest.json", "gated.jsonl", "ungated.jsonl")
                   if not (run_dir / name).is_file()]
        if missing or not args.gold.is_file():
            print(f"{run_dir}: missing {missing or [str(args.gold)]}", file=sys.stderr)
            return EXIT_UNUSABLE
        report = diagnose(run_dir, args.gold)
    except Exception as exc:
        print(f"ERROR ERR_INTERNAL (exit {EXIT_INTERNAL}): {type(exc).__name__}", file=sys.stderr)
        if args.debug:
            traceback.print_exc()
        return EXIT_INTERNAL
    if not args.quiet:
        print_report(report, sys.stderr)
    sys.stdout.write(json.dumps(report, indent=2) + "\n")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
