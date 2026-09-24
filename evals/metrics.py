#!/usr/bin/env python3
"""WHAT A RUN ACTUALLY MEASURED — populations, layers, and whether a gap is real.

    ./.venv/bin/python evals/metrics.py evals/results/<run_id>
    ./.venv/bin/python evals/metrics.py evals/results/<run_a> evals/results/<run_b>

Free. Pure functions over artefacts already on disk. No network, no key, no
tokens, nothing written anywhere. Enforcement lives in `evals/spend_guard.py`;
this module only reads. A module that can refuse a call should not also be the
one that scores it.

## Why this exists, and why it exists late

`evals/run_ekacare.py` answers "how good is it". `evals/diagnose.py` answers
"why is that number what it is". Neither answers the question that decides
whether a change was an improvement at all:

> **How far apart must two of these numbers sit before the gap means anything?**

The prompt correction on 2026-09-20 moved pooled recall from 0.600 to 0.645 and
was reported as a 4.5-point improvement. At n = 155 labelled fields that gap may
be indistinguishable from re-running the same system, and a recall figure quoted
without that check is one sample of a noisy process presented as a finding. So
this module reports:

  - **populations**, which must sum to the case count: cases scored, cases whose
    payload was an error envelope, and cases our own code refused to send. Only
    the first can be read as a statement about the model.
  - **layer ownership** for every failure, reusing `diagnose.attribute` rather
    than re-deriving it. A second implementation of the same comparison is a
    second answer to the same question.
  - **cost per correct field**, not cost per call. Cost per call flatters a
    system that fails cheaply.
  - **separability**: paired McNemar between two runs over the same gold set,
    and an unpaired two-proportion test when the case sets differ.

A large p-value here does **not** mean two systems are equally good. It means
this experiment was too small to tell, which is a statement about the experiment
rather than about the systems. Report it that way.

Standard library only, so `math.erfc` gives the normal tail and there is nothing
to install.
"""

import argparse
import collections
import json
import math
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "evals"))

import diagnose  # noqa: E402  (its attribution, so causes cannot drift apart)
from extract import CRITICAL_FIELDS  # noqa: E402
from scoring import value_matches, wilson  # noqa: E402

SEALED_PATH = ROOT / "data" / "gold_labels" / "gold_v1.json"
RESULTS_DIR = ROOT / "evals" / "results"

EXIT_OK = 0
EXIT_UNUSABLE = 2
EXIT_INTERNAL = 6

# Which layer owns the fix. A count with no owner is an observation; a count
# with an owner is a work item. Keyed off diagnose.CAUSES so the two files
# cannot disagree about what a cause is.
LAYER = {
    "CORRECT": "-",
    "CORRECT_NEGATIVE": "-",
    "MED_STRENGTH_APPENDED": "prompt (field rules contradicted each other)",
    "MED_DIFFERENT_DRUG": "schema (one slot, a median of three drugs)",
    "MED_MODEL_SILENT": "model",
    "MED_GOLD_SILENT": "labels or model (disputed)",
    "DOSE_GOLD_NOT_A_STRENGTH": "labels (the label breaks its own field rule)",
    "DOSE_OTHER_DRUG": "schema (cascade of the drug choice)",
    "DOSE_MODEL_SILENT": "model",
    "DOSE_GATE_BLANKED": "gate (correct on inspection; the label is the defect)",
    "DOSE_GOLD_SILENT": "labels or model (disputed)",
    "FREQ_PHRASE_LENGTH": "labels (food timing the field rule excludes)",
    "FREQ_DIFFERENT_REGIMEN": "schema (cascade of the drug choice)",
    "FREQ_MODEL_SILENT": "model",
    "FREQ_GOLD_SILENT": "labels or model (disputed)",
    "ALLERGY_MISMATCH": "corpus (no allergy positives exist)",
}


# =====================================================================
# FRACTIONS THAT DO NOT OVERCLAIM
# =====================================================================
def frac(n, d):
    """A fraction that never pretends to be a rate it cannot support."""
    return {"n": n, "d": d,
            "rate": (float(n) / d) if d else None,
            "text": ("%d/%d (%.1f%%)" % (n, d, 100.0 * n / d)) if d
                    else "%d/0 (n/a)" % n}


def percentile(values, p):
    """Nearest-rank percentile, round-half-up. None, never 0, for an empty list.

    The convention is stated because there are three in common use and they
    disagree on small samples: the 90th percentile of 1..10 is 10 here, and 9
    under the ceiling convention. This is the definition for the whole project;
    `spend_guard.py` repeats the expression rather than importing it, so that
    enforcement never depends on measurement, and says so there.
    """
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, int(round(p / 100.0 * len(ordered) + 0.5)))
    return ordered[min(rank, len(ordered)) - 1]


# =====================================================================
# THE SPLIT — and it must sum to the case count
# =====================================================================
def split(gold_cases, gated, ungated):
    """Three populations. Only `scored` says anything about the model.

    `refused` is this project's equivalent of a run our own code stopped: the
    note carried hidden or look-alike characters, so the encoding gate wiped
    every field and no call was ever made. Counting those as model abstentions
    would report the model declining something it never saw.
    """
    scored, errored, refused = [], [], []
    for case in gold_cases:
        case_id = case["case_id"]
        run, raw = gated.get(case_id), ungated.get(case_id)
        if run is None:
            continue
        if run.get("status") != "ok" or not raw or raw.get("status") != "ok":
            errored.append(case_id)
        elif (run.get("gate")
              and all(g["code"] == "ABSTAIN_ENCODING_ANOMALY" for g in run["gate"])):
            refused.append(case_id)
        else:
            scored.append(case_id)
    return {"scored": scored, "errored": errored, "refused": refused,
            "total": len(gold_cases)}


# =====================================================================
# FIELD QUALITY — scored cases only
# =====================================================================
def field_quality(gold_cases, gated, scored_ids=None):
    """Per-field precision, recall and F1, plus macro-F1 over the four fields.

    Definitions are `scoring.py`'s, deliberately and exactly: a field is
    *proposed* when the gates let it through (VERIFIED or REVIEW), *correct*
    when it is proposed, gold says found, and the values match under the
    pre-registered `scoring.value_matches`; **precision is correct / proposed**
    and **recall is correct / gold-found**.

    The textbook `tp / (tp + fn)` is wrong for this task and was wrong here
    first. A field that is proposed but wrong is not only a false positive - it
    is also a gold fact the physician did not get - so counting it solely as a
    false positive made medication recall read 100% while `scores.json` said
    69.6%. Two implementations of one metric are two answers to one question,
    and the pre-registered one wins. Macro-F1 is all this module adds.
    """
    scored_ids = set(scored_ids) if scored_ids is not None else None
    per_field, f1s = {}, []
    for field in CRITICAL_FIELDS:
        tp = proposed_n = gold_found = 0
        for case in gold_cases:
            case_id = case["case_id"]
            if case_id not in gated or (scored_ids is not None
                                        and case_id not in scored_ids):
                continue
            truth = case["ground_truth"][field]
            code = {g["field"]: g["code"] for g in gated[case_id]["gate"]}[field]
            proposed = code == "VERIFIED" or code.startswith("REVIEW")
            value = gated[case_id]["extraction"][field]["value"] or ""
            right = (truth["status"] == "found"
                     and value_matches(field, value, truth["value"] or ""))
            tp += bool(proposed and right)
            proposed_n += bool(proposed)
            gold_found += truth["status"] == "found"
        precision, recall = frac(tp, proposed_n), frac(tp, gold_found)
        p, r = precision["rate"], recall["rate"]
        f1 = (2 * p * r / (p + r)) if (p and r) else (
            0.0 if (p is not None and r is not None) else None)
        per_field[field] = {"support": gold_found, "precision": precision,
                            "recall": recall, "f1": f1}
        if f1 is not None:
            f1s.append(f1)
    return {"per_field": per_field,
            "macro_f1": (sum(f1s) / len(f1s)) if f1s else None}


# =====================================================================
# RUN SHAPE — what a CORRECT field cost
# =====================================================================
def run_shape(gold_cases, gated, correct_fields):
    """Tokens, dollars and latency, with cost expressed per correct field.

    `cost_per_correct_field` is the number to quote. Cost per call flatters a
    system that fails cheaply, and the two differ by however wrong the system
    is: at 0.645 recall a correct field costs roughly 1.5x a call.
    """
    served = [p for p in gated.values() if (p.get("provenance") or {}).get("called")]
    api = [p["latency_ms"]["api"] for p in served]
    billed = sum((p.get("usage") or {}).get("billed_usd", 0.0) or 0.0 for p in served)
    budget = next((p.get("latency_budget_ms") for p in gated.values()
                   if p.get("latency_budget_ms")), None)
    inside = sum(1 for p in served
                 if budget and (p["latency_ms"]["api"]
                                + (p["latency_ms"].get("local") or 0.0)) < budget)
    return {
        "calls": len(served),
        "tokens_in": sum((p.get("usage") or {}).get("input_tokens", 0) or 0 for p in served),
        "tokens_out": sum((p.get("usage") or {}).get("output_tokens", 0) or 0 for p in served),
        "tokens_cached": sum((p.get("usage") or {}).get("cached_tokens", 0) or 0
                             for p in served),
        "cost_usd": round(billed, 6),
        "cost_per_call": round(billed / len(served), 6) if served else None,
        # None, never 0: a run that got nothing right has no cost per correct
        # field, and 0.0 there would read as "free and correct".
        "cost_per_correct_field": round(billed / correct_fields, 6) if correct_fields else None,
        # `median` is statistics.median; the tails are nearest-rank round-half-up
        # (percentile above). The key is named for the convention rather than
        # called p50, because under nearest-rank the 50th percentile of an even
        # sample is the upper middle value and not the median.
        "latency_ms": {"median": statistics.median(api) if api else None,
                       "p90": percentile(api, 90), "p95": percentile(api, 95),
                       "max": max(api) if api else None},
        "latency_budget_ms": budget,
        "inside_budget": frac(inside, len(served)),
    }


# =====================================================================
# FAILURE TAXONOMY — and the layer that owns each bucket
# =====================================================================
def failure_taxonomy(gold_cases, gated, ungated, scored_ids=None):
    """Every failing field bucketed by cause, with the layer that owns the fix.

    Causes come from `diagnose.attribute`, not from a second implementation
    here. Two implementations of the same comparison are two answers to the
    same question, and the one in `diagnose.py` is the one the tests pin.
    """
    scored_ids = set(scored_ids) if scored_ids is not None else None
    disagreed = diagnose_disagreements(gold_cases, gated)
    buckets, examples = collections.Counter(), {}
    for case in gold_cases:
        case_id = case["case_id"]
        if case_id not in gated or case_id not in ungated:
            continue
        if scored_ids is not None and case_id not in scored_ids:
            continue
        for field in CRITICAL_FIELDS:
            cause = diagnose.attribute(case, field, gated[case_id], ungated[case_id],
                                       case_id in disagreed)
            if cause.startswith("CORRECT"):
                continue
            buckets[cause] += 1
            if cause not in examples:
                truth = case["ground_truth"][field]
                got = gated[case_id]["extraction"][field]["value"]
                examples[cause] = f"{case_id} {field}: gold {truth['value']!r} vs {got!r}"
    by_layer = collections.Counter()
    for cause, count in buckets.items():
        by_layer[LAYER.get(cause, "unclassified")] += count
    return {
        "failed_fields": sum(buckets.values()),
        "buckets": [{"cause": cause, "count": count,
                     "layer": LAYER.get(cause, "unclassified"),
                     "example": examples.get(cause, "")}
                    for cause, count in buckets.most_common()],
        "by_layer": dict(by_layer.most_common()),
    }


def diagnose_disagreements(gold_cases, gated):
    """Cases where the labeller and the model named different drugs."""
    out = set()
    for case in gold_cases:
        case_id = case["case_id"]
        if case_id not in gated:
            continue
        truth = case["ground_truth"]["medication"]
        if truth["status"] != "found":
            continue
        value = gated[case_id]["extraction"]["medication"]["value"] or ""
        gold_value = truth["value"] or ""
        if value and not value_matches("medication", value, gold_value) and \
                diagnose.medication_core(value) != diagnose.medication_core(gold_value):
            out.add(case_id)
    return out


# =====================================================================
# COST PROVENANCE — measured versus estimated, never blended
# =====================================================================
def cost_provenance(gated):
    """What the provider charged versus what the price table predicted.

    A figure built from a mixture of the two is neither, so they are reported
    apart. A drift beyond rounding means the price table is stale, and a stale
    price table makes the spend ceiling a guess.
    """
    served = [p for p in gated.values() if (p.get("provenance") or {}).get("called")]
    measured = [p for p in served
                if (p.get("usage") or {}).get("provider_cost_usd") is not None]
    reported = sum((p["usage"]["provider_cost_usd"] or 0.0) for p in measured)
    estimate = sum((p["usage"].get("est_cost_usd") or 0.0) for p in measured)
    return {"calls_priced_by_provider": frac(len(measured), len(served)),
            "provider_usd": round(reported, 6),
            "estimate_usd": round(estimate, 6),
            "drift_usd": round(reported - estimate, 6),
            "all_measured": bool(served) and len(measured) == len(served)}


# =====================================================================
# HOW MUCH OF A DIFFERENCE IS REAL
# =====================================================================
def wilson_interval(passes, trials, z=1.96):
    """95% Wilson interval, delegated to the pre-registered implementation.

    `scoring.wilson` is the one every reported number already uses. Writing a
    second one here would mean two intervals for the same count.
    """
    if not trials:
        return (None, None)
    return wilson(passes, trials, z)


def two_proportion_p(passes_a, trials_a, passes_b, trials_b):
    """Two-sided p for 'these two rates are the same'. Unpaired.

    Use when the two arms did not see the same cases - the regex baseline on its
    held-out 47 against the model on 67, for instance. When they did see the
    same cases, `mcnemar` is both correct and far more powerful, because it
    looks at which fields changed rather than at two summary rates.
    """
    if not trials_a or not trials_b:
        return (0.0, 1.0)
    p_a, p_b = passes_a / trials_a, passes_b / trials_b
    pooled = (passes_a + passes_b) / (trials_a + trials_b)
    se = math.sqrt(pooled * (1.0 - pooled) * (1.0 / trials_a + 1.0 / trials_b))
    if se == 0:
        return (0.0, 1.0)
    z = (p_a - p_b) / se
    return (z, math.erfc(abs(z) / math.sqrt(2.0)))


def fisher_exact(passes_a, trials_a, passes_b, trials_b):
    """Two-sided Fisher exact p for the 2x2 table, computed not typed.

    This is the **wrong** test for the leakage comparison and is reported anyway,
    because an evaluator handed two rates will reach for it. It assumes the two
    arms are independent samples; they are the same notes under two conditions,
    so it discards the pairing and answers a question nobody asked. `mcnemar` is
    the applicable test.

    It exists as a function rather than as a number in a document because the
    first hand-computed version of these values was wrong by up to 0.06 - which
    is exactly the misreading that reporting Fisher's was meant to prevent.
    """
    a, b = passes_a, trials_a - passes_a
    c, d = passes_b, trials_b - passes_b
    if min(a, b, c, d) < 0 or (a + b) == 0 or (c + d) == 0:
        return 1.0
    total, row1, row2, col1 = a + b + c + d, a + b, c + d, a + c

    def probability(x):
        return (math.comb(row1, x) * math.comb(row2, col1 - x)) / math.comb(total, col1)

    observed = probability(a)
    low, high = max(0, col1 - row2), min(row1, col1)
    return min(1.0, sum(probability(x) for x in range(low, high + 1)
                        if probability(x) <= observed + 1e-12))


def _binomial_two_sided(k, n):
    """Exact two-sided binomial p at p=0.5. No scipy, and honest at small n,
    where the chi-square form of McNemar is not."""
    if n == 0:
        return 1.0
    tail = sum(math.comb(n, i) for i in range(0, min(k, n - k) + 1)) / (2.0 ** n)
    return min(1.0, 2.0 * tail)


def mcnemar(flips_to_right, flips_to_wrong):
    """Exact McNemar for two systems over the same fields.

    The two runs are scored on the same 67 cases against the same labels, so the
    samples are paired and only the fields that CHANGED carry information. A
    field both runs got right says nothing about which is better. `b` is
    wrong -> right, `c` is right -> wrong; under "no difference" each flip is a
    coin toss, so the exact binomial is the test.
    """
    b, c = flips_to_right, flips_to_wrong
    return {"flipped_to_right": b, "flipped_to_wrong": c, "discordant": b + c,
            "p": _binomial_two_sided(min(b, c), b + c),
            "net": b - c}


def rater_decisions(gold_cases, gated):
    """(case_id, field) -> was the pipeline right, over EVERY field decision.

    A different population from `field_outcomes`, on purpose, and the difference
    matters enough to say twice. `field_outcomes` keeps only the fields gold marks
    `found`, because that is recall's denominator. Calibrating a judge needs the
    correct negatives too: most of a judge's working day is agreeing that a note
    says nothing about allergies, and a judge that cannot do that is useless
    however well it scores on the fields that carry a value.

    So this is 67 x 4 = 268 decisions where recall is scored on 155, and an
    agreement figure computed here must never be set beside a recall figure
    computed there.
    """
    out = {}
    for case in gold_cases:
        case_id = case["case_id"]
        if case_id not in gated:
            continue
        extraction = gated[case_id]["extraction"]
        for field in CRITICAL_FIELDS:
            truth = case["ground_truth"][field]
            value = extraction[field]["value"] or ""
            if truth["status"] == "found":
                out[(case_id, field)] = bool(
                    value and value_matches(field, value, truth["value"] or ""))
            else:
                # Gold says nothing is there. Staying silent is the right answer;
                # proposing anything at all is wrong.
                out[(case_id, field)] = not value
    return out


def agreement_table(rater_a, rater_b):
    """The 2x2 over the keys both raters decided. Keys only one of them covers
    are dropped and counted, never treated as a disagreement."""
    shared = sorted(set(rater_a) & set(rater_b), key=lambda k: (str(k[0]), str(k[1])))
    table = {"n11": 0, "n10": 0, "n01": 0, "n00": 0}
    for key in shared:
        a, b = bool(rater_a[key]), bool(rater_b[key])
        table["n11" if (a and b) else "n10" if a else "n01" if b else "n00"] += 1
    table["n"] = len(shared)
    table["a_only"] = len(set(rater_a) - set(rater_b))
    table["b_only"] = len(set(rater_b) - set(rater_a))
    return table


def _proportions(table):
    n = table["n"]
    if not n:
        return None
    observed = (table["n11"] + table["n00"]) / n
    p_a = (table["n11"] + table["n10"]) / n      # rater A says 1
    p_b = (table["n11"] + table["n01"]) / n      # rater B says 1
    return observed, p_a, p_b


def cohens_kappa(table):
    """Cohen's kappa on a 2x2, with the term that makes it misleading here.

    kappa corrects observed agreement by the agreement two raters would reach by
    chance *given their own marginals*, and that correction grows as the marginals
    become lopsided. When both raters say "correct" to nearly everything - which
    is what a working extractor and a competent judge both do - chance agreement
    approaches the observed agreement and kappa collapses toward 0 while the
    raters are in fact agreeing on almost every decision. That is the kappa
    paradox, it is a property of the statistic and not of the raters, and it is
    why `gwet_ac1` is reported beside this and not instead of it.

    `chance_agreement` is returned so the collapse is visible rather than
    inferred from a small number.
    """
    parts = _proportions(table)
    if parts is None:
        return None
    observed, p_a, p_b = parts
    expected = p_a * p_b + (1 - p_a) * (1 - p_b)
    return {
        "observed_agreement": round(observed, 4),
        "chance_agreement": round(expected, 4),
        "kappa": (None if expected >= 1.0
                  else round((observed - expected) / (1 - expected), 4)),
        "prevalence_of_correct": {"rater_a": round(p_a, 4), "rater_b": round(p_b, 4)},
    }


def gwet_ac1(table):
    """Gwet's AC1: the same observed agreement against a chance term that does
    not depend on how lopsided the marginals are.

    Chance agreement is 2*pi*(1-pi) where pi is the prevalence of "correct"
    averaged over the two raters - maximal at pi = 0.5 and approaching 0 as one
    category takes over. Where kappa punishes a high-prevalence category, AC1
    reports that there was little left to agree on by accident. For this project
    it is the defensible headline and kappa is the number an evaluator will look
    for, so both are published with the chance terms that separate them.
    """
    parts = _proportions(table)
    if parts is None:
        return None
    observed, p_a, p_b = parts
    pi = (p_a + p_b) / 2
    expected = 2 * pi * (1 - pi)
    return {
        "observed_agreement": round(observed, 4),
        "chance_agreement": round(expected, 4),
        "ac1": (None if expected >= 1.0
                else round((observed - expected) / (1 - expected), 4)),
        "prevalence_of_correct": round(pi, 4),
    }


def bootstrap_interval(rater_a, rater_b, statistic, *, draws=2000, seed=0,
                       alpha=0.05):
    """Percentile bootstrap over the paired decisions.

    kappa and AC1 have asymptotic variance formulas; resampling the pairs is
    assumption-light, correct at n = 268, and - seeded - reproducible, which the
    formulas typed from memory would not be. `statistic` takes a table and returns
    a float or None.
    """
    import random
    shared = sorted(set(rater_a) & set(rater_b), key=lambda k: (str(k[0]), str(k[1])))
    if len(shared) < 2:
        return None
    rng = random.Random(seed)
    values = []
    for _ in range(draws):
        picked = [shared[rng.randrange(len(shared))] for _ in shared]
        # Counted by multiplicity rather than through `agreement_table`: a resample
        # draws the same key more than once, and building it from dicts would
        # collapse the repeats and silently shrink n.
        table = {"n11": 0, "n10": 0, "n01": 0, "n00": 0, "n": 0,
                 "a_only": 0, "b_only": 0}
        for key in picked:
            a, b = bool(rater_a[key]), bool(rater_b[key])
            table["n11" if (a and b) else "n10" if a else "n01" if b else "n00"] += 1
            table["n"] += 1
        got = statistic(table)
        if got is not None:
            values.append(got)
    if not values:
        return None
    values.sort()
    lo = values[max(0, int(alpha / 2 * len(values)) - 1)]
    hi = values[min(len(values) - 1, int((1 - alpha / 2) * len(values)))]
    return (round(lo, 4), round(hi, 4))


def confidence_separation(pairs):
    """Does the judge's confidence track whether it was right?

    `pairs` is (confidence, judge_agreed_with_gold). A calibrated judge is more
    confident when it agrees with the labels than when it does not; the gap
    between those two means is the whole value of asking for a confidence at all.
    Reported as a mean difference plus the point-biserial correlation, because a
    single correlation hides which side moved.
    """
    hits = [c for c, ok in pairs if ok]
    misses = [c for c, ok in pairs if not ok]
    if not hits or not misses:
        return {"n": len(pairs), "mean_when_agreed": None,
                "mean_when_disagreed": None, "separation": None,
                "point_biserial": None, "distinct_levels_used": len({c for c, _ in pairs})}
    mean_hit = sum(hits) / len(hits)
    mean_miss = sum(misses) / len(misses)
    scores = [c for c, _ in pairs]
    n = len(scores)
    mean_all = sum(scores) / n
    sd = (sum((c - mean_all) ** 2 for c in scores) / n) ** 0.5
    p = len(hits) / n
    r_pb = (None if sd == 0 else
            round((mean_hit - mean_miss) / sd * math.sqrt(p * (1 - p)), 4))
    return {
        "n": n,
        "mean_when_agreed": round(mean_hit, 3),
        "mean_when_disagreed": round(mean_miss, 3),
        "separation": round(mean_hit - mean_miss, 3),
        "point_biserial": r_pb,
        "distinct_levels_used": len(set(scores)),
        "sd": round(sd, 3),
    }


def field_outcomes(gold_cases, gated):
    """(case_id, field) -> was it correct. The unit both comparisons need."""
    out = {}
    for case in gold_cases:
        case_id = case["case_id"]
        if case_id not in gated:
            continue
        codes = {g["field"]: g["code"] for g in gated[case_id]["gate"]}
        for field in CRITICAL_FIELDS:
            truth = case["ground_truth"][field]
            if truth["status"] != "found":
                continue          # recall's denominator, and the only paired unit
            code = codes[field]
            proposed = code == "VERIFIED" or code.startswith("REVIEW")
            value = gated[case_id]["extraction"][field]["value"] or ""
            out[(case_id, field)] = bool(
                proposed and value_matches(field, value, truth["value"] or ""))
    return out


def compare(gold_cases, gated_a, gated_b, label_a="A", label_b="B"):
    """Is B's recall really different from A's? Paired where possible.

    Reported per field as well as pooled, because a change can be real in one
    field and noise overall - which is exactly what the 2026-09-20 prompt
    correction turned out to be.
    """
    a, b = field_outcomes(gold_cases, gated_a), field_outcomes(gold_cases, gated_b)
    shared = sorted(set(a) & set(b))
    rows = {}
    for scope in ("pooled",) + tuple(CRITICAL_FIELDS):
        keys = [k for k in shared if scope == "pooled" or k[1] == scope]
        if not keys:
            continue
        ka, kb = sum(a[k] for k in keys), sum(b[k] for k in keys)
        to_right = sum(1 for k in keys if b[k] and not a[k])
        to_wrong = sum(1 for k in keys if a[k] and not b[k])
        test = mcnemar(to_right, to_wrong)
        rows[scope] = {
            "n": len(keys),
            # Keyed "a"/"b", not by label: two runs of the same prompt share a
            # fingerprint, and a dict keyed by label then collapses to one column
            # with the second silently overwriting the first.
            "a": frac(ka, len(keys)), "b": frac(kb, len(keys)),
            "delta_pp": round(100.0 * (kb - ka) / len(keys), 1),
            "mcnemar": test,
            "separated": test["p"] < 0.05,
            # Reported because a reader will compute it; it is not the applicable
            # test here, because these arms are paired. See fisher_exact.
            "fisher_unpaired_p": fisher_exact(ka, len(keys), kb, len(keys)),
            "ci95_a": wilson_interval(ka, len(keys)),
            "ci95_b": wilson_interval(kb, len(keys)),
        }
    return {"paired_fields": len(shared), "labels": [label_a, label_b], "scopes": rows}


def compare_golds(gated, gold_a_cases, gold_b_cases, label_a="gold-a", label_b="gold-b"):
    """One run's saved output, scored against two gold versions.

    `compare` holds the gold fixed and varies the run; this holds the run fixed
    and varies the gold, which isolates the effect of a label correction with
    **zero sampling noise** - no model is called, so nothing here can be the
    provider behaving differently on a second attempt.

    The pairing unit is a field that is `found` in **both** versions. A field
    whose status changed leaves the denominator rather than counting as a flip:
    treating a withdrawn label as a system regression would invent a difference
    that is entirely ours. Those are reported separately, and they are the whole
    point of a correction pass - gold-v2 withdraws seven dose labels because the
    dose rule says a quantity is not a dose.
    """
    a_by_id = {case["case_id"]: case for case in gold_a_cases}
    b_by_id = {case["case_id"]: case for case in gold_b_cases}
    rows, withdrawn, added = {}, [], []

    def correct(case, field, run):
        truth = case["ground_truth"][field]
        codes = {g["field"]: g["code"] for g in run["gate"]}
        code = codes[field]
        if not (code == "VERIFIED" or code.startswith("REVIEW")):
            return False
        value = run["extraction"][field]["value"] or ""
        return bool(value_matches(field, value, truth["value"] or ""))

    paired = {}
    for case_id, case_a in a_by_id.items():
        case_b, run = b_by_id.get(case_id), gated.get(case_id)
        if case_b is None or run is None or run.get("status") != "ok":
            continue
        for field in CRITICAL_FIELDS:
            found_a = case_a["ground_truth"][field]["status"] == "found"
            found_b = case_b["ground_truth"][field]["status"] == "found"
            if found_a and not found_b:
                withdrawn.append(f"{case_id}.{field}")
            elif found_b and not found_a:
                added.append(f"{case_id}.{field}")
            elif found_a and found_b:
                paired[(case_id, field)] = (correct(case_a, field, run),
                                            correct(case_b, field, run))

    for scope in ("pooled",) + tuple(CRITICAL_FIELDS):
        keys = [k for k in paired if scope == "pooled" or k[1] == scope]
        if not keys:
            continue
        ka = sum(paired[k][0] for k in keys)
        kb = sum(paired[k][1] for k in keys)
        to_right = sum(1 for k in keys if paired[k][1] and not paired[k][0])
        to_wrong = sum(1 for k in keys if paired[k][0] and not paired[k][1])
        test = mcnemar(to_right, to_wrong)
        rows[scope] = {
            "n": len(keys),
            "a": frac(ka, len(keys)), "b": frac(kb, len(keys)),
            "delta_pp": round(100.0 * (kb - ka) / len(keys), 1),
            "mcnemar": test, "separated": test["p"] < 0.05,
            "fisher_unpaired_p": fisher_exact(ka, len(keys), kb, len(keys)),
        }
    return {"labels": [label_a, label_b], "paired_fields": len(paired),
            "labels_withdrawn": sorted(withdrawn), "labels_added": sorted(added),
            "note": "one run, two gold versions: the delta is the label correction "
                    "and nothing else, because no model was called",
            "scopes": rows}


# =====================================================================
# ONE RUN, IN ONE DICT
# =====================================================================
def load_run(run_dir) -> dict:
    run_dir = Path(run_dir)          # accept a str from a caller or a notebook

    def jsonl(name):
        path = run_dir / name
        if not path.is_file():
            return {}
        return {r["case_id"]: r for r in
                (json.loads(line) for line in
                 path.read_text(encoding="utf-8").splitlines() if line)}

    manifest_path = run_dir / "manifest.json"
    return {
        "dir": run_dir,
        "manifest": (json.loads(manifest_path.read_text(encoding="utf-8"))
                     if manifest_path.is_file() else {}),
        "gated": jsonl("gated.jsonl"),
        "ungated": jsonl("ungated.jsonl"),
    }


def headline(run_dir: Path, gold_path: Path = SEALED_PATH) -> dict:
    """Everything one run says, in one dict, for cross-run history."""
    run = load_run(Path(run_dir))
    gold_cases = json.loads(Path(gold_path).read_text(encoding="utf-8"))["cases"]
    gated, ungated = run["gated"], run["ungated"] or run["gated"]
    pops = split(gold_cases, gated, ungated)
    quality = field_quality(gold_cases, gated, pops["scored"])
    outcomes = field_outcomes(gold_cases, gated)
    correct = sum(outcomes.values())
    manifest = run["manifest"]
    return {
        "run_id": manifest.get("run_id", Path(run_dir).name),
        "mode": manifest.get("mode"),
        "model": manifest.get("model"),
        "experiment": manifest.get("experiment"),
        "prompt_fingerprint": manifest.get("prompt_fingerprint"),
        "gold_version": (manifest.get("gold") or {}).get("version"),
        "populations": {k: len(v) for k, v in pops.items() if k != "total"},
        "cases": pops["total"],
        "recall": frac(correct, len(outcomes)),
        "recall_ci95": wilson_interval(correct, len(outcomes)),
        "macro_f1": quality["macro_f1"],
        "per_field": {f: {"recall": v["recall"]["text"], "precision": v["precision"]["text"],
                          "f1": v["f1"]} for f, v in quality["per_field"].items()},
        "shape": run_shape(gold_cases, gated, correct),
        "cost_provenance": cost_provenance(gated),
        "taxonomy": failure_taxonomy(gold_cases, gated, ungated, pops["scored"]),
    }


# =====================================================================
# RENDERING
# =====================================================================
def render(head: dict) -> str:
    out, w = [], None
    out.append("=" * 74)
    out.append("  %s · %s · prompt %s%s" % (
        head["run_id"], head["model"], head["prompt_fingerprint"],
        f" · experiment {head['experiment']}" if head.get("experiment") else ""))
    out.append("=" * 74)
    w = out.append

    pops = head["populations"]
    w("")
    w("  WHAT HAPPENED TO %d CASES" % head["cases"])
    w("    scored (the model answered)        %d" % pops["scored"])
    w("    error envelope, never scored       %d" % pops["errored"])
    w("    refused by our own code            %d" % pops["refused"])
    if pops["errored"] or pops["refused"]:
        w("    ^ only the first row is a statement about the model. An upstream")
        w("      failure is not an abstention, and a note our encoding gate")
        w("      refused to send was never seen by any model.")

    w("")
    w("  FIELD QUALITY - scored cases only")
    w("    %-12s %-14s %-14s %s" % ("field", "precision", "recall", "F1"))
    for field, v in head["per_field"].items():
        w("    %-12s %-14s %-14s %s" % (
            field, v["precision"], v["recall"],
            "%.2f" % v["f1"] if v["f1"] is not None else "n/a"))
    low, high = head["recall_ci95"]
    w("    pooled recall %s   95%% CI %.3f-%.3f   macro-F1 %s" % (
        head["recall"]["text"], low, high,
        "%.2f" % head["macro_f1"] if head["macro_f1"] is not None else "n/a"))

    shape = head["shape"]
    w("")
    w("  RUN SHAPE")
    w("    calls            %d" % shape["calls"])
    w("    tokens           %s in / %s out%s" % (
        "{:,}".format(shape["tokens_in"]), "{:,}".format(shape["tokens_out"]),
        " / {:,} cached".format(shape["tokens_cached"]) if shape["tokens_cached"] else ""))
    w("    cost per call            %s" % (
        "US$%.6f" % shape["cost_per_call"] if shape["cost_per_call"] is not None else "n/a"))
    w("    cost per CORRECT field   %s" % (
        "US$%.6f" % shape["cost_per_correct_field"]
        if shape["cost_per_correct_field"] is not None else "n/a - nothing was correct"))
    w("    ^ the second is the one to quote. A system that fails cheaply looks")
    w("      cheap only on the first.")
    if shape["latency_ms"]["median"] is not None:
        w("    latency          median %.0f ms  p90 %.0f ms  p95 %.0f ms  max %.0f ms" % (
            shape["latency_ms"]["median"], shape["latency_ms"]["p90"],
            shape["latency_ms"]["p95"], shape["latency_ms"]["max"]))
        w("    inside the %s ms budget   %s" % (
            shape["latency_budget_ms"], shape["inside_budget"]["text"]))

    prov = head["cost_provenance"]
    w("")
    w("  COST PROVENANCE - measured against the price table, never blended")
    w("    provider priced %s   US$%.6f charged / US$%.6f predicted (drift US$%+.6f)" % (
        prov["calls_priced_by_provider"]["text"], prov["provider_usd"],
        prov["estimate_usd"], prov["drift_usd"]))
    if not prov["all_measured"]:
        w("    ^ not every call carries a provider figure: do not quote the total")

    tax = head["taxonomy"]
    w("")
    w("  WHY %d FIELDS FAILED, AND WHOSE PROBLEM EACH IS" % tax["failed_fields"])
    w("    %-30s %-6s %s" % ("cause", "count", "layer that owns the fix"))
    for bucket in tax["buckets"]:
        w("    %-30s %-6d %s" % (bucket["cause"][:30], bucket["count"], bucket["layer"]))
    w("")
    w("    by layer:")
    for layer, count in tax["by_layer"].items():
        w("      %-58s %d" % (layer, count))
    w("")
    return "\n".join(out)


def render_comparison(result: dict) -> str:
    label_a, label_b = result["labels"]
    out = ["=" * 74,
           "  IS THE DIFFERENCE REAL?  %s  ->  %s" % (label_a, label_b),
           "=" * 74, "",
           "  Paired over %d labelled fields: the same cases, the same labels, so"
           % result["paired_fields"],
           "  only fields that CHANGED carry information (exact McNemar).", ""]
    out.append("    %-12s %-14s %-14s %-8s %-14s %s"
               % ("scope", label_a[:14], label_b[:14], "delta", "flips r/w", "p"))
    for scope, row in result["scopes"].items():
        test = row["mcnemar"]
        out.append("    %-12s %-14s %-14s %+7.1fpp %-14s %.3f%s" % (
            scope, row["a"]["text"], row["b"]["text"], row["delta_pp"],
            "%d / %d" % (test["flipped_to_right"], test["flipped_to_wrong"]),
            test["p"], "  SEPARATED" if row["separated"] else ""))
    out.append("")
    pooled = result["scopes"].get("pooled")
    if pooled and not pooled["separated"]:
        out.append("  Pooled: NOT separated at alpha 0.05. That does not mean the two are")
        out.append("  equally good - it means this experiment is too small to tell, which")
        out.append("  is a statement about the experiment and not about the systems.")
    separated = [s for s, r in result["scopes"].items() if r["separated"] and s != "pooled"]
    if separated:
        out.append("  Separated in: %s. A change can be real in one field and noise"
                   % ", ".join(separated))
        out.append("  overall; report it at the level where the evidence supports it.")
    out.append("")
    return "\n".join(out)


def render_gold_comparison(result: dict) -> str:
    label_a, label_b = result["labels"]
    out = ["=" * 74,
           "  SAME OUTPUT, TWO GOLD VERSIONS:  %s  ->  %s" % (label_a, label_b),
           "=" * 74, "",
           "  No model was called, so every difference below is the label",
           "  correction and nothing else. %d fields are `found` in both versions"
           % result["paired_fields"],
           "  and are the paired unit; a field whose status changed leaves the",
           "  denominator rather than counting as a flip.", ""]
    out.append("    %-12s %-14s %-14s %-9s %-13s %-8s %s"
               % ("scope", label_a[:14], label_b[:14], "delta", "flips r/w",
                  "McNemar", "Fisher (unpaired, not applicable)"))
    for scope, row in result["scopes"].items():
        test = row["mcnemar"]
        out.append("    %-12s %-14s %-14s %+8.1fpp %-13s %-8.3f %.3f%s" % (
            scope, row["a"]["text"], row["b"]["text"], row["delta_pp"],
            "%d / %d" % (test["flipped_to_right"], test["flipped_to_wrong"]),
            test["p"], row["fisher_unpaired_p"],
            "  SEPARATED" if row["separated"] else ""))
    out.append("")
    out.append("  labels withdrawn (found -> not_stated): %d%s"
               % (len(result["labels_withdrawn"]),
                  "  " + ", ".join(result["labels_withdrawn"])
                  if result["labels_withdrawn"] else ""))
    out.append("  labels added (not_stated -> found):     %d%s"
               % (len(result["labels_added"]),
                  "  " + ", ".join(result["labels_added"])
                  if result["labels_added"] else ""))
    out.append("")
    return "\n".join(out)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("run_dir", nargs="+", type=Path,
                        help="one run to describe, or two to compare")
    parser.add_argument("--gold", type=Path, default=SEALED_PATH)
    parser.add_argument("--gold-b", type=Path, default=None,
                        help="a second gold version: with ONE run directory this "
                             "scores that run against both and isolates the label "
                             "correction, with no model call and no sampling noise")
    parser.add_argument("--quiet", action="store_true", help="JSON on stdout only")
    args = parser.parse_args(argv)

    for path in args.run_dir:
        if not (path / "gated.jsonl").is_file():
            print(f"{path}: no gated.jsonl", file=sys.stderr)
            return EXIT_UNUSABLE
    if not args.gold.is_file():
        print(f"{args.gold}: not found", file=sys.stderr)
        return EXIT_UNUSABLE

    if args.gold_b is not None:
        if len(args.run_dir) != 1:
            print("--gold-b takes exactly one run directory: it varies the gold, "
                  "not the run", file=sys.stderr)
            return EXIT_UNUSABLE
        if not args.gold_b.is_file():
            print(f"{args.gold_b}: not found", file=sys.stderr)
            return EXIT_UNUSABLE
        gated = load_run(args.run_dir[0])["gated"]
        a_cases = json.loads(args.gold.read_text(encoding="utf-8"))["cases"]
        b_cases = json.loads(args.gold_b.read_text(encoding="utf-8"))["cases"]
        result = compare_golds(gated, a_cases, b_cases, args.gold.stem, args.gold_b.stem)
        if not args.quiet:
            print(render_gold_comparison(result), file=sys.stderr)
        sys.stdout.write(json.dumps(result, indent=2, default=str) + "\n")
        return EXIT_OK

    report = {"runs": [headline(path, args.gold) for path in args.run_dir]}
    if not args.quiet:
        for head in report["runs"]:
            print(render(head), file=sys.stderr)
    if len(args.run_dir) == 2:
        gold_cases = json.loads(args.gold.read_text(encoding="utf-8"))["cases"]
        a, b = (load_run(p)["gated"] for p in args.run_dir)
        labels = [h["prompt_fingerprint"] or h["run_id"] for h in report["runs"]]
        if labels[0] == labels[1]:
            # Same prompt, different gold variant or different day: the run id is
            # what actually distinguishes them.
            labels = [h["run_id"] for h in report["runs"]]
        report["comparison"] = compare(gold_cases, a, b, labels[0], labels[1])
        if not args.quiet:
            print(render_comparison(report["comparison"]), file=sys.stderr)
    sys.stdout.write(json.dumps(report, indent=2, default=str) + "\n")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
