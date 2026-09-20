#!/usr/bin/env python3
"""WP0 spike: will the provider accept the v4 medication-list schema?

    ./.venv/bin/python evals/probes/probe_v4_schema.py            # live, budgeted
    ./.venv/bin/python evals/probes/probe_v4_schema.py --dry-run  # schemas only, no call

The v4 output schema replaces one medication slot with a list of entries, because
the measurement in `evals/diagnose.py` says the single slot - not the model - is
what caps recall. Before rewriting `src/extract.py` around it, one question has
to be answered against the real endpoint rather than assumed: **does strict
structured output still work when the schema contains an array of objects with
`maxItems`?**

Offline we already know the schema generates cleanly - `maxItems: 12` survives
inlining and no `$ref` is left behind - so what is untested is the provider's
acceptance of it alongside `require_parameters: true`, `temperature: 0` and
`seed: 0`. A failure here is fail-closed (the call errors) rather than silent, so
the probe tries variants in order of preference and stops at the first that works:

  1. `v4-strict`        the list with `maxItems: 12` in the wire schema
  2. `v4-no-maxitems`   the same list with the bound removed from the wire and
                        enforced in Pydantic plus a deterministic gate instead
  3. `v3-control`       today's four-field schema, known good from 67 live calls

## Budget

Two bounds, the same pattern the batch loop uses. `--cap-usd` (default $0.002)
is checked by `evals/metrics.py` against *worst case* before each attempt, and
the global $8 ceiling is still the ledger's. With `--max-output-tokens 400` one
attempt's worst case is about $0.0012, so the cap allows **two** live attempts
and refuses a third. That is deliberate: v3 is already proven by the 67-case
run, so the control does not need re-buying, and seeing the guard refuse is part
of what the spike demonstrates. Actual spend is reported against the $0.002
target; it is typically nearer $0.0007 because the first attempt succeeds or
fails on its own.

stdout: one JSON verdict. stderr: progress and the exact provider error.
Exit codes: 0 a v4 variant was accepted; 2 configuration (no key, bad flags);
3 no v4 variant was accepted - keep v3; 5 the cap stopped the probe; 6 internal.
"""

import argparse
import copy
import json
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "evals"))

import extract  # noqa: E402
import spend_guard  # noqa: E402
from extract import ExtractedField, SpendLedger  # noqa: E402
from pydantic import BaseModel, ConfigDict, Field  # noqa: E402

EXIT_V4_OK = 0
EXIT_CONFIG = 2
EXIT_V4_REJECTED = 3
EXIT_CAPPED = 5
EXIT_INTERNAL = 6

MAX_MEDICATIONS = 12          # the pool's maximum; an LLM06 bound
DEFAULT_CAP_USD = 0.002
DEFAULT_MAX_OUTPUT_TOKENS = 400

# Synthetic, written for this probe: three drugs so a list has something to
# fill, short so the input tokens stay small. Not a patient note.
PROBE_NOTE = (
    "Tablet Dolo 650 twice a day after food. Tablet Pan D once before breakfast. "
    "Syrup Augmentin 5 ml three times a day for five days. No known drug allergies."
)


class MedicationEntry(BaseModel):
    """One prescribed drug and the two facts that belong to it."""

    model_config = ConfigDict(extra="forbid")

    medication: ExtractedField
    dose: ExtractedField
    frequency: ExtractedField


class ClinicalExtractionV4(BaseModel):
    model_config = ConfigDict(extra="forbid")

    medications: list[MedicationEntry] = Field(..., max_length=MAX_MEDICATIONS)
    allergy: ExtractedField


class ClinicalExtractionV4Unbounded(BaseModel):
    """Identical, minus the wire-level bound. The cap still exists in Python."""

    model_config = ConfigDict(extra="forbid")

    medications: list[MedicationEntry]
    allergy: ExtractedField


def inline_schema(model: type[BaseModel]) -> dict:
    """Same inlining rule as extract.response_json_schema, applied to any model:
    every object inline, every property required, additionalProperties false."""
    raw = model.model_json_schema()
    defs = raw.pop("$defs", {})

    def walk(node):
        if isinstance(node, dict):
            if "$ref" in node:
                return walk(copy.deepcopy(defs[node["$ref"].split("/")[-1]]))
            out = {k: walk(v) for k, v in node.items() if k not in ("title", "default")}
            if out.get("type") == "object" and "properties" in out:
                out["required"] = list(out["properties"])
                out["additionalProperties"] = False
            return out
        if isinstance(node, list):
            return [walk(item) for item in node]
        return node

    return walk(raw)


def variants() -> list[dict]:
    strict = inline_schema(ClinicalExtractionV4)
    unbounded = inline_schema(ClinicalExtractionV4Unbounded)
    return [
        {"name": "v4-strict", "schema": strict, "model": ClinicalExtractionV4,
         "note": "array of objects with maxItems in the wire schema"},
        {"name": "v4-no-maxitems", "schema": unbounded, "model": ClinicalExtractionV4Unbounded,
         "note": "bound moved out of the wire schema into Pydantic and a gate"},
        {"name": "v3-control", "schema": extract.response_json_schema(),
         "model": extract.ClinicalExtraction,
         "note": "today's schema; already proven by 67 live calls"},
    ]


def describe(schema: dict) -> dict:
    """What the offline checks can say without spending anything."""
    blob = json.dumps(schema)
    medications = (schema.get("properties") or {}).get("medications") or {}
    return {
        "chars": len(blob),
        "has_unresolved_ref": "$ref" in blob,
        "max_items": medications.get("maxItems"),
        "array_of_objects": (medications.get("type") == "array"
                             and (medications.get("items") or {}).get("type") == "object"),
        "additional_properties_false": schema.get("additionalProperties") is False,
    }


def build_request(model: str, schema: dict, name: str, max_output_tokens: int,
                  data_collection: str) -> tuple[dict, dict]:
    client_kwargs = {
        "base_url": extract.OPENROUTER_BASE_URL,
        "timeout": extract.HTTP_TIMEOUT_S,
        "max_retries": extract.HTTP_MAX_RETRIES,
    }
    request = {
        "model": model,
        "messages": [
            {"role": "system", "content": extract.SYSTEM_INSTRUCTION},
            {"role": "user", "content": extract.NOTE_TEMPLATE.format(note=PROBE_NOTE)},
        ],
        "temperature": 0.0,
        "seed": 0,
        "max_tokens": max_output_tokens,
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": name.replace("-", "_"), "strict": True, "schema": schema},
        },
        "extra_body": {
            "provider": {**extract.PROVIDER_PREFERENCES, "data_collection": data_collection},
            "reasoning": dict(extract.REASONING),
        },
    }
    return client_kwargs, request


def error_detail(exc: Exception) -> dict:
    """Everything about the refusal that is worth writing down."""
    response = getattr(exc, "response", None)
    body = None
    if response is not None:
        try:
            body = response.json()
        except Exception:
            body = getattr(response, "text", None)
    return {
        "type": type(exc).__name__,
        "status_code": getattr(exc, "status_code", None) or getattr(response, "status_code", None),
        "message": str(exc)[:600],
        "body": body if isinstance(body, (dict, str)) else None,
    }


def attempt(client, variant: dict, cfg, guard: spend_guard.CostGuard, prices) -> dict:
    client_kwargs, request = build_request(
        cfg.model, variant["schema"], variant["name"], cfg.max_output_tokens,
        cfg.data_collection)
    prompt_chars = sum(len(m["content"]) for m in request["messages"])
    prompt_chars += len(json.dumps(request["response_format"]))
    worst = extract.worst_case_cost(prompt_chars, cfg.max_output_tokens, *prices)

    try:
        guard.check_before(worst)
    except extract.BudgetExceeded as exc:
        return {"variant": variant["name"], "accepted": None, "skipped": "cap",
                "reason": str(exc), "worst_case_usd": round(worst, 6)}

    if client is None:
        import openai
        client = openai.OpenAI(api_key=extract.load_api_key(), **client_kwargs)

    started = time.perf_counter()
    try:
        completion = client.chat.completions.create(**request)
    except Exception as exc:
        api_ms = (time.perf_counter() - started) * 1000
        detail = error_detail(exc)
        # A rejected request is normally not billed, but record the attempt so
        # the audit log shows every call that left this machine.
        guard.record(spend_guard.CallRecord.from_result(
            guard.run_id, variant["name"], cfg.model, {"attempts": 1},
            {"requested_model": cfg.model}, api_ms, ok=False,
            code=str(detail.get("status_code") or detail["type"])))
        return {"variant": variant["name"], "accepted": False, "error": detail,
                "latency_ms": round(api_ms, 1)}

    api_ms = (time.perf_counter() - started) * 1000
    usage = extract._usage(completion, prices)
    provenance = {
        "requested_model": cfg.model,
        "served_model": getattr(completion, "model", None),
        "provider": (getattr(completion, "model_extra", None) or {}).get("provider"),
        "response_id": getattr(completion, "id", None),
    }
    guard.record(spend_guard.CallRecord.from_result(
        guard.run_id, variant["name"], cfg.model, {**usage, "attempts": 1},
        provenance, api_ms))

    choices = getattr(completion, "choices", None) or []
    content = getattr(choices[0].message, "content", None) if choices else None
    finish = getattr(choices[0], "finish_reason", None) if choices else None
    parsed, validated, validation_error = None, False, None
    if content:
        try:
            parsed = json.loads(extract._strip_code_fence(content))
        except ValueError as exc:
            validation_error = f"not JSON: {exc}"
        if parsed is not None:
            try:
                variant["model"].model_validate(parsed)
                validated = True
            except Exception as exc:
                validation_error = f"{type(exc).__name__}: {str(exc)[:300]}"
    return {
        "variant": variant["name"],
        # Accepted means: the endpoint took the schema, returned JSON, and the
        # JSON satisfies the Pydantic model the pipeline would use.
        "accepted": bool(validated),
        "finish_reason": finish,
        "validated": validated,
        "validation_error": validation_error,
        "entries_returned": (len(parsed.get("medications", [])) if isinstance(parsed, dict)
                             and isinstance(parsed.get("medications"), list) else None),
        "served_model": provenance["served_model"],
        "provider": provenance["provider"],
        "latency_ms": round(api_ms, 1),
        "billed_usd": usage["billed_usd"],
        "tokens": {"input": usage["input_tokens"], "output": usage["output_tokens"]},
    }


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model", default=extract.MODEL)
    parser.add_argument("--cap-usd", type=float, default=DEFAULT_CAP_USD,
                        help=f"hard cap for this probe (default ${DEFAULT_CAP_USD})")
    parser.add_argument("--max-output-tokens", type=int, default=DEFAULT_MAX_OUTPUT_TOKENS)
    parser.add_argument("--provider-data-collection", dest="data_collection",
                        choices=["deny", "allow"], default="deny")
    parser.add_argument("--dry-run", action="store_true",
                        help="report the schemas and spend nothing")
    parser.add_argument("--price-in-per-mtok", type=float, default=None)
    parser.add_argument("--price-out-per-mtok", type=float, default=None)
    parser.add_argument("--ledger", type=Path, default=extract.DEFAULT_LEDGER)
    parser.add_argument("--debug", action="store_true")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    cfg = parse_args(argv)
    run_id = f"probe-v4-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}"
    report = {"probe": "v4-wire-schema", "run_id": run_id, "model": cfg.model,
              "cap_usd": cfg.cap_usd, "max_output_tokens": cfg.max_output_tokens,
              "offline": {}, "attempts": []}
    try:
        all_variants = variants()
        for variant in all_variants:
            report["offline"][variant["name"]] = describe(variant["schema"])
            print(f"{variant['name']:16} {json.dumps(report['offline'][variant['name']])}",
                  file=sys.stderr)
        if cfg.dry_run:
            report["verdict"] = "dry-run: schemas generated, nothing sent"
            sys.stdout.write(json.dumps(report, indent=2) + "\n")
            return EXIT_V4_OK

        prices = extract.resolve_prices(cfg.model, cfg.price_in_per_mtok, cfg.price_out_per_mtok)
        ceiling = extract.resolve_budget(None)
        ledger = SpendLedger(cfg.ledger)
        # This probe calls the endpoint directly rather than through
        # extract.call_model, so the guard is the only thing that can write the
        # project ledger. Without records_to_ledger the spend would never reach
        # the $8 ceiling that is meant to bound it.
        guard = spend_guard.CostGuard(run_id=run_id, cap_usd=cfg.cap_usd, ledger=ledger,
                                  ceiling_usd=ceiling, expected_calls=len(all_variants),
                                  records_to_ledger=True)
        client = None
        accepted = None
        for variant in all_variants:
            print(f"\n-> {variant['name']}: {variant['note']}", file=sys.stderr)
            outcome = attempt(client, variant, cfg, guard, prices)
            report["attempts"].append(outcome)
            if outcome.get("skipped") == "cap":
                print(f"   skipped: {outcome['reason']}", file=sys.stderr)
                continue
            if outcome["accepted"]:
                print(f"   ACCEPTED, {outcome['entries_returned']} medication entries, "
                      f"{outcome['latency_ms']:.0f} ms, ${outcome['billed_usd']:.6f}",
                      file=sys.stderr)
                accepted = variant["name"]
                break
            print(f"   REJECTED: {json.dumps(outcome.get('error') or outcome.get('validation_error'))[:400]}",
                  file=sys.stderr)
        report["cost"] = guard.summary()
        guard.print_summary(sys.stderr)
        report["accepted_variant"] = accepted
        report["within_target_usd"] = report["cost"]["billed_usd"] < 0.002
    except extract.PipelineError as exc:
        print(f"REFUSED {exc.code}: {exc.message}", file=sys.stderr)
        report["verdict"] = exc.code
        sys.stdout.write(json.dumps(report, indent=2) + "\n")
        return EXIT_CONFIG
    except Exception as exc:
        print(f"ERROR ERR_INTERNAL: {type(exc).__name__}", file=sys.stderr)
        if cfg.debug:
            traceback.print_exc()
        return EXIT_INTERNAL

    if accepted is None:
        capped = any(a.get("skipped") == "cap" for a in report["attempts"])
        report["verdict"] = ("the cap stopped the probe before every variant was tried"
                            if capped else "no variant was accepted")
        sys.stdout.write(json.dumps(report, indent=2) + "\n")
        return EXIT_CAPPED if capped else EXIT_V4_REJECTED
    if accepted == "v3-control":
        report["verdict"] = "only v3 works: keep the four-field schema"
        sys.stdout.write(json.dumps(report, indent=2) + "\n")
        return EXIT_V4_REJECTED
    report["verdict"] = f"{accepted} accepted: v4 is viable"
    sys.stdout.write(json.dumps(report, indent=2) + "\n")
    return EXIT_V4_OK


if __name__ == "__main__":
    sys.exit(main())
