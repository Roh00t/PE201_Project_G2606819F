#!/usr/bin/env python3
"""Calibrate an LLM judge against the sealed gold labels, and probe it for bias.

    ./.venv/bin/python evals/judge_calibration.py evals/results/gold-v2-live --dry-run
    ./.venv/bin/python evals/judge_calibration.py evals/results/gold-v2-live \
        --gold data/gold_labels/gold_v2.json --arm baseline --cap-usd 0.06
    ./.venv/bin/python evals/judge_calibration.py evals/results/gold-v2-live \
        --arm swap --limit 20 --cap-usd 0.02
    ./.venv/bin/python evals/judge_calibration.py --report evals/results/judge-gold-v2-live

The question is narrow and worth stating before the numbers: **could an LLM judge
stand in for the sealed labels?** If it agrees with them, a future prompt change
could be scored without a human labelling 268 more field decisions. If it does
not, the labels stay the only ground truth and the judge is a diagnostic at best.
Answering it needs the judge measured against gold, not trusted against it.

**What is being compared.** Two raters over the same field decisions: the
deterministic scorer applied to sealed gold (`evals/scoring.py:value_matches`),
and the judge (`evals/judge_rubric.py`). Agreement is computed over EVERY field
decision - 67 cases x 4 fields = 268 - and not over recall's 155 gold-`found`
fields. A judge that cannot agree "this note says nothing about allergies" is
useless in a clinic, so the correct negatives belong in the denominator. That
makes the population here different from the one the recall headline uses, and the
two numbers must never be set side by side.

**Both agreement statistics, with their chance terms.** Cohen's kappa is what an
evaluator will look for and it misleads badly on lopsided marginals; Gwet's AC1
does not. Which one applies is a property of the data, so the report prints both,
each beside the chance-agreement term that explains the gap.

**Three arms, and a fourth that costs nothing.** `baseline` is the calibration.
`swap` re-asks the same cases with the four fields in reverse order: same
evidence, same decisions, so a changed verdict is position bias and not
judgement. `verbose` re-asks them demanding full narrative rationales, to price
what verbosity buys. `compressed` is derived post hoc from `baseline` by pulling
every confidence toward the centre of the scale - no calls at all, and an exactly
paired re-read, which a fourth live arm could not be.

stdout: one JSON summary. stderr: the tables and the live cost line.
Exit codes: 0 done; 2 inputs unusable; 3 upstream/API failure; 4 the judge's
output failed the schema on every case; 5 a cost bound would be crossed;
6 internal error. 3 and 5 are distinct from every other code on purpose
(CLAUDE.md §3.3): an API failure must never be counted as a judge abstention.
"""

import argparse
import json
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "evals"))

import extract  # noqa: E402
import judge_rubric as rubric  # noqa: E402
import metrics  # noqa: E402
import spend_guard  # noqa: E402
from extract import CRITICAL_FIELDS  # noqa: E402

DEFAULT_GOLD = ROOT / "data" / "gold_labels" / "gold_v2.json"
RESULTS_DIR = ROOT / "evals" / "results"

EXIT_OK = 0
EXIT_UNUSABLE = 2
EXIT_UPSTREAM = 3
EXIT_SCHEMA = 4
EXIT_BUDGET = 5
EXIT_INTERNAL = 6

# Each arm is one instruction to the judge about the same 268 decisions. Only
# `baseline` is a calibration; the others exist to be differenced against it.
ARMS = {
    "baseline": {"order": rubric.FIELDS, "brevity": "terse",
                 "asks": "the calibration arm: natural field order, terse rationales"},
    "swap": {"order": tuple(reversed(rubric.FIELDS)), "brevity": "terse",
             "asks": "position-bias probe: the same decisions, fields reversed"},
    "verbose": {"order": rubric.FIELDS, "brevity": "verbose",
                "asks": "verbosity probe: the same decisions, full narrative rationales"},
}
DERIVED_ARM = "compressed"


def _rel(path) -> str:
    try:
        return str(Path(path).resolve().relative_to(ROOT))
    except ValueError:
        return str(path)


def load_jsonl(path: Path) -> dict:
    return {rec["case_id"]: rec for rec in
            (json.loads(line) for line in
             path.read_text(encoding="utf-8").splitlines() if line)}


def worst_case_one(messages, max_output_tokens: int, prices) -> float:
    chars = sum(len(m["content"]) for m in messages)
    return extract.worst_case_cost(chars, max_output_tokens, *prices)


# --------------------------------------------------------------------------
# One call
# --------------------------------------------------------------------------

def judge_case(case_id: str, note: str, extraction: dict, arm: str, *,
               client, guard, prices, model: str) -> dict:
    """One judged case. Returns a row; never raises for a bad verdict.

    A reply that fails the schema is recorded as `parsed: False` and drops out of
    the agreement denominator. It is not turned into a guessed verdict, and it is
    not the same event as the judge answering `unclear` - one is our failure to
    read the judge, the other is the judge declining to decide (CLAUDE.md §2.3:
    no display strings in the data).
    """
    spec = ARMS[arm]
    request = rubric.build_request(note, extraction, model=model,
                                   order=spec["order"], brevity=spec["brevity"])
    guard.check_before(worst_case_one(request["messages"],
                                      request["max_tokens"], prices))

    import time
    started = time.perf_counter()
    try:
        completion = client.chat.completions.create(**request)
    except Exception as exc:
        mapped = extract.upstream_error(exc)
        raise (mapped or exc) from None
    api_ms = (time.perf_counter() - started) * 1000

    usage = extract._usage(completion, prices)
    usage["attempts"] = 1
    choices = getattr(completion, "choices", None) or []
    raw = getattr(choices[0].message, "content", None) if choices else None
    extra = getattr(completion, "model_extra", None) or {}
    provenance = {
        "requested_model": model,
        "served_model": getattr(completion, "model", None),
        "provider": extra.get("provider"),
        "response_id": getattr(completion, "id", None),
        "judge_fingerprint": rubric.judge_fingerprint(spec["brevity"]),
        "max_output_tokens": request["max_tokens"],
    }
    guard.record(spend_guard.CallRecord.from_result(
        guard.run_id, case_id, model, usage, provenance, api_ms))

    reply = rubric.parse_reply(raw)
    row = {
        "case_id": case_id, "arm": arm, "parsed": reply is not None,
        "truncated": bool(choices) and getattr(choices[0], "finish_reason", None) == "length",
        "latency_ms": round(api_ms, 1),
        "billed_usd": usage["billed_usd"],
        "provenance": provenance,
        "verdicts": (None if reply is None else
                     {name: getattr(reply, name).model_dump() for name in rubric.FIELDS}),
    }
    return row


def run_arm(arm: str, cases: list[dict], gated: dict, *, guard, model: str,
            prices, client=None, out=sys.stderr) -> list[dict]:
    if client is None:
        import openai
        client = openai.OpenAI(api_key=extract.load_api_key(),
                               base_url=extract.OPENROUTER_BASE_URL,
                               timeout=extract.HTTP_TIMEOUT_S,
                               max_retries=extract.HTTP_MAX_RETRIES)
    rows = []
    for case in cases:
        case_id = case["case_id"]
        rows.append(judge_case(case_id, case["source_text"],
                               gated[case_id]["extraction"], arm,
                               client=client, guard=guard, prices=prices, model=model))
    return rows


# --------------------------------------------------------------------------
# The statistics
# --------------------------------------------------------------------------

def judge_decisions(rows: list[dict]) -> dict:
    """(case_id, field) -> 1 if the judge called it correct. Unparsed cases are
    absent, so they leave the denominator rather than defaulting to a verdict."""
    out = {}
    for row in rows:
        if not row.get("verdicts"):
            continue
        for name, entry in row["verdicts"].items():
            out[(row["case_id"], name)] = rubric.binary(entry["verdict"])
    return out


def confidence_pairs(rows: list[dict], truth: dict, *, compress: bool = False) -> list:
    """(confidence, did the judge agree with gold) for every judged field."""
    pairs = []
    for row in rows:
        if not row.get("verdicts"):
            continue
        verdicts = (rubric.compress_verdicts(row["verdicts"]) if compress
                    else row["verdicts"])
        for name, entry in verdicts.items():
            key = (row["case_id"], name)
            if key not in truth:
                continue
            agreed = rubric.binary(entry["verdict"]) == int(bool(truth[key]))
            pairs.append((int(entry["confidence"]), agreed))
    return pairs


def confusion(table: dict) -> dict:
    """The 2x2 named, with gold as rater A and the judge as rater B.

    The asymmetry is the point. A judge that falsely rejects a good extraction
    costs a physician a second look; a judge that falsely accepts a bad one is the
    failure this project exists to prevent, so it is named rather than left as a
    cell reference.
    """
    return {
        "both_call_it_correct": table["n11"],
        "judge_wrongly_rejects": table["n10"],
        "judge_wrongly_accepts": table["n01"],
        "both_call_it_wrong": table["n00"],
        "decisions": table["n"],
        "judge_false_accept_rate": metrics.frac(
            table["n01"], table["n01"] + table["n00"]),
        "judge_false_reject_rate": metrics.frac(
            table["n10"], table["n10"] + table["n11"]),
    }


def rubber_stamp(truth: dict) -> dict:
    """The judge-shaped majority-class baseline: what a judge that answered
    "correct" to everything would score.

    `evals/score_arm.py` prints the majority-class baseline beside every recall
    figure because a recall number with nothing to compare against says nothing.
    The same is true of an agreement number, and more sharply: most extractions
    are right, so a judge can look accurate by never objecting. A judge that does
    not beat this row is not measuring anything.
    """
    n = len(truth)
    accepted = sum(1 for ok in truth.values() if ok)
    return {
        "strategy": "answer 'correct' to every field decision",
        "decisions": n,
        "agrees_with_gold": accepted,
        "agreement": metrics.frac(accepted, n),
        "catches_nothing": True,
    }


def calibrate(gold_cases: list[dict], gated: dict, rows: list[dict], *,
              compress: bool = False) -> dict:
    """Judge against gold: the agreement block, per field and pooled."""
    truth = metrics.rater_decisions(gold_cases, gated)
    judged = judge_decisions(rows)
    if compress:
        rows = [dict(r, verdicts=(rubric.compress_verdicts(r["verdicts"])
                                  if r.get("verdicts") else None)) for r in rows]

    def block(keys):
        t = {k: truth[k] for k in keys if k in truth}
        j = {k: judged[k] for k in keys if k in judged}
        table = metrics.agreement_table(t, j)
        return {
            "table": table,
            "confusion": confusion(table),
            "cohens_kappa": metrics.cohens_kappa(table),
            "gwet_ac1": metrics.gwet_ac1(table),
        }

    pooled = block(set(truth) | set(judged))
    pooled["cohens_kappa_ci95"] = metrics.bootstrap_interval(
        truth, judged, lambda t: (metrics.cohens_kappa(t) or {}).get("kappa"))
    pooled["gwet_ac1_ci95"] = metrics.bootstrap_interval(
        truth, judged, lambda t: (metrics.gwet_ac1(t) or {}).get("ac1"))

    abstained = sum(1 for r in rows if r.get("verdicts")
                    for e in r["verdicts"].values() if e["verdict"] == rubric.ABSTAIN)
    lengths = sorted(len(e["rationale"]) for r in rows if r.get("verdicts")
                     for e in r["verdicts"].values())
    stamp = rubber_stamp(truth)
    # Both sides rounded the same way, and a tie reads as "does not beat": the
    # comparison exists to refuse a judge that adds nothing, so the benefit of an
    # exact draw belongs to the baseline.
    observed = pooled["cohens_kappa"]["observed_agreement"]
    stamped = round(stamp["agreement"]["rate"] or 0.0, 4)
    return {
        "beats_rubber_stamp": observed > stamped,
        "rubber_stamp_baseline": stamp,
        "population": ("every field decision, including the correct negatives: "
                       f"{len(gold_cases)} cases x {len(CRITICAL_FIELDS)} fields. "
                       "Not recall's gold-found denominator."),
        "cases_judged": sum(1 for r in rows if r.get("verdicts")),
        "cases_unreadable": sum(1 for r in rows if not r.get("verdicts")),
        "pooled": pooled,
        "per_field": {name: block({k for k in truth if k[1] == name})
                      for name in CRITICAL_FIELDS},
        "judge_abstentions": abstained,
        "confidence": metrics.confidence_separation(
            confidence_pairs(rows, truth, compress=False)),
        "rationale_chars": {
            "median": metrics.percentile(lengths, 50) if lengths else None,
            "p90": metrics.percentile(lengths, 90) if lengths else None,
            "over_cap": sum(1 for n in lengths if n > rubric.RATIONALE_CHAR_CAP),
            "cap": rubric.RATIONALE_CHAR_CAP,
        },
    }


def compression_effect(gold_cases: list[dict], gated: dict, rows: list[dict]) -> dict:
    """What the spec's central-tendency transform costs, on identical verdicts.

    Same judge, same calls, same verdicts: only the confidence numbers move, so
    the comparison is exact rather than sampled. Agreement is untouched by
    construction - compression changes no verdict - which is why the only thing
    reported here is what happens to the confidence signal.
    """
    truth = metrics.rater_decisions(gold_cases, gated)
    raw = metrics.confidence_separation(confidence_pairs(rows, truth, compress=False))
    squeezed = metrics.confidence_separation(confidence_pairs(rows, truth, compress=True))
    # A retention ratio is only meaningful when there was something to retain. If
    # the judge's raw confidence already separated nothing, dividing by it would
    # manufacture a percentage out of noise - and would hide the more important
    # finding, which is that the confidence signal was absent before compression
    # ever touched it.
    baseline_separation = raw["separation"] or 0.0
    measurable = abs(baseline_separation) >= 0.1
    kept = (metrics.frac(squeezed["separation"], baseline_separation)
            if measurable else None)
    return {
        "transform": "confidence clamped to [2, 4]; verdicts untouched",
        "raw": raw,
        "compressed": squeezed,
        "separation_retained": kept,
        "levels_lost": raw["distinct_levels_used"] - squeezed["distinct_levels_used"],
        "measurable_on_this_run": measurable,
        "reading": ("Compression cannot change agreement, because it changes no "
                    "verdict. It can only degrade the confidence signal - and it "
                    "can only be shown to do so on a run where that signal exists. "
                    + ("It does here, and the separation figures are the cost."
                       if measurable else
                       "It does not here: the judge's raw confidence separated "
                       "right from wrong by less than 0.1 of a scale point, so "
                       "this run cannot price the transform. That is a finding "
                       "about the judge, not a pass for the transform.")),
    }


def bias_report(baseline_rows: list[dict], probe_rows: list[dict], gold_cases,
                gated, label: str) -> dict:
    """Paired self-consistency: the same judge, the same cases, one thing changed.

    The unit is a field decision judged in both arms. A flip is the judge
    contradicting itself on identical evidence, so McNemar over the flips is the
    test, exactly as it is for two extraction runs (CLAUDE.md §5.6).
    """
    a, b = judge_decisions(baseline_rows), judge_decisions(probe_rows)
    shared = sorted(set(a) & set(b), key=lambda k: (k[0], k[1]))
    truth = metrics.rater_decisions(gold_cases, gated)
    to_right = sum(1 for k in shared
                   if not a[k] == int(bool(truth.get(k, 0))) and b[k] == int(bool(truth.get(k, 0))))
    to_wrong = sum(1 for k in shared
                   if a[k] == int(bool(truth.get(k, 0))) and not b[k] == int(bool(truth.get(k, 0))))
    disagreements = [k for k in shared if a[k] != b[k]]
    conf_a = {(r["case_id"], n): e["confidence"] for r in baseline_rows
              if r.get("verdicts") for n, e in r["verdicts"].items()}
    conf_b = {(r["case_id"], n): e["confidence"] for r in probe_rows
              if r.get("verdicts") for n, e in r["verdicts"].items()}
    len_a = sorted(len(e["rationale"]) for r in baseline_rows if r.get("verdicts")
                   for e in r["verdicts"].values())
    len_b = sorted(len(e["rationale"]) for r in probe_rows if r.get("verdicts")
                   for e in r["verdicts"].values())
    return {
        "probe": label,
        "asks": ARMS[label]["asks"],
        "decisions_in_both": len(shared),
        "self_consistency": metrics.frac(len(shared) - len(disagreements), len(shared)),
        "verdict_flips": len(disagreements),
        "flipped_fields": [f"{c}.{f}" for c, f in disagreements],
        "agreement_with_gold": metrics.mcnemar(to_right, to_wrong),
        "mean_confidence": {
            "baseline": (round(sum(conf_a[k] for k in shared) / len(shared), 3)
                         if shared else None),
            label: (round(sum(conf_b[k] for k in shared) / len(shared), 3)
                    if shared else None)},
        "median_rationale_chars": {
            "baseline": metrics.percentile(len_a, 50) if len_a else None,
            label: metrics.percentile(len_b, 50) if len_b else None},
        "cost_usd": {
            "baseline": round(sum(r["billed_usd"] for r in baseline_rows), 6),
            label: round(sum(r["billed_usd"] for r in probe_rows), 6)},
    }


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------

def render(result: dict) -> str:
    lines = []
    head = result["calibration"]
    pooled = head["pooled"]
    k, g = pooled["cohens_kappa"], pooled["gwet_ac1"]
    conf = head["confidence"]
    lines.append(f"judge      {result['judge']['model']}  "
                 f"fingerprint {result['judge']['fingerprint']}")
    lines.append(f"under test {result['under_test']['model']}  "
                 f"run {result['under_test']['run_id']}")
    lines.append(f"gold       {result['gold']['path']}  ({result['gold']['schema_version']})")
    lines.append(f"population {head['population']}")
    lines.append("")
    stamp = head["rubber_stamp_baseline"]
    verdict = "BEATS" if head["beats_rubber_stamp"] else "DOES NOT BEAT"
    lines.append(f"  observed agreement    {k['observed_agreement']:.4f}  "
                 f"on {pooled['table']['n']} decisions")
    lines.append(f"  rubber stamp ({stamp['strategy']})"
                 f"  {stamp['agreement']['text']}")
    lines.append(f"  -> the judge {verdict} a judge that objects to nothing")
    lines.append(f"  Cohen's kappa         {k['kappa']}   "
                 f"(chance {k['chance_agreement']:.4f})  95% CI {pooled['cohens_kappa_ci95']}")
    lines.append(f"  Gwet's AC1            {g['ac1']}   "
                 f"(chance {g['chance_agreement']:.4f})  95% CI {pooled['gwet_ac1_ci95']}")
    c = pooled["confusion"]
    lines.append("")
    lines.append(f"  both correct {c['both_call_it_correct']:>4}   "
                 f"both wrong {c['both_call_it_wrong']:>4}")
    lines.append(f"  judge WRONGLY ACCEPTS a bad extraction   {c['judge_wrongly_accepts']:>4}"
                 f"   {c['judge_false_accept_rate']['text']} of the extractions gold calls wrong")
    lines.append(f"  judge wrongly rejects a good extraction  {c['judge_wrongly_rejects']:>4}"
                 f"   {c['judge_false_reject_rate']['text']} of the ones gold calls right")
    lines.append("")
    lines.append(f"  confidence when it agreed with gold    {conf['mean_when_agreed']}")
    lines.append(f"  confidence when it did not            {conf['mean_when_disagreed']}")
    lines.append(f"  separation {conf['separation']}   point-biserial "
                 f"{conf['point_biserial']}   levels used {conf['distinct_levels_used']}/5")
    lines.append(f"  judge abstentions ('unclear')  {head['judge_abstentions']}"
                 f"   unreadable replies  {head['cases_unreadable']}")
    lines.append("")
    lines.append("  per field         n   agree   kappa     AC1   false-accepts")
    for name, block in head["per_field"].items():
        kk, gg = block["cohens_kappa"], block["gwet_ac1"]
        lines.append(f"  {name:<14}{block['table']['n']:>4}  "
                     f"{kk['observed_agreement']:.3f}  {str(kk['kappa']):>7}  "
                     f"{str(gg['ac1']):>6}   {block['confusion']['judge_wrongly_accepts']:>3}")
    for probe in result.get("bias_probes", []):
        lines.append("")
        lines.append(f"  probe {probe['probe']}: {probe['asks']}")
        lines.append(f"    self-consistency {probe['self_consistency']['text']}, "
                     f"{probe['verdict_flips']} verdict flips on identical evidence")
        mc = probe["agreement_with_gold"]
        lines.append(f"    agreement with gold: {mc['flipped_to_right']} better / "
                     f"{mc['flipped_to_wrong']} worse, McNemar exact p = {mc['p']:.4f}")
        lines.append(f"    median rationale {probe['median_rationale_chars']}")
        lines.append(f"    cost {probe['cost_usd']}")
    if result.get("compression"):
        comp = result["compression"]
        lines.append("")
        lines.append(f"  {DERIVED_ARM} arm ({comp['transform']}), no calls:")
        retained = (comp["separation_retained"]["text"]
                    if comp["separation_retained"] else "not measurable on this run")
        lines.append(f"    confidence separation {comp['raw']['separation']} -> "
                     f"{comp['compressed']['separation']}   retained {retained}")
        lines.append(f"    point-biserial {comp['raw']['point_biserial']} -> "
                     f"{comp['compressed']['point_biserial']}"
                     f"   scale levels lost {comp['levels_lost']}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def build_summary(run_dir: Path, gold_path: Path, gold: dict, cases: list[dict],
                  gated: dict, arms: dict, model: str) -> dict:
    manifest_path = run_dir / "manifest.json"
    manifest = (json.loads(manifest_path.read_text(encoding="utf-8"))
                if manifest_path.is_file() else {})
    baseline = arms.get("baseline") or []
    # The calibration is scored over the cases the BASELINE arm judged, never over
    # this invocation's case list. A bias probe is normally run with --limit, and
    # taking the case list from the current invocation would let `--arm verbose
    # --limit 20` quietly rewrite a 268-decision headline as an 80-decision one.
    # The probes stay restricted to their own shared decisions, which is correct
    # for them and wrong for the headline.
    judged_ids = {r["case_id"] for r in baseline}
    calibration_cases = [c for c in gold.get("cases", cases)
                         if c["case_id"] in judged_ids]
    result = {
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "judge": {"model": model, "prices_per_mtok": list(rubric.JUDGE_PRICES_PER_MTOK),
                  "rubric_version": rubric.RUBRIC_VERSION,
                  "fingerprint": rubric.judge_fingerprint("terse"),
                  "different_family_from_system_under_test": True},
        "under_test": {"run_id": manifest.get("run_id", run_dir.name),
                       "model": manifest.get("model"),
                       "prompt_fingerprint": (manifest.get("prompt") or {}).get("fingerprint"),
                       "path": _rel(run_dir)},
        "gold": {"path": _rel(gold_path), "schema_version": gold.get("schema_version"),
                 "cases_in_this_invocation": len(cases),
                 "cases_in_the_calibration": len(calibration_cases)},
        "arms": {name: {"calls": len(rows), "asks": ARMS[name]["asks"],
                        "billed_usd": round(sum(r["billed_usd"] for r in rows), 6)}
                 for name, rows in arms.items()},
        "calibration": (calibrate(calibration_cases, gated, baseline)
                        if baseline else None),
        "bias_probes": [bias_report(baseline, rows, cases, gated, name)
                        for name, rows in arms.items()
                        if name != "baseline" and baseline and rows],
        "compression": (compression_effect(calibration_cases, gated, baseline)
                        if baseline else None),
    }
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("run_dir", type=Path, nargs="?",
                        help="a run-shaped directory holding the candidate extractions")
    parser.add_argument("--gold", type=Path, default=DEFAULT_GOLD)
    parser.add_argument("--arm", choices=sorted(ARMS), default="baseline")
    parser.add_argument("--limit", type=int, default=None,
                        help="judge only the first N cases (the bias probes do not "
                             "need the full corpus; the calibration does)")
    parser.add_argument("--model", default=rubric.JUDGE_MODEL)
    parser.add_argument("--cap-usd", type=float, default=0.06,
                        help="this invocation's ceiling, checked against worst case "
                             "before every call")
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--dry-run", action="store_true",
                        help="print the prompt and the cost arithmetic; spend nothing")
    parser.add_argument("--report", type=Path, default=None,
                        help="re-read saved verdicts and re-print the statistics, "
                             "making no calls")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args(argv)

    try:
        if args.report:
            # Recomputed from the saved verdicts, never re-read from the saved
            # summary. A statistic that is only ever read back cannot be checked
            # against the rows it came from, and this one was wrong once: a
            # `--limit 20` probe had rewritten a 268-decision headline as an
            # 80-decision one. Rebuilding makes that impossible to miss, and it
            # costs nothing - no arm is called.
            arms = {}
            for name in ARMS:
                saved = args.report / name / "verdicts.jsonl"
                if saved.is_file():
                    arms[name] = [json.loads(line) for line in
                                  saved.read_text(encoding="utf-8").splitlines() if line]
            if not arms:
                print(f"REFUSED: no saved verdicts under {_rel(args.report)}",
                      file=sys.stderr)
                return EXIT_UNUSABLE
            previous = json.loads(
                (args.report / "calibration.json").read_text(encoding="utf-8"))
            run_dir = ROOT / previous["under_test"]["path"]
            gold_path = args.gold if args.gold.is_file() else ROOT / previous["gold"]["path"]
            gold = json.loads(gold_path.read_text(encoding="utf-8"))
            gated = load_jsonl(run_dir / "gated.jsonl")
            cases = [c for c in gold["cases"] if c["case_id"] in gated]
            summary = build_summary(run_dir, gold_path, gold, cases, gated, arms,
                                    previous["judge"]["model"])
            (args.report / "calibration.json").write_text(
                json.dumps(summary, indent=2) + "\n", encoding="utf-8")
            print(render(summary), file=sys.stderr)
            sys.stdout.write(json.dumps(summary, indent=2) + "\n")
            return EXIT_OK

        if args.run_dir is None:
            print("REFUSED: give a run directory, or --report a saved calibration",
                  file=sys.stderr)
            return EXIT_UNUSABLE
        gated_path = args.run_dir / "gated.jsonl"
        if not gated_path.is_file() or not args.gold.is_file():
            print(f"REFUSED: missing {_rel(gated_path)} or {_rel(args.gold)}",
                  file=sys.stderr)
            return EXIT_UNUSABLE

        gold = json.loads(args.gold.read_text(encoding="utf-8"))
        gated = load_jsonl(gated_path)
        cases = [c for c in gold["cases"] if c["case_id"] in gated]
        if args.limit:
            cases = cases[:args.limit]
        if not cases:
            print("REFUSED: no case appears in both the gold set and the run",
                  file=sys.stderr)
            return EXIT_UNUSABLE

        prices = rubric.JUDGE_PRICES_PER_MTOK
        out_dir = args.out or (RESULTS_DIR / f"judge-{args.run_dir.name}")
        arm_dir = out_dir / args.arm

        if args.dry_run:
            sample = cases[0]
            request = rubric.build_request(
                sample["source_text"], gated[sample["case_id"]]["extraction"],
                model=args.model, order=ARMS[args.arm]["order"],
                brevity=ARMS[args.arm]["brevity"])
            per_call = worst_case_one(request["messages"],
                                      request["max_tokens"], prices)
            print("\n".join(m["content"] for m in request["messages"]), file=sys.stderr)
            print(f"\n--- arm {args.arm}: {ARMS[args.arm]['asks']}", file=sys.stderr)
            print(f"--- judge {args.model}  fingerprint "
                  f"{rubric.judge_fingerprint(ARMS[args.arm]['brevity'])}", file=sys.stderr)
            print(f"--- {len(cases)} cases, worst case {spend_guard.money(per_call)} each, "
                  f"{spend_guard.money(per_call * len(cases))} total "
                  f"(cap {spend_guard.money(args.cap_usd)})", file=sys.stderr)
            sys.stdout.write(json.dumps({
                "dry_run": True, "arm": args.arm, "cases": len(cases),
                "judge": args.model, "worst_case_usd": round(per_call * len(cases), 6),
                "cap_usd": args.cap_usd, "spent_usd": 0.0}, indent=2) + "\n")
            return EXIT_OK

        run_id = f"judge-{args.arm}-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
        ledger = extract.SpendLedger()
        guard = spend_guard.CostGuard(
            run_id=run_id, cap_usd=args.cap_usd, ledger=ledger,
            ceiling_usd=extract.resolve_budget(None), expected_calls=len(cases),
            # This script talks to the endpoint directly instead of going through
            # extract.call_model, so it is the only writer for these calls and must
            # say so or its spend never reaches the $8 ceiling (CLAUDE.md §5.3).
            records_to_ledger=True)

        rows = run_arm(args.arm, cases, gated, guard=guard, model=args.model,
                       prices=prices)
        if all(not r["parsed"] for r in rows):
            print(f"ERROR ERR_JUDGE_SCHEMA (exit {EXIT_SCHEMA}): no reply in this arm "
                  "could be read against the schema", file=sys.stderr)
            return EXIT_SCHEMA

        arm_dir.mkdir(parents=True, exist_ok=True)
        (arm_dir / "verdicts.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        guard.write_summary(arm_dir / "spend.json")

        arms = {}
        for name in ARMS:
            saved = out_dir / name / "verdicts.jsonl"
            if name == args.arm:
                arms[name] = rows
            elif saved.is_file():
                arms[name] = [json.loads(line) for line in
                              saved.read_text(encoding="utf-8").splitlines() if line]
        summary = build_summary(args.run_dir, args.gold, gold, cases, gated,
                               arms, args.model)
        (out_dir / "calibration.json").write_text(
            json.dumps(summary, indent=2) + "\n", encoding="utf-8")

        if summary["calibration"]:
            print(render(summary), file=sys.stderr)
        guard.print_summary()
        sys.stdout.write(json.dumps(summary, indent=2) + "\n")
        return EXIT_OK

    except spend_guard.RunCapExceeded as exc:
        print(f"ERROR ERR_RUN_CAP (exit {EXIT_BUDGET}): {exc}", file=sys.stderr)
        return EXIT_BUDGET
    except extract.BudgetExceeded as exc:
        print(f"ERROR ERR_BUDGET_EXCEEDED (exit {EXIT_BUDGET}): {exc}", file=sys.stderr)
        return EXIT_BUDGET
    except extract.PipelineError as exc:
        print(f"ERROR {exc.code} (exit {EXIT_UPSTREAM}): {exc}", file=sys.stderr)
        return EXIT_UPSTREAM
    except (ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"REFUSED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_UNUSABLE
    except Exception as exc:
        print(f"ERROR ERR_INTERNAL (exit {EXIT_INTERNAL}): {type(exc).__name__}",
              file=sys.stderr)
        if args.debug:
            traceback.print_exc()
        return EXIT_INTERNAL


if __name__ == "__main__":
    sys.exit(main())
