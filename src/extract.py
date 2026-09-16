#!/usr/bin/env python3
"""MediExtract - smallest first version.

One dictated note in, four verified fields out:

    python src/extract.py --note gold/case_001.txt

Single pass, single call. No agent, no retrieval, no framework. Gemini 2.5
Flash is called once with JSON-schema-constrained decoding, then the
deterministic evidence gate in guardrails.py decides what Dr. Aisha is
allowed to see.

Useful flags:
    --mock              run the gate against a canned response; no API call,
                        no credit spent (the fixture contains a deliberate
                        fabrication so the gate has something to catch)
    --no-gate           report gate verdicts but do not blank anything.
                        This is the Week 3 abstention test.
    --thinking-budget   0 by default. Thinking costs latency we do not have.

stdout is always nothing but the JSON payload, so the Week 3 harness can
pipe it. The human-readable gate table goes to stderr.
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from guardrails import BLANKED, PASS, apply_gates  # noqa: E402
from schema import ClinicalExtraction  # noqa: E402

MODEL = "gemini-2.5-flash"

# USD per million tokens. Check against the current Gemini pricing page
# before quoting these in the report - they move.
PRICE_IN_PER_MTOK = 0.30
PRICE_OUT_PER_MTOK = 2.50

# The physician-facing budget from the persona work: verification must fit
# inside 3 seconds, so the extraction itself needs to land well under that.
LATENCY_BUDGET_MS = 3000

SYSTEM_INSTRUCTION = """\
You extract four fields from a dictated clinical note for a polyclinic physician.

FIELDS
  medication - the drug the patient is being prescribed or is currently taking
  dose       - the amount per administration of that medication
  frequency  - how often it is taken
  allergy    - a drug or substance allergy the patient has

EVIDENCE IS THE PRODUCT
For every field you mark `found` or `unsure`, `evidence` must be a span copied
VERBATIM from the note - character for character, including original casing,
spelling, abbreviations and spacing. Do not expand shorthand, do not correct
typos, do not re-wrap whitespace, do not add surrounding words that are not
contiguous in the note. A span that is not a literal substring of the note is
discarded and the field is shown to the physician as blank, so a paraphrased
span is worse than no span.

WHAT DOES NOT COUNT
  Negation      - "denies any drug allergies", "no known allergies" -> the
                  allergy field is not_stated, not an allergy.
  Attribution   - "mother has diabetes on metformin" -> not the patient's
                  medication. not_stated.
  Temporality   - "was on metformin until March" -> discontinued, not current.
                  not_stated.
  Contemplation - "consider starting amlodipine if BP stays high" -> not
                  prescribed. not_stated.

STATUS
  found      - stated for this patient, now, and you have a verbatim span.
  not_stated - the note is silent, or the mention is excluded by the rules
               above. Leave value and evidence as empty strings.
  unsure     - stated but genuinely ambiguous. Supply your best verbatim span.
               Prefer `unsure` over a confident guess; an unsure field costs
               the physician one glance, a wrong field costs a patient.

If several medications are mentioned, report the single most clinically
significant one being prescribed or continued at this visit.\
"""

MOCK_RESPONSE = {
    # medication + dose: verbatim, should pass.
    "medication": {"value": "Metformin", "evidence": "metformin", "status": "found"},
    "dose": {"value": "500 mg", "evidence": "500 mg", "status": "found"},
    # frequency: paraphrased shorthand ("po bid" -> "twice daily"). Near miss
    # in spirit, absent in fact -> blanked.
    "frequency": {"value": "twice daily", "evidence": "twice daily", "status": "found"},
    # allergy: outright fabrication -> blanked.
    "allergy": {
        "value": "Penicillin",
        "evidence": "patient reports a penicillin allergy",
        "status": "found",
    },
}


def load_api_key() -> str:
    """Env first, then a .env file next to the repo root. No extra deps."""
    for var in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
        if os.environ.get(var):
            return os.environ[var]

    dotenv = Path(__file__).resolve().parent.parent / ".env"
    if dotenv.is_file():
        for line in dotenv.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            if key.strip() in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
                return value.strip().strip("'\"")

    sys.exit(
        "No API key. Set GEMINI_API_KEY in your shell or in a .env file at the "
        "repo root, or run with --mock to exercise the gate without an API call."
    )


def call_gemini(note: str, model: str, thinking_budget: int):
    """One constrained call. Returns (extraction, api_ms, usage dict)."""
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=load_api_key())
    config = types.GenerateContentConfig(
        system_instruction=SYSTEM_INSTRUCTION,
        response_mime_type="application/json",
        response_schema=ClinicalExtraction,
        temperature=0.0,
        thinking_config=types.ThinkingConfig(thinking_budget=thinking_budget),
    )

    started = time.perf_counter()
    response = client.models.generate_content(
        model=model,
        contents=f"<note>\n{note}\n</note>",
        config=config,
    )
    api_ms = (time.perf_counter() - started) * 1000

    extraction = response.parsed
    if extraction is None:  # schema-constrained decoding should make this dead code
        extraction = ClinicalExtraction.model_validate_json(response.text)

    meta = response.usage_metadata
    usage = {
        "input_tokens": getattr(meta, "prompt_token_count", None),
        "output_tokens": getattr(meta, "candidates_token_count", None),
        "thinking_tokens": getattr(meta, "thoughts_token_count", None) or 0,
        "total_tokens": getattr(meta, "total_token_count", None),
    }
    billable_out = (usage["output_tokens"] or 0) + usage["thinking_tokens"]
    usage["est_cost_usd"] = round(
        (usage["input_tokens"] or 0) / 1e6 * PRICE_IN_PER_MTOK
        + billable_out / 1e6 * PRICE_OUT_PER_MTOK,
        6,
    )
    return extraction, api_ms, usage


def print_gate_report(results, gate_enabled: bool, out=sys.stderr) -> None:
    print("\nEVIDENCE GATE  (assert evidence in source_text)", file=out)
    print("-" * 64, file=out)
    for result in results:
        mark = {PASS: "PASS", BLANKED: "FAIL", "skipped": "----"}[result.outcome]
        note = " [near miss]" if result.near_miss else ""
        print(f"  {mark}  {result.field:<11} {result.reason}{note}", file=out)
    print("-" * 64, file=out)

    failed = [r.field for r in results if r.outcome == BLANKED]
    if not failed:
        print("  verdict: PASS - every populated field is verifiable", file=out)
    elif gate_enabled:
        print(f"  verdict: FAIL - blanked {', '.join(failed)} -> human review", file=out)
    else:
        print(
            f"  verdict: FAIL - would have blanked {', '.join(failed)}, "
            "but --no-gate left the payload untouched",
            file=out,
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--note", required=True, type=Path, help="path to a note .txt")
    parser.add_argument("--model", default=MODEL)
    parser.add_argument("--thinking-budget", type=int, default=0)
    parser.add_argument("--mock", action="store_true", help="no API call, canned response")
    parser.add_argument("--no-gate", action="store_true", help="report but do not blank")
    parser.add_argument(
        "--json-only", action="store_true", help="suppress the stderr gate table"
    )
    args = parser.parse_args()

    if not args.note.is_file():
        return f"note not found: {args.note}"
    source_text = args.note.read_text()

    total_started = time.perf_counter()
    if args.mock:
        extraction = ClinicalExtraction.model_validate(MOCK_RESPONSE)
        api_ms, usage = 0.0, {"est_cost_usd": 0.0, "note": "mock, no API call"}
    else:
        extraction, api_ms, usage = call_gemini(
            source_text, args.model, args.thinking_budget
        )

    gate_enabled = not args.no_gate
    gated, results = apply_gates(extraction, source_text, enabled=gate_enabled)
    total_ms = (time.perf_counter() - total_started) * 1000

    payload = {
        "note": str(args.note),
        "model": "mock" if args.mock else args.model,
        "gate_enabled": gate_enabled,
        "latency_ms": {"api": round(api_ms, 1), "total": round(total_ms, 1)},
        "latency_budget_ms": LATENCY_BUDGET_MS,
        "within_budget": total_ms < LATENCY_BUDGET_MS,
        "usage": usage,
        "extraction": gated.model_dump(mode="json"),
        "gate": [
            {
                "field": r.field,
                "outcome": r.outcome,
                "reason": r.reason,
                "near_miss": r.near_miss,
            }
            for r in results
        ],
    }
    print(json.dumps(payload, indent=2))

    if not args.json_only:
        print_gate_report(results, gate_enabled)
        print(
            f"\nlatency: {total_ms:.0f} ms total ({api_ms:.0f} ms model) "
            f"vs {LATENCY_BUDGET_MS} ms budget"
            f"   cost: ${usage.get('est_cost_usd', 0):.6f}",
            file=sys.stderr,
        )

    # Exit 1 when the gate blanked something, so the Week 3 harness can count
    # abstentions straight off the exit code.
    return 1 if any(r.outcome == BLANKED for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
