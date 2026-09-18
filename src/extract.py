#!/usr/bin/env python3
"""MediExtract - one dictated note in, four verified fields out.

    python src/extract.py --note gold/case_001.txt

The whole pipeline flow lives in this file: read the note, check it, call
the model once through OpenRouter, gate every field, print one JSON
document. No agent, no retrieval, no tools, no framework. The gates
(guardrails.py), the schema (schema.py) and the spend ledger (budget.py) are
libraries this file calls; guardrails.py is shared with the labelling tool so
gold labels and model output face the same verbatim check.

Transport: the OpenAI Python SDK pointed at OpenRouter
(https://openrouter.ai/api/v1), model google/gemini-2.5-flash, key in
OPENROUTER_API_KEY. Timeout, retries and the output-token cap are set on
the SDK, because its defaults (2 retries, 600 s read timeout) would let a
hung call stall a batch.

Useful flags:
    --mock              run the gates against a canned response; no API call,
                        no credit spent (the fixture contains deliberate
                        fabrications so the gates have something to catch)
    --no-gate           report gate verdicts but do not wipe anything.
                        This is the Week 3 abstention test.
    --budget-usd N      hard ceiling on total spend across runs (default 8.00,
                        or MEDIEXTRACT_BUDGET_USD). Checked before every call.

stdout carries exactly one JSON document of machine-checkable values: the
extraction payload (status "ok") or an error envelope (status "error", an
error code, data null). No human-readable text goes there - no display
labels, no gate reasons, no error messages. All of that - the gate table,
error messages, any stray print from a library - goes to stderr, because
stdout is redirected to stderr while the pipeline runs.

Exit codes (pinned by tests/test_extract_cli.py):
    0  every field verified, not stated, or routed to review
    1  at least one field wiped by a gate
    2  input rejected: missing, unreadable, empty, oversized, binary or not
       UTF-8. argparse usage errors also exit 2, but print no envelope.
    3  upstream failure: timeout, unreachable, rate limited, auth, model or
       provider unavailable. There is no automatic fallback to another model.
    4  the model output failed the schema twice, or was truncated
    5  spend ceiling reached (the call was not made) or OpenRouter credit
       exhausted
    6  configuration or internal error

A crash can therefore never be mistaken for an abstention (exit 1), which
is what the Week 3 harness counts.
"""

import argparse
import contextlib
import hashlib
import json
import os
import re
import sys
import time
import traceback
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from budget import (  # noqa: E402
    DEFAULT_BUDGET_USD, DEFAULT_LEDGER, BudgetExceeded, SpendLedger, worst_case_cost,
)
from guardrails import (  # noqa: E402
    BLANKED, MAX_NOTE_CHARS, PASS, SKIPPED, InputRejected, InputScan, apply_gates,
    check_input, scan_input, wiped_extraction,
)
from schema import (  # noqa: E402
    BLANK, ClinicalExtraction, render_field_rules, response_json_schema,
)

ROOT = Path(__file__).resolve().parent.parent
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
MODEL = "google/gemini-2.5-flash"
OUTPUT_SCHEMA_VERSION = "mediextract.output.v3"

# USD per million tokens, by model, as listed by OpenRouter's public models
# API on 2026-09-18 (reasoning tokens bill at the output rate). A model not
# listed here cannot be called without --price-in-per-mtok and
# --price-out-per-mtok: the spend ceiling is only as honest as its prices.
PRICES_PER_MTOK = {
    "google/gemini-2.5-flash": (0.30, 2.50),
}

# The physician-facing budget from the persona work: verification must fit
# inside 3 seconds, so the extraction itself needs to land well under that.
LATENCY_BUDGET_MS = 3000

HTTP_TIMEOUT_S = 15.0
HTTP_MAX_RETRIES = 1                 # SDK default is 2; one retry = 2 attempts
MAX_OUTPUT_TOKENS = 1024             # about 250 are needed: 4x headroom
SCHEMA_ATTEMPTS = 2                  # one retry when the output fails the schema
MAX_NOTE_BYTES = MAX_NOTE_CHARS * 4  # UTF-8 worst case, checked before reading

# OpenRouter routing: only endpoints that honour every parameter we send
# (so the JSON schema is enforced, not ignored), and only providers whose
# data policy does not allow collecting the prompt. "allow" is accepted for
# public or synthetic notes only (--provider-data-collection).
PROVIDER_PREFERENCES = {"require_parameters": True, "data_collection": "deny"}
REASONING = {"effort": "none"}       # thinking costs latency we do not have

NOTE_TEMPLATE = "<note>\n{note}\n</note>"

EXIT_OK = 0
EXIT_BLANKED = 1
EXIT_INPUT = 2
EXIT_UPSTREAM = 3
EXIT_SCHEMA = 4
EXIT_BUDGET = 5
EXIT_INTERNAL = 6

SYSTEM_INSTRUCTION = f"""\
You extract four fields from a dictated clinical note for a polyclinic physician.

FIELDS
{render_field_rules()}

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
               the physician one glance, a wrong field costs a patient.\
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


class PipelineError(Exception):
    """A request-level failure: becomes an error envelope and an exit code."""

    def __init__(self, code: str, message: str, exit_code: int):
        super().__init__(message)
        self.code = code
        self.message = message
        self.exit_code = exit_code


def error_envelope(code: str, note: str | None = None) -> dict:
    """The stdout document for a failed request: a code and null data. The
    human-readable message goes to stderr, never into this payload."""
    return {
        "status": "error",
        "code": code,
        "data": None,
        "schema_version": OUTPUT_SCHEMA_VERSION,
        "note": note,
    }


def read_note(path: Path) -> str:
    """The canonical source text: UTF-8 (a leading BOM dropped), universal
    newlines exactly as Path.read_text() produces them, otherwise untouched.
    The model, the gates and the physician all see this same text."""
    if not path.is_file():
        raise PipelineError("ERR_INPUT_NOT_FOUND", f"note not found: {path}", EXIT_INPUT)
    size = path.stat().st_size
    if size > MAX_NOTE_BYTES:
        raise PipelineError(
            "ERR_INPUT_TOO_LARGE",
            f"the file is {size:,} bytes; notes are limited to {MAX_NOTE_CHARS:,} characters",
            EXIT_INPUT,
        )
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise PipelineError(
            "ERR_INPUT_UNREADABLE", f"could not read the note ({type(exc).__name__})", EXIT_INPUT,
        ) from None
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise PipelineError(
            "ERR_INPUT_ENCODING", f"the note is not valid UTF-8 (byte {exc.start})", EXIT_INPUT,
        ) from None
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    check_text(text)
    return text


def check_text(text: str) -> None:
    """check_input, mapped onto this file's envelope and exit code."""
    try:
        check_input(text)
    except InputRejected as exc:
        raise PipelineError(exc.code, str(exc), EXIT_INPUT) from None


def _looks_like_placeholder(key: str) -> bool:
    return "..." in key or "<" in key or len(key) < 20


def load_api_key(dotenv: Path = ROOT / ".env") -> str:
    """OPENROUTER_API_KEY from the environment, then from .env at the repo
    root. A placeholder pasted from documentation is refused, because a
    shell variable would otherwise shadow the real key in .env."""
    key = os.environ.get("OPENROUTER_API_KEY")
    source = "the OPENROUTER_API_KEY environment variable"
    if not key and dotenv.is_file():
        for line in dotenv.read_text(encoding="utf-8").splitlines():
            name, _, value = line.strip().partition("=")
            if name.strip() == "OPENROUTER_API_KEY" and value.strip():
                key, source = value.strip().strip("'\""), f"{dotenv.name}"
    if not key:
        raise PipelineError(
            "ERR_CONFIG_NO_API_KEY",
            "no API key: set OPENROUTER_API_KEY in your shell or in .env at the repo "
            "root, or run with --mock to exercise the gates without an API call",
            EXIT_INTERNAL,
        )
    if _looks_like_placeholder(key):
        raise PipelineError(
            "ERR_CONFIG_PLACEHOLDER_KEY",
            f"OPENROUTER_API_KEY from {source} is a placeholder, not a key "
            "(run `unset OPENROUTER_API_KEY` if a pasted export is shadowing .env)",
            EXIT_INTERNAL,
        )
    return key


def resolve_prices(model: str, price_in: float | None, price_out: float | None):
    if price_in is not None and price_out is not None:
        return price_in, price_out
    if model in PRICES_PER_MTOK:
        return PRICES_PER_MTOK[model]
    raise PipelineError(
        "ERR_CONFIG_UNPRICED_MODEL",
        f"no price on file for {model!r}: pass --price-in-per-mtok and "
        "--price-out-per-mtok from the current pricing page, or the spend "
        "ceiling would multiply by the wrong number",
        EXIT_INTERNAL,
    )


def resolve_budget(budget_usd: float | None) -> float:
    if budget_usd is not None:
        return budget_usd
    raw = os.environ.get("MEDIEXTRACT_BUDGET_USD")
    if raw is None:
        return DEFAULT_BUDGET_USD
    try:
        return float(raw)
    except ValueError:
        raise PipelineError(
            "ERR_CONFIG_BAD_BUDGET", f"MEDIEXTRACT_BUDGET_USD={raw!r} is not a number",
            EXIT_INTERNAL,
        ) from None


def build_request(model: str, note: str, data_collection: str = "deny"):
    """The complete request, built without touching the network so tests
    can pin it. Returns (client settings, create() arguments). There are no
    tools and no tool choice: the model has nothing it could call (OWASP
    LLM03:2026, Excessive Agency)."""
    client_kwargs = {
        "base_url": OPENROUTER_BASE_URL,
        "timeout": HTTP_TIMEOUT_S,
        "max_retries": HTTP_MAX_RETRIES,
    }
    request = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_INSTRUCTION},
            {"role": "user", "content": NOTE_TEMPLATE.format(note=note)},
        ],
        "temperature": 0.0,
        "seed": 0,
        "max_tokens": MAX_OUTPUT_TOKENS,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "clinical_extraction",
                "strict": True,
                "schema": response_json_schema(),
            },
        },
        "extra_body": {
            "provider": {**PROVIDER_PREFERENCES, "data_collection": data_collection},
            "reasoning": dict(REASONING),
        },
    }
    return client_kwargs, request


def prompt_fingerprint() -> str:
    """Hash of everything that steers the model except the note and the
    model ID: instructions, note template, response schema, decoding
    settings. Results are comparable only within one fingerprint."""
    material = json.dumps({
        "system": SYSTEM_INSTRUCTION,
        "template": NOTE_TEMPLATE,
        "schema": response_json_schema(),
        "temperature": 0.0,
        "seed": 0,
        "max_tokens": MAX_OUTPUT_TOKENS,
        "reasoning": REASONING,
    }, sort_keys=True)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def upstream_error(exc: Exception) -> PipelineError | None:
    """Map an SDK exception to an envelope. Only the status is echoed, never
    the upstream message: OpenRouter error bodies can carry metadata such
    as flagged input, and the input is the note."""
    import openai

    if isinstance(exc, openai.APITimeoutError):
        return PipelineError(
            "ERR_UPSTREAM_TIMEOUT",
            f"no response within {HTTP_TIMEOUT_S:.0f} s on either of {HTTP_MAX_RETRIES + 1} attempts",
            EXIT_UPSTREAM,
        )
    if isinstance(exc, openai.APIConnectionError):
        return PipelineError(
            "ERR_UPSTREAM_UNREACHABLE", "could not reach OpenRouter", EXIT_UPSTREAM,
        )
    if not isinstance(exc, openai.APIStatusError):
        return None

    status = exc.status_code
    upstream = str(getattr(exc, "message", "") or "").lower()
    if status == 402:
        return PipelineError(
            "ERR_UPSTREAM_CREDITS_EXHAUSTED",
            "OpenRouter refused the call for lack of credit (402)", EXIT_BUDGET,
        )
    if status == 404 and "endpoint" in upstream:
        return PipelineError(
            "ERR_NO_ELIGIBLE_PROVIDER",
            "no OpenRouter provider serves this model with every required parameter "
            "and the requested data policy (404). Do not relax the policy for real "
            "patient data; for public or synthetic notes only, "
            "--provider-data-collection allow widens routing",
            EXIT_UPSTREAM,
        )
    if status == 404:
        return PipelineError(
            "ERR_MODEL_UNAVAILABLE",
            "the model returned 404 - it may be retired or renamed. Pass --model "
            "explicitly; there is no automatic fallback, because a different model "
            "invalidates every number measured on this one",
            EXIT_UPSTREAM,
        )
    if status == 408:
        return PipelineError("ERR_UPSTREAM_TIMEOUT", "OpenRouter timed out (408)", EXIT_UPSTREAM)
    if status == 429:
        return PipelineError(
            "ERR_RATE_LIMITED", f"rate limited after {HTTP_MAX_RETRIES + 1} attempts", EXIT_UPSTREAM,
        )
    if status in (401, 403):
        return PipelineError(
            "ERR_UPSTREAM_AUTH",
            f"OpenRouter refused the request ({status}): invalid key, missing "
            "permission, or input flagged by moderation",
            EXIT_UPSTREAM,
        )
    if status >= 500:
        return PipelineError(
            "ERR_UPSTREAM_ERROR", f"OpenRouter or the provider failed ({status})", EXIT_UPSTREAM,
        )
    return PipelineError(
        "ERR_UPSTREAM_REJECTED", f"OpenRouter rejected the request ({status})", EXIT_UPSTREAM,
    )


_CODE_FENCE = re.compile(r"\s*```(?:json)?[ \t]*\n(.*?)\n?[ \t]*```\s*", re.DOTALL)


def _strip_code_fence(text: str) -> str:
    """Some providers wrap JSON in a markdown fence even when a schema is
    requested. Unwrap exactly one whole-document fence; anything else is
    left for the validator to reject."""
    match = _CODE_FENCE.fullmatch(text)
    return match.group(1) if match else text


def parse_output(content: str | None) -> ClinicalExtraction | None:
    """Raw model text -> validated extraction, or None. Pydantic is strict
    here: every key required, no unknown keys, a closed status enum."""
    try:
        return ClinicalExtraction.model_validate_json(_strip_code_fence(content or ""))
    except (ValueError, TypeError):  # pydantic's ValidationError is a ValueError
        return None


def _usage(completion, prices) -> dict:
    usage = getattr(completion, "usage", None)
    details = getattr(usage, "completion_tokens_details", None)
    extra = (getattr(usage, "model_extra", None) or {}) if usage is not None else {}
    reported = extra.get("cost")
    price_in, price_out = prices
    input_tokens = getattr(usage, "prompt_tokens", 0) or 0
    output_tokens = getattr(usage, "completion_tokens", 0) or 0  # includes reasoning
    estimate = round(input_tokens / 1e6 * price_in + output_tokens / 1e6 * price_out, 6)
    reported_ok = isinstance(reported, (int, float)) and not isinstance(reported, bool)
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "reasoning_tokens": (getattr(details, "reasoning_tokens", 0) or 0) if details else 0,
        "est_cost_usd": estimate,
        "provider_cost_usd": float(reported) if reported_ok else None,
        # What the ledger records: OpenRouter's own charge when it reports
        # one, the list-price estimate otherwise.
        "billed_usd": round(float(reported), 6) if reported_ok else estimate,
    }


@dataclass
class ModelResult:
    extraction: ClinicalExtraction
    usage: dict
    provenance: dict
    api_ms: float


def call_model(note, model, prices, ledger, budget_usd, data_collection="deny", client=None):
    """One constrained call through OpenRouter, plus at most one retry if
    the output fails the schema. Every attempt is checked against the
    spend ceiling first and recorded in the ledger after. `client` is for
    tests; normally one is built from the API key."""
    import importlib.metadata

    client_kwargs, request = build_request(model, note, data_collection)
    prompt_chars = sum(len(m["content"]) for m in request["messages"])
    prompt_chars += len(json.dumps(request["response_format"]))
    worst = worst_case_cost(prompt_chars, MAX_OUTPUT_TOKENS, *prices)

    totals = {"called": True, "input_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0,
              "est_cost_usd": 0.0, "provider_cost_usd": None, "billed_usd": 0.0, "attempts": 0}
    api_ms = 0.0
    for attempt in range(1, SCHEMA_ATTEMPTS + 1):
        try:
            ledger.ensure_room(worst, budget_usd)
        except BudgetExceeded as exc:
            raise PipelineError("ERR_BUDGET_EXCEEDED", str(exc), EXIT_BUDGET) from None
        if client is None:
            import openai
            client = openai.OpenAI(api_key=load_api_key(), **client_kwargs)

        started = time.perf_counter()
        try:
            completion = client.chat.completions.create(**request)
        except Exception as exc:
            mapped = upstream_error(exc)
            if mapped is None:
                raise
            raise mapped from None
        api_ms += (time.perf_counter() - started) * 1000

        usage = _usage(completion, prices)
        ledger.record(usage["billed_usd"], model)
        for key in ("input_tokens", "output_tokens", "reasoning_tokens"):
            totals[key] += usage[key]
        for key in ("est_cost_usd", "billed_usd"):
            totals[key] = round(totals[key] + usage[key], 6)
        if usage["provider_cost_usd"] is not None:
            totals["provider_cost_usd"] = round((totals["provider_cost_usd"] or 0.0)
                                                + usage["provider_cost_usd"], 6)
        totals["attempts"] = attempt

        choices = getattr(completion, "choices", None) or []
        if not choices:
            raise PipelineError(
                "ERR_UPSTREAM_ERROR", "the response carried no choices", EXIT_UPSTREAM,
            )
        if getattr(choices[0], "finish_reason", None) == "length":
            # Retrying would hit the same cap and bill twice.
            raise PipelineError(
                "ERR_OUTPUT_TRUNCATED",
                f"the output hit the {MAX_OUTPUT_TOKENS}-token cap", EXIT_SCHEMA,
            )
        extraction = parse_output(getattr(choices[0].message, "content", None))
        if extraction is not None:
            extra = getattr(completion, "model_extra", None) or {}
            try:
                sdk = f"openai {importlib.metadata.version('openai')}"
            except importlib.metadata.PackageNotFoundError:
                sdk = "openai (version unknown)"
            provenance = {
                "called": True,
                "requested_model": model,
                "served_model": getattr(completion, "model", None),
                "provider": extra.get("provider"),
                "response_id": getattr(completion, "id", None),
                "sdk": sdk,
                "prompt_fingerprint": prompt_fingerprint(),
            }
            return ModelResult(extraction, totals, provenance, api_ms)

    raise PipelineError(
        "ERR_SCHEMA_INVALID",
        f"the model output failed the schema on {SCHEMA_ATTEMPTS} attempts",
        EXIT_SCHEMA,
    )


def to_output(extraction: ClinicalExtraction) -> dict:
    """The data a consumer stores. Wiped and absent values are null (a hard
    wipe), never an empty string and never display text. Display text lives
    only in the separate `gate` list."""
    return {
        name: {
            "evidence": field.evidence if field.evidence != BLANK else None,
            "value": field.value if field.value != BLANK else None,
            "status": field.status.value,
        }
        for name, field in extraction
    }


def build_payload(extraction, source_text, scan, gate_enabled, *, note, model_label,
                  usage, provenance, api_ms, started):
    """Gate the extraction and assemble the stdout document.
    Returns (payload, exit code, gate results)."""
    gated, results = apply_gates(extraction, source_text, enabled=gate_enabled, scan=scan)
    total_ms = (time.perf_counter() - started) * 1000
    payload = {
        "status": "ok",
        "schema_version": OUTPUT_SCHEMA_VERSION,
        "note": note,
        "model": model_label,
        "provenance": provenance,
        "gate_enabled": gate_enabled,
        "latency_ms": {"api": round(api_ms, 1), "total": round(total_ms, 1)},
        "latency_budget_ms": LATENCY_BUDGET_MS,
        "within_budget": total_ms < LATENCY_BUDGET_MS,
        "usage": usage,
        "input_scan": {"flags": scan.flags, "review_required": scan.review_required},
        "extraction": to_output(gated),
        # Codes only. The review screen turns a code into its label with
        # guardrails.DISPLAY; reasons are printed to stderr.
        "gate": [
            {"field": r.field, "outcome": r.outcome, "code": r.code, "near_miss": r.near_miss}
            for r in results
        ],
    }
    exit_code = EXIT_BLANKED if any(r.outcome == BLANKED for r in results) else EXIT_OK
    return payload, exit_code, results


def _mark(result) -> str:
    if result.outcome == BLANKED:
        return "WIPED"
    if result.outcome == SKIPPED:
        return "----"
    return "REVIEW" if result.code.startswith("REVIEW") else "OK"


def print_gate_report(results, gate_enabled: bool, scan: InputScan, out=sys.stderr) -> None:
    print("\nSAFETY GATES  (evidence in source_text -> value -> number/unit)", file=out)
    print("-" * 78, file=out)
    for result in results:
        note = " [near miss]" if result.near_miss else ""
        print(f"  {_mark(result):<6} {result.field:<11} {result.display}{note}", file=out)
        if result.outcome != PASS or result.code.startswith("REVIEW"):
            print(f"  {'':<6} {'':<11} {result.reason}", file=out)
    print("-" * 78, file=out)
    if scan.review_required:
        print(f"  input flags: {', '.join(scan.flags)} -> nothing is shown pre-verified", file=out)

    failed = [r.field for r in results if r.outcome == BLANKED]
    if not failed:
        print("  verdict: no field wiped", file=out)
    elif gate_enabled:
        print(f"  verdict: wiped {', '.join(failed)} -> manual entry", file=out)
    else:
        print(
            f"  verdict: would have wiped {', '.join(failed)}, "
            "but --no-gate left the payload untouched",
            file=out,
        )


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--note", required=True, type=Path, help="path to a note .txt")
    parser.add_argument("--model", default=MODEL)
    parser.add_argument("--mock", action="store_true", help="no API call, canned response")
    parser.add_argument("--no-gate", action="store_true", help="report but do not wipe")
    parser.add_argument(
        "--json-only", action="store_true", help="suppress the stderr gate table",
    )
    parser.add_argument(
        "--budget-usd", type=float, default=None,
        help=f"total spend ceiling (default {DEFAULT_BUDGET_USD:.2f}, or MEDIEXTRACT_BUDGET_USD)",
    )
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER, help=argparse.SUPPRESS)
    parser.add_argument("--price-in-per-mtok", type=float, default=None)
    parser.add_argument("--price-out-per-mtok", type=float, default=None)
    parser.add_argument(
        "--provider-data-collection", choices=["deny", "allow"], default="deny",
        help="OpenRouter data policy; 'allow' only for public or synthetic notes",
    )
    parser.add_argument("--debug", action="store_true", help="traceback on internal errors")
    return parser.parse_args(argv)


def run(args):
    """The pipeline for one note. Returns (payload, exit code, results, scan)."""
    source_text = read_note(args.note)
    scan = scan_input(source_text)

    started = time.perf_counter()
    if scan.encoding_anomaly:
        # Forced safe abstention, decided before any call: hidden or
        # look-alike characters mean the note is not what the physician
        # sees, so nothing is extracted and nothing is sent upstream.
        extraction = wiped_extraction()
        usage = {"called": False, "billed_usd": 0.0}
        provenance = {"requested_model": "mock" if args.mock else args.model, "called": False,
                      "prompt_fingerprint": prompt_fingerprint()}
        api_ms = 0.0
    elif args.mock:
        extraction = ClinicalExtraction.model_validate(MOCK_RESPONSE)
        usage = {"called": False, "billed_usd": 0.0}
        provenance = {"called": False, "requested_model": "mock",
                      "prompt_fingerprint": prompt_fingerprint()}
        api_ms = 0.0
    else:
        prices = resolve_prices(args.model, args.price_in_per_mtok, args.price_out_per_mtok)
        result = call_model(
            source_text, args.model, prices, SpendLedger(args.ledger),
            resolve_budget(args.budget_usd), args.provider_data_collection,
        )
        extraction, usage, provenance, api_ms = (
            result.extraction, result.usage, result.provenance, result.api_ms)

    payload, exit_code, results = build_payload(
        extraction, source_text, scan, not args.no_gate,
        note=str(args.note), model_label="mock" if args.mock else args.model,
        usage=usage, provenance=provenance, api_ms=api_ms, started=started,
    )
    return payload, exit_code, results, scan


def main(argv=None) -> int:
    args = parse_args(argv)
    note = str(args.note)
    stdout = sys.stdout
    try:
        # Anything printed while the pipeline runs - by this code or by a
        # library - lands on stderr. stdout receives exactly one JSON
        # document, written below.
        with contextlib.redirect_stdout(sys.stderr):
            payload, exit_code, results, scan = run(args)
            if not args.json_only:
                print_gate_report(results, not args.no_gate, scan)
                latency = payload["latency_ms"]
                print(
                    f"\nlatency: {latency['total']:.0f} ms total ({latency['api']:.0f} ms model) "
                    f"vs {LATENCY_BUDGET_MS} ms budget   "
                    f"billed: ${payload['usage'].get('billed_usd', 0):.6f}",
                    file=sys.stderr,
                )
    except PipelineError as exc:
        payload, exit_code = error_envelope(exc.code, note), exc.exit_code
        # Errors are telemetry: always on stderr, --json-only or not.
        print(f"ERROR {exc.code} (exit {exit_code}): {exc.message}", file=sys.stderr)
    except Exception as exc:
        # The backstop. A crash must never exit 1, which means "a gate wiped
        # a field". The exception text is withheld because it can quote the
        # note (pydantic errors echo their input).
        payload = error_envelope("ERR_INTERNAL", note)
        exit_code = EXIT_INTERNAL
        print(f"ERROR ERR_INTERNAL (exit {exit_code}): {type(exc).__name__} "
              "(re-run with --debug for the traceback)", file=sys.stderr)
        if args.debug:
            traceback.print_exc()
    stdout.write(json.dumps(payload, indent=2) + "\n")
    stdout.flush()
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
