#!/usr/bin/env python3
"""Score any arm's output against a gold set, with the same rules as the model's.

    ./.venv/bin/python evals/score_arm.py evals/results/baseline-pattern-all
    ./.venv/bin/python evals/score_arm.py evals/results/<dir> --split report

An "arm" is anything that produced run-shaped payloads: the regex baseline
(`evals/baseline.py`), a low-code console run pasted back into the same shape, or
a second annotator. They are all scored by `evals/scoring.py` with the
pre-registered `value_matches` rules, so the comparison is between extractors
rather than between scorers. `run_ekacare.py` keeps doing this for the model; this
is the same tail end for everything else, and it re-uses that module's table
printer so the numbers line up column for column.

Every report carries the **majority-class baseline** - the score from answering
"not_stated" to everything - because a recall figure without it says nothing. On
the full 67-case gold set that baseline already agrees with gold on 113 of 268
field decisions, at recall 0.

`--split report` scores only the cases outside the tuning subsample recorded in
the arm's manifest, so a tuned extractor is not reported on the cases it was
tuned on.

stdout: one JSON summary. stderr: the table. Writes scores.json and rows.csv
into the arm's directory, and nothing else anywhere.
Exit codes: 0 scored; 2 inputs unusable; 6 internal error.
"""

import argparse
import json
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "evals"))

import run_ekacare  # noqa: E402  (its table printer, so columns match the model's report)
import scoring  # noqa: E402
from extract import CRITICAL_FIELDS  # noqa: E402

SEALED_PATH = ROOT / "data" / "gold_labels" / "gold_v1.json"

EXIT_OK = 0
EXIT_UNUSABLE = 2
EXIT_INTERNAL = 6


def _rel(path) -> str:
    try:
        return str(Path(path).resolve().relative_to(ROOT))
    except ValueError:
        return str(path)


def load_jsonl(path: Path) -> dict:
    return {rec["case_id"]: rec for rec in
            (json.loads(line) for line in
             path.read_text(encoding="utf-8").splitlines() if line)}


def majority_class(gold_cases: list[dict]) -> dict:
    """"Everything is not_stated": no recall, and it is still right a lot.

    The denominators are the same ones `scoring.score` uses, so this row can sit
    beside the real numbers without a footnote.
    """
    decisions = len(gold_cases) * len(CRITICAL_FIELDS)
    correct_negatives = sum(1 for case in gold_cases for field in CRITICAL_FIELDS
                            if case["ground_truth"][field]["status"] != "found")
    gold_found = decisions - correct_negatives
    return {
        "strategy": "answer not_stated to every field",
        "field_decisions": decisions,
        "agrees_with_gold": correct_negatives,
        "agreement": round(correct_negatives / decisions, 4) if decisions else None,
        "recall": 0.0,
        "gold_found": gold_found,
        "proposes_nothing": True,
    }


def score_arm(run_dir: Path, gold_path: Path, split: str) -> dict:
    gold = json.loads(gold_path.read_text(encoding="utf-8"))
    cases = gold["cases"]
    manifest_path = run_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else {}

    tuning = set((manifest.get("split") or {}).get("tuning_case_ids") or [])
    if split == "report" and not tuning:
        raise ValueError("this arm's manifest records no tuning subsample, so there is "
                         "nothing to hold out: score it with --split all and say so")
    if split == "tune":
        cases = [c for c in cases if c["case_id"] in tuning]
    elif split == "report":
        cases = [c for c in cases if c["case_id"] not in tuning]

    gated = load_jsonl(run_dir / "gated.jsonl")
    ungated_path = run_dir / "ungated.jsonl"
    ungated = load_jsonl(ungated_path) if ungated_path.is_file() else gated
    cases = [c for c in cases if c["case_id"] in gated]

    scores = scoring.score(cases, gated, ungated)
    # Scoring a run against a gold version other than its own must never
    # overwrite that run's own scores.json - re-scoring the gold-v1 run against
    # gold-v2 is a second measurement of the same outputs, not a replacement.
    recorded = (manifest.get("gold") or {}).get("path")
    own_gold = recorded and Path(recorded).name == gold_path.name
    suffix = "" if own_gold or not recorded else f"-vs-{gold_path.stem}"
    scoring.write_rows_csv(run_dir / f"rows{suffix}.csv", scores["rows"])
    stored = {k: v for k, v in scores.items() if k != "rows"}
    stored["scored_against"] = {"gold": _rel(gold_path), "run_own_gold": recorded,
                                "is_own_gold": bool(own_gold)}
    stored["majority_class_baseline"] = majority_class(cases)
    stored["split"] = {"which": split, "cases_scored": len(cases),
                       "held_out_from_tuning": sorted(tuning) if split == "report" else []}
    out_path = run_dir / f"scores{suffix}.json"
    out_path.write_text(json.dumps(stored, indent=2) + "\n", encoding="utf-8")

    summary = {
        "run_id": manifest.get("run_id", run_dir.name),
        "mode": manifest.get("mode", "arm"),
        "model": manifest.get("model"),
        "experiment": manifest.get("experiment"),
        "cases_run": len(cases),
        "exit_counts": manifest.get("exit_counts", {}),
        "billed_this_run_usd": (manifest.get("spend") or {}).get("billed_this_run_usd", 0.0),
        "gold": {"path": str(gold_path), "split": split, "wrote": str(out_path.name)},
        "majority_class_baseline": stored["majority_class_baseline"],
        "scored": True,
        "unscored_code": None,
        "scores": {"coverage": scores["coverage"], "errored_cases": scores["errored_cases"],
                   "pooled": scores["pooled"], "per_field": scores["per_field"]},
    }
    return summary, scores


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("run_dir", type=Path, help="a run-shaped directory")
    parser.add_argument("--gold", type=Path, default=SEALED_PATH)
    parser.add_argument("--split", choices=["tune", "report", "all"], default="all")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args(argv)
    try:
        missing = [name for name in ("gated.jsonl",) if not (args.run_dir / name).is_file()]
        if missing or not args.gold.is_file():
            print(f"{args.run_dir}: missing {missing or [str(args.gold)]}", file=sys.stderr)
            return EXIT_UNUSABLE
        summary, scores = score_arm(args.run_dir, args.gold, args.split)
    except ValueError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return EXIT_UNUSABLE
    except Exception as exc:
        print(f"ERROR ERR_INTERNAL (exit {EXIT_INTERNAL}): {type(exc).__name__}", file=sys.stderr)
        if args.debug:
            traceback.print_exc()
        return EXIT_INTERNAL
    if not args.quiet:
        run_ekacare.print_evaluation_table(summary, summary["scores"], sys.stderr)
        base = summary["majority_class_baseline"]
        print(f"majority class ({base['strategy']}): agrees with gold on "
              f"{base['agrees_with_gold']}/{base['field_decisions']} field decisions "
              f"({base['agreement']:.1%}) at recall 0.0", file=sys.stderr)
        print(f"split {args.split}: {summary['cases_run']} cases scored", file=sys.stderr)
    sys.stdout.write(json.dumps(summary, indent=2) + "\n")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
