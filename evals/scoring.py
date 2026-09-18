"""Score a batch run against the sealed gold set. Deterministic; no model.

Implements control S-09 of guardrails.md section 6.1. The unit is the field:
each gold case has four labelled fields, and each run produces a gated and
an ungated payload for the same cached model output.

  proposed   the field is shown with a value: VERIFIED or REVIEW
  correct    proposed, gold status `found`, and the values match under the
             pre-registered rules in value_matches()

  recall                correct / gold `found` fields            (headline)
  precision             correct / proposed
  abstention rate       wiped / (wiped + proposed)
  abstention precision  wiped fields whose UNGATED value was wrong / wiped
  silent-failure rate   VERIFIED fields that are wrong / VERIFIED  (target 0)

Cases whose payload is an error envelope are not scored: they are counted
in `errored_cases`, so an upstream failure can never pose as an abstention.
Fields wiped because the note carried hidden or look-alike characters
(ABSTAIN_ENCODING_ANOMALY) never reached a model: they count as misses for
recall, but in `forced`, not `wiped`, so abstention rate and precision
measure only the gates' decisions on real model output.
"""

import csv
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from guardrails import FREQUENCY_EQUIVALENTS, _norm_phrase, _quantities, _tokens  # noqa: E402
from schema import CRITICAL_FIELDS  # noqa: E402

# Pre-registered: dosage-form words do not decide whether two medication or
# allergy values name the same thing ("Tab Dolo 650" == "Dolo 650").
DOSAGE_FORMS = frozenset({
    "tab", "tabs", "tablet", "tablets", "cap", "caps", "capsule", "capsules",
    "syrup", "inj", "injection",
})

# A spreadsheet treats a cell starting with one of these as a formula.
FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")

COUNTERS = ("gold_found", "gold_unsure", "proposed", "correct", "verified", "silent",
            "wiped", "wiped_would_be_wrong", "near_miss", "review", "forced")
FORCED = "ABSTAIN_ENCODING_ANOMALY"


def csv_safe(cell) -> str:
    """Neutralise spreadsheet formulas in any exported cell (S-08)."""
    text = "" if cell is None else str(cell)
    return "'" + text if text.startswith(FORMULA_PREFIXES) else text


def wilson(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson score interval: honest at n = 67, where the normal
    approximation is not."""
    if n == 0:
        return (0.0, 1.0)
    p = successes / n
    denominator = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denominator
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denominator
    return (round(max(0.0, centre - half), 4), round(min(1.0, centre + half), 4))


def value_matches(field: str, predicted, gold) -> bool:
    """The pre-registered matching rules, fixed before any run."""
    if not predicted or not gold:
        return False
    if field == "dose":
        return set(_quantities(predicted)) == set(_quantities(gold))
    if field == "frequency":
        p, g = _norm_phrase(predicted), _norm_phrase(gold)
        return p == g or any(p in group and g in group for group in FREQUENCY_EQUIVALENTS)
    return set(_tokens(predicted)) - DOSAGE_FORMS == set(_tokens(gold)) - DOSAGE_FORMS


def _rates(c: dict) -> dict:
    def ratio(a, b):
        return round(a / b, 4) if b else None

    return {
        "recall": ratio(c["correct"], c["gold_found"]),
        "recall_ci95": wilson(c["correct"], c["gold_found"]),
        "precision": ratio(c["correct"], c["proposed"]),
        "precision_ci95": wilson(c["correct"], c["proposed"]),
        "abstention_rate": ratio(c["wiped"], c["wiped"] + c["proposed"]),
        "abstention_precision": ratio(c["wiped_would_be_wrong"], c["wiped"]),
        "gate_false_positive_rate": (
            round(1 - c["wiped_would_be_wrong"] / c["wiped"], 4) if c["wiped"] else None),
        "silent_failure_rate": ratio(c["silent"], c["verified"]),
        "near_miss_share": ratio(c["near_miss"], c["wiped"]),
    }


def score(gold_cases: list[dict], gated: dict, ungated: dict,
          slices: dict[str, set[str]] | None = None) -> dict:
    """gold_cases: GoldSet.cases as dicts. gated / ungated: case_id -> the
    payload extract.py produced for that arm (both built from one cached
    call). slices: optional name -> set of case_ids (e.g. "negation")."""
    pooled = {k: 0 for k in COUNTERS}
    per_field = {f: {k: 0 for k in COUNTERS} for f in CRITICAL_FIELDS}
    per_slice = {name: {k: 0 for k in COUNTERS} for name in (slices or {})}
    rows, errored, scored = [], [], 0

    for case in gold_cases:
        case_id = case["case_id"]
        run, raw = gated.get(case_id), ungated.get(case_id)
        if not run or run.get("status") != "ok" or not raw or raw.get("status") != "ok":
            errored.append(case_id)
            continue
        scored += 1
        codes = {g["field"]: g["code"] for g in run["gate"]}
        buckets = [pooled] + [per_slice[n] for n, ids in (slices or {}).items() if case_id in ids]
        for field in CRITICAL_FIELDS:
            gold = case["ground_truth"][field]
            code = codes[field]
            value = run["extraction"][field]["value"]
            raw_value = raw["extraction"][field]["value"]
            gold_found = gold["status"] == "found"
            right = gold_found and value_matches(field, value, gold["value"])
            proposed = code == "VERIFIED" or code.startswith("REVIEW")
            forced = code == FORCED
            wiped = code.startswith("ABSTAIN") and not forced
            raw_wrong = not (gold_found and value_matches(field, raw_value, gold["value"]))
            for c in buckets + [per_field[field]]:
                c["gold_found"] += gold_found
                c["gold_unsure"] += gold["status"] == "unsure"
                c["proposed"] += proposed
                c["correct"] += proposed and right
                c["verified"] += code == "VERIFIED"
                c["silent"] += code == "VERIFIED" and not right
                c["review"] += code.startswith("REVIEW")
                c["wiped"] += wiped
                c["forced"] += forced
                c["near_miss"] += code == "ABSTAIN_NEAR_MISS"
                c["wiped_would_be_wrong"] += wiped and raw_wrong
            rows.append({
                "case_id": case_id, "field": field, "code": code,
                "gold_status": gold["status"], "gold_value": gold["value"],
                "value": value, "ungated_value": raw_value,
                "correct": bool(proposed and right),
                "silent_failure": bool(code == "VERIFIED" and not right),
            })

    return {
        "cases": len(gold_cases),
        "scored_cases": scored,
        "errored_cases": errored,
        "coverage": round(scored / len(gold_cases), 4) if gold_cases else None,
        "pooled": {**pooled, **_rates(pooled)},
        "per_field": {f: {**c, **_rates(c)} for f, c in per_field.items()},
        "per_slice": {n: {**c, **_rates(c)} for n, c in per_slice.items()},
        "rows": rows,
    }


def write_rows_csv(path: Path, rows: list[dict]) -> None:
    """Per-field results for a spreadsheet, every cell neutralised."""
    columns = ["case_id", "field", "code", "gold_status", "gold_value", "value",
               "ungated_value", "correct", "silent_failure"]
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(columns)
        for row in rows:
            writer.writerow([csv_safe(row[c]) for c in columns])
