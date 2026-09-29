#!/usr/bin/env python3
"""The low-code control arm: the same model and prompt with the gate layer removed.

    ./.venv/bin/python evals/lowcode_arm.py build
    ./.venv/bin/python evals/score_arm.py evals/results/lowcode-proxy \
        --gold data/gold_labels/gold_v2.json
    ./.venv/bin/python evals/metrics.py evals/results/lowcode-proxy \
        evals/results/gold-v2-live --gold data/gold_labels/gold_v2.json

`project_proposal.md` §5 records that prompts and schemas were prototyped in Google
AI Studio before migrating to Python. The watch-outs ask for the measured version
of that claim - "I tried X first, it broke on Y, so I built Z" - and until now the
project had the sentence and not the number.

**The arm needs no new model calls, because the run already contains it.**
`run_ekacare.py` writes both `gated.jsonl` and `ungated.jsonl` from one cached
call: the second is the model's raw structured output with `gate_enabled: false`,
which is exactly what a console gives you. Same model, same prompt, same strict
JSON schema, no span verification, no value grounding, no hard wipe. This script
materialises those payloads as a run directory so the **pre-registered** scorer
grades the low-code arm with the same rules as the coded one.

**It is a proxy, and the limit is worth stating before the numbers.** This is not
a measurement of Google AI Studio. It measures the same model and prompt with the
deterministic layer removed, which is the capability boundary a console has - but
it still enjoys the `FIELD_RULES` prompt work, which could itself be pasted into a
console. `docs/lowcode_pack/` exists to close that gap for anyone who wants the
real thing: the exact prompt and schema to paste, and a converter that turns
console output into payloads this same path can score.

Nothing is invented and nothing is re-run. The payloads are copied verbatim from
the source run, and the manifest records which run they came from and that the
arm is a proxy, so a reader cannot mistake it for an independent measurement.

stdout: one JSON summary. stderr: what was written.
Exit codes: 0 built; 2 inputs unusable; 6 internal error.
"""

import argparse
import json
import shutil
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "evals"))

from extract import CRITICAL_FIELDS  # noqa: E402

RESULTS_DIR = ROOT / "evals" / "results"
DEFAULT_SOURCE = RESULTS_DIR / "gold-v2-live"
DEFAULT_OUT = RESULTS_DIR / "lowcode-proxy"

EXIT_OK = 0
EXIT_UNUSABLE = 2
EXIT_INTERNAL = 6

# What a console configured with this prompt and schema cannot do at all, and the
# symbol in this repository that does it. Each row is a capability rather than an
# opinion, and each names its own evidence so the table can be checked instead of
# believed.
CANNOT_DO = [
    ("Hard-wipe an ungrounded field to null",
     "src/extract.py:wiped_extraction", "the payload is never a display string"),
    ("Verify a quote is verbatim in the note",
     "src/extract.py:verify_evidence", "containment, not plausibility"),
    ("Verify the value is supported by its own quote",
     "src/extract.py:verify_value", "catches a real quote paired with a wrong value"),
    ("Pair a dose number with its unit",
     "src/extract.py:_check_dose", "15 mg against 50 mg, by Decimal"),
    ("Flag instruction-like text in the note",
     "src/extract.py:INJECTION_PATTERNS", "the arm that matters most - see below"),
    ("Force abstention on hidden or look-alike characters",
     "src/extract.py:scan_input", "refused before any call is billed"),
    ("Separate a safe abstention from an API failure",
     "src/extract.py:EXIT_BLANKED vs EXIT_UPSTREAM", "exit 1 is not exit 3"),
    ("Refuse a call that would cross a spend bound",
     "evals/spend_guard.py:CostGuard.check_before", "checked against worst case"),
    ("Hold a ground truth immutable",
     "evals/run_ekacare.py:REGISTRY", "SHA-256, 0444, write-once seals"),
    ("Test a delta rather than eyeball it",
     "evals/metrics.py:mcnemar", "paired, exact, over the fields that changed"),
    ("Keep stdout machine-parsable",
     "src/extract.py:main", "one JSON document, telemetry to stderr"),
    ("Fail a build when a document drifts from its artefact",
     "tests/test_guardrails_doc.py", "15 checks, run in the suite"),
]


def _rel(path) -> str:
    try:
        return str(Path(path).resolve().relative_to(ROOT))
    except ValueError:
        return str(path)


def console_payload(payload: dict) -> dict:
    """One payload as a console would deliver it: every value presented as fact.

    This is the correction that makes the arm mean anything. `ungated.jsonl`
    retains the values the gate would have wiped, but it *also* retains the gate
    array - the verdicts are reported and simply not applied. And
    `scoring.score` reads `proposed`, `verified` and `wiped` from those codes,
    not from whether a value is null. So copying the file verbatim scored
    identically to the coded pipeline, and the first version of this script did
    exactly that: it compared the system with itself and called the null result a
    finding.

    A console has no gate to report. Every field carrying a value is delivered as
    stated, and a field with no value is not stated. Nothing is wiped, nothing is
    held for review, and - this is the whole cost - nothing distinguishes a value
    grounded in the note from one the model invented.
    """
    out = dict(payload)
    out["gate_enabled"] = False
    out["gate"] = [
        {"field": name,
         "outcome": "pass",
         # VERIFIED in the scorer's vocabulary means "proposed and presented as
         # correct", which is precisely what a console does with every value it
         # produces. It is not a claim that anything was verified.
         "code": "VERIFIED" if (payload["extraction"].get(name) or {}).get("value")
                 else "NOT_STATED",
         "near_miss": False}
        for name in CRITICAL_FIELDS
    ]
    out["lowcode_proxy"] = True
    return out


def build(source: Path, out: Path) -> dict:
    """Materialise the ungated payloads as a scoreable arm.

    The payload lines are copied byte for byte. The only thing written fresh is
    the manifest, and it says what this is.
    """
    ungated = source / "ungated.jsonl"
    source_manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
    lines = [line for line in ungated.read_text(encoding="utf-8").splitlines() if line]
    payloads = [json.loads(line) for line in lines]

    enabled = [p["case_id"] for p in payloads if p.get("gate_enabled") is not False]
    if enabled:
        raise ValueError(
            f"{len(enabled)} payloads in {_rel(ungated)} have gate_enabled set: this "
            "file is not the ungated arm and scoring it as one would compare the "
            "coded pipeline against itself")

    out.mkdir(parents=True, exist_ok=True)
    # Written as `gated.jsonl` because that is the filename every scorer reads.
    # The manifest is what distinguishes the arm, not the filename.
    delivered = [console_payload(p) for p in payloads]
    (out / "gated.jsonl").write_text(
        "".join(json.dumps(p) + "\n" for p in delivered), encoding="utf-8")
    # The ungated arm of a console run is the console run: there is no second,
    # un-gated copy to compare against, so the scorer's abstention metrics are
    # correctly degenerate here rather than borrowed from the coded pipeline.
    shutil.copyfile(out / "gated.jsonl", out / "ungated.jsonl")

    manifest = {
        "run_id": "lowcode-proxy",
        "mode": "lowcode-proxy",
        "model": source_manifest.get("model"),
        "experiment": "low-code control arm",
        "derived_from": {"run": _rel(source), "file": "ungated.jsonl",
                         "run_id": source_manifest.get("run_id")},
        "gold": source_manifest.get("gold"),
        "prompt": source_manifest.get("prompt"),
        "cases_run": len(payloads),
        "what_this_is": (
            "The same model, prompt and strict JSON schema as the coded pipeline, "
            "with the deterministic gate layer removed. Stands in for a low-code "
            "console run (Google AI Studio), which can configure a prompt and a "
            "schema and cannot verify a span, wipe a field, or separate an "
            "abstention from a failure."),
        "what_this_is_not": (
            "A measurement of Google AI Studio. The payloads still benefit from "
            "this project's FIELD_RULES prompt engineering, which a console could "
            "also be given. docs/lowcode_pack/ closes that gap with the exact "
            "prompt, schema and a converter for a real console run."),
        "no_model_calls": True,
        "transformation": (
            "Values are the ungated ones, copied unchanged. The gate array is "
            "replaced with what a console reports - nothing - so every field "
            "carrying a value is delivered as stated. Without this the scorer "
            "reads the retained gate codes and grades the coded pipeline twice."),
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        # Spend is the source run's; this arm adds none. Recorded as zero so a
        # reader cannot add it to the project total twice.
        "spend": {"billed_this_run_usd": 0.0,
                  "note": "no calls: payloads copied from the source run"},
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n",
                                       encoding="utf-8")
    return {"built": _rel(out), "cases": len(payloads),
            "derived_from": manifest["derived_from"], "calls_made": 0}


def capability_table() -> str:
    """The rows as Markdown, so `docs/lowcode_arm.md` cannot drift from the code."""
    head = ("| What the coded pipeline does | Where | Why it matters |\n"
            "| :--- | :--- | :--- |\n")
    return head + "\n".join(f"| {what} | `{where}` | {why} |"
                            for what, where, why in CANNOT_DO)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=["build", "capabilities"])
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args(argv)

    if args.command == "capabilities":
        sys.stdout.write(capability_table() + "\n")
        return EXIT_OK

    try:
        for name in ("ungated.jsonl", "manifest.json"):
            if not (args.source / name).is_file():
                print(f"REFUSED: {_rel(args.source / name)} is missing", file=sys.stderr)
                return EXIT_UNUSABLE
        summary = build(args.source, args.out)
    except ValueError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return EXIT_UNUSABLE
    except Exception as exc:
        print(f"ERROR ERR_INTERNAL (exit {EXIT_INTERNAL}): {type(exc).__name__}",
              file=sys.stderr)
        if args.debug:
            traceback.print_exc()
        return EXIT_INTERNAL

    print(f"wrote {summary['built']}: {summary['cases']} cases, 0 model calls",
          file=sys.stderr)
    print("score it with the same scorer as every other arm:", file=sys.stderr)
    print(f"  ./.venv/bin/python evals/score_arm.py {summary['built']} "
          "--gold data/gold_labels/gold_v2.json", file=sys.stderr)
    sys.stdout.write(json.dumps(summary, indent=2) + "\n")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
