#!/usr/bin/env python3
"""Turn a real low-code console run into payloads the project's scorer will grade.

    ./.venv/bin/python docs/lowcode_pack/convert_console_output.py \
        docs/lowcode_pack/console_results.json --out evals/results/lowcode-studio
    ./.venv/bin/python evals/score_arm.py evals/results/lowcode-studio \
        --gold data/gold_labels/gold_v2.json

`evals/lowcode_arm.py` builds a **proxy** low-code arm from the ungated half of an
existing run: the same model and prompt with the deterministic layer removed. That
is a faithful stand-in and it is not a measurement of a console, because the
payloads still enjoy this project's prompt engineering.

This script closes the gap. Paste `system_prompt.txt` and `response_schema.json`
into Google AI Studio, run the notes, collect the JSON, and this converts it into
run-shaped payloads scored by the **same pre-registered scorer** as every other
arm - so the comparison is between extractors rather than between scorers.

**Input format.** One JSON file, an object keyed by case id, each value being the
JSON the console returned for that note:

    {
      "case_001": {"medication": {"value": "...", "evidence": "...", "status": "found"},
                   "dose": {...}, "frequency": {...}, "allergy": {...}},
      "case_002": { ... }
    }

**What it does not do.** It does not gate, score, or repair. A field the console
left out becomes `not_stated` and is counted as such; a field carrying a value is
delivered as stated, exactly as a console delivers it. There is no span check,
because the absence of one is the thing being measured.

stdout: one JSON summary. stderr: what was written and what was skipped.
Exit codes: 0 converted; 2 inputs unusable; 6 internal error.
"""

import argparse
import json
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "src"))

from extract import CRITICAL_FIELDS, OUTPUT_SCHEMA_VERSION  # noqa: E402

EXIT_OK = 0
EXIT_UNUSABLE = 2
EXIT_INTERNAL = 6


def payload_for(case_id: str, returned: dict, model: str) -> dict:
    """One console answer as a run-shaped payload.

    The gate array says what a console reports, which is nothing: a field with a
    value is delivered as stated, a field without one is not stated. The scorer
    reads `proposed` and `verified` from these codes, so this is what makes the
    arm mean "no gate" rather than "gate reported but not applied" - the mistake
    `evals/lowcode_arm.py:console_payload` documents.
    """
    extraction = {}
    for name in CRITICAL_FIELDS:
        got = (returned or {}).get(name) or {}
        value = got.get("value") or None
        extraction[name] = {
            "value": value,
            "evidence": got.get("evidence") or None,
            "status": got.get("status") or ("found" if value else "not_stated"),
        }
    return {
        "case_id": case_id,
        "status": "ok",
        "schema_version": OUTPUT_SCHEMA_VERSION,
        "note": case_id,
        "model": model,
        "provenance": {"called": True, "requested_model": model,
                       "served_model": model, "via": "low-code console, by hand"},
        "gate_enabled": False,
        "lowcode_console": True,
        "latency_ms": {"api": None, "local": None, "total": None},
        "latency_budget_ms": None,
        "within_budget": None,
        "usage": {"called": True, "note": "not metered by the console"},
        "input_scan": {"flags": [], "review_required": False},
        "extraction": extraction,
        "gate": [{"field": name, "outcome": "pass", "near_miss": False,
                  "code": "VERIFIED" if extraction[name]["value"] else "NOT_STATED"}
                 for name in CRITICAL_FIELDS],
    }


def convert(results_path: Path, out: Path, model: str) -> dict:
    returned = json.loads(results_path.read_text(encoding="utf-8"))
    if not isinstance(returned, dict) or not returned:
        raise ValueError("expected a non-empty object keyed by case id")

    payloads, skipped = [], []
    for case_id, answer in sorted(returned.items()):
        if not isinstance(answer, dict):
            skipped.append(case_id)
            continue
        payloads.append(payload_for(case_id, answer, model))

    out.mkdir(parents=True, exist_ok=True)
    body = "".join(json.dumps(p) + "\n" for p in payloads)
    (out / "gated.jsonl").write_text(body, encoding="utf-8")
    # A console run has no second, un-gated copy; the arm is its own raw output.
    (out / "ungated.jsonl").write_text(body, encoding="utf-8")
    (out / "manifest.json").write_text(json.dumps({
        "run_id": out.name,
        "mode": "lowcode-console",
        "model": model,
        "experiment": "low-code console arm, run by hand",
        "cases_run": len(payloads),
        "source": str(results_path.name),
        "what_this_is": (
            "A real low-code console run, converted to run shape. No deterministic "
            "gate ran, so every value the console returned is delivered as stated."),
        "compare_against": "evals/results/gold-v2-live (the coded pipeline)",
        "spend": {"billed_this_run_usd": 0.0,
                  "note": "the console is not metered per call by this project"},
        "converted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }, indent=2) + "\n", encoding="utf-8")
    return {"built": out.name, "cases": len(payloads), "skipped": skipped}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("results", type=Path, help="the console's JSON, keyed by case id")
    parser.add_argument("--out", type=Path,
                        default=ROOT / "evals" / "results" / "lowcode-studio")
    parser.add_argument("--model", default="google/gemini-2.5-flash (AI Studio console)")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args(argv)

    if not args.results.is_file():
        print(f"REFUSED: {args.results} is not a file", file=sys.stderr)
        return EXIT_UNUSABLE
    try:
        summary = convert(args.results, args.out, args.model)
    except (ValueError, json.JSONDecodeError) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return EXIT_UNUSABLE
    except Exception as exc:
        print(f"ERROR ERR_INTERNAL (exit {EXIT_INTERNAL}): {type(exc).__name__}",
              file=sys.stderr)
        if args.debug:
            traceback.print_exc()
        return EXIT_INTERNAL

    print(f"wrote {summary['cases']} payloads to {args.out}", file=sys.stderr)
    if summary["skipped"]:
        print(f"skipped {len(summary['skipped'])}: {summary['skipped']}", file=sys.stderr)
    print("score it with the same scorer as every other arm:", file=sys.stderr)
    print(f"  ./.venv/bin/python evals/score_arm.py {args.out} "
          "--gold data/gold_labels/gold_v2.json", file=sys.stderr)
    sys.stdout.write(json.dumps(summary, indent=2) + "\n")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
