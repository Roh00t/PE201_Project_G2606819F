#!/usr/bin/env python3
"""The non-AI baseline: regex and a gazetteer, no model, no network, no spend.

    ./.venv/bin/python evals/baseline.py                        # pattern-only arm
    ./.venv/bin/python evals/baseline.py --gazetteer data/gazetteer/rxnorm_ingredients.txt

`project_proposal.md` section 4 commits to this comparator, and the instructor's
feedback named it. A recall number with no baseline says nothing: the majority-class
baseline for this task ("everything is not_stated") already agrees with gold on 113 of
268 field decisions, so 0.600 has to be read against something.

It writes a run-shaped directory, so `evals/score_arm.py`, `evals/diagnose.py` and
`demo/build_review.py` all read it with no special cases, and every field passes
through `extract.apply_gates` - the same gates the model's output passes through.
Comparing extractors, not scorers.

## The rules, stated in full

Rule-based extraction has no notion of "most clinically significant", so the
selection rule here is the reproducible one: **the first medication named in a
prescribing instruction**. That is a declared substitution, not an oversight,
and it is measured rather than defended - the gold labels pick the first
mentioned drug only 40% of the time.

  medication  the first `<form> <Name>` mention, where <form> is tablet, tab,
              cap, capsule, syrup, syp, inj, injection, drops, ointment or
              cream. A trailing strength is split off the name, because
              FIELD_RULES puts the strength in `dose`. With --gazetteer, a
              known ingredient name anywhere in the note is accepted as a
              fallback when no form word appears.
  dose        a strength attached to that medication: a number with a unit
              (mg, mcg, g, ml, IU, units), or a bare number immediately after
              the drug name as in "Dolo 650". A number followed by a dosage
              form ("1 tablet") is a quantity per administration, which
              FIELD_RULES says is not a dose, so it is rejected.
  frequency   the first dosing phrase after the medication: the shorthand and
              phrase table already in extract.FREQUENCY_EQUIVALENTS, plus
              digit regimens (1-0-1) and "N times a day" forms.
  allergy     "allergic/allergy/allergies to X", unless a negation sits within
              30 characters before it, in which case the field is not_stated.

## Two properties of this baseline worth reporting

1. **It cannot fail the evidence gate.** Every span is a slice of the note by
   construction, so the grounding gate is vacuous here. The abstention
   machinery only measures anything against a generative extractor - which is
   a statement about the scope of the guardrail, not a flaw in the baseline.
2. **It cannot abstain.** There is no "unsure": a pattern either matches or it
   does not. A physician gets no signal that a field was hard.

## Tuning discipline

A baseline tuned on every case it is then scored on is not a baseline. With
`--split report` the patterns are scored on the 47 cases outside the tuning
subsample; the 20-case tuning subsample is a seeded draw recorded in the
manifest. Nothing in this file was written by looking at a gold *value* - only
at the field rules and at notes in the tuning subsample.

stdout: the output directory. stderr: a summary.
Exit codes: 0 written; 2 inputs unusable; 6 internal error.
"""

import argparse
import json
import random
import re
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "evals"))

import extract  # noqa: E402
from extract import (  # noqa: E402
    BLANK, FREQUENCY_EQUIVALENTS, ClinicalExtraction, ExtractedField, Status,
    build_payload, scan_input,
)
from scoring import DOSAGE_FORMS  # noqa: E402

GOLD_DIR = ROOT / "data" / "gold_labels"
SEALED_PATH = GOLD_DIR / "gold_v1.json"
RESULTS_DIR = ROOT / "evals" / "results"
DEFAULT_GAZETTEER = ROOT / "data" / "gazetteer" / "rxnorm_ingredients.txt"

EXIT_OK = 0
EXIT_UNUSABLE = 2
EXIT_INTERNAL = 6

TUNE_N = 20
TUNE_SEED = 42

FORM = r"(?:tablets?|tabs?|capsules?|caps?|syrups?|syp|injections?|inj|drops?|ointment|cream)"
MEDICATION = re.compile(rf"\b{FORM}\.?\s+([A-Za-z][\w-]*(?:\s+[A-Za-z0-9][\w-]*){{0,2}})", re.I)
TOKEN = re.compile(r"[\w-]+")

# A drug name in dictation runs until a dosing word starts. Without this the
# capture above swallows the schedule - "Tablet Dolo twice a day" becomes
# "Dolo twice a" - which would make the baseline look far worse than the rules
# deserve. These are words about *how* a drug is taken, never part of its name;
# the list is dictation structure, not knowledge of any gold answer.
STOP_WORDS = frozenset("""
once twice thrice times time daily day days night nightly morning noon afternoon
evening bedtime after before with without food foods meal meals breakfast lunch
dinner empty stomach for and or at in on of per as if needed sos prn stat po bd
bid tds tid od qd qid qds hs week weeks month months start started stop stopped
continue continued now today tomorrow the a an is was to mg mcg ug g gm ml l iu
unit units lakh lakhs tablet tablets tab tabs capsule capsules cap caps syrup
syrups syp injection injections inj drop drops ointment cream
""".split())
UNIT = r"(?:mg|mcg|ug|µg|g|gm|ml|l|iu|units?|lakh|lakhs)"
DOSE_WITH_UNIT = re.compile(rf"\b(\d+(?:[.,]\d+)?)\s*({UNIT})\b", re.I)
TRAILING_STRENGTH = re.compile(r"\s+(\d+(?:[.,]\d+)?)\s*$")
COUNT_THEN_FORM = re.compile(rf"\b\d+(?:[.,]\d+)?\s+{FORM}\b", re.I)
REGIMEN = re.compile(r"(?<![\d-])\d(?:-\d){2,3}(?![\d-])")
N_TIMES = re.compile(r"\b(?:once|twice|thrice|one|two|three|four|\d+)\s+times?\s+(?:a|per|in a)\s+"
                     r"(?:day|night|week)\b", re.I)
ALLERGY = re.compile(r"\ballerg(?:y|ic|ies)\s+to\s+([A-Za-z][\w-]*(?:\s+[A-Za-z][\w-]*)?)", re.I)
NEGATION = re.compile(r"\b(?:no|not|denies|denied|nil|without|never|any)\b", re.I)

# Longest first, so "twice a day" is not matched as "a day".
FREQUENCY_PHRASES = sorted(
    {phrase for group in FREQUENCY_EQUIVALENTS for phrase in group},
    key=len, reverse=True)
FREQUENCY = re.compile("|".join(rf"\b{re.escape(p)}\b" for p in FREQUENCY_PHRASES), re.I)


def _rel(path) -> str:
    """Repo-relative when it can be, absolute otherwise. A path given on the
    command line may be relative to the shell's cwd, and printing one must
    never raise - the same helper exists in run_ekacare.py and diagnose.py."""
    try:
        return str(Path(path).resolve().relative_to(ROOT))
    except ValueError:
        return str(path)


def load_gazetteer(path: Path | None) -> frozenset:
    if path is None:
        return frozenset()
    return frozenset(
        line.strip().casefold() for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#"))


def found(note: str, start: int, end: int, value: str | None = None) -> ExtractedField:
    """A field whose evidence is a literal slice of the note, by construction."""
    evidence = note[start:end]
    return ExtractedField(evidence=evidence, value=(value if value is not None else evidence),
                          status=Status.FOUND)


NOT_STATED_FIELD = ExtractedField(evidence=BLANK, value=BLANK, status=Status.NOT_STATED)


def trim_name(note: str, start: int, end: int) -> int:
    """Pull the captured name back to its last real name token.

    Works on offsets rather than on a copied string, so the evidence stays a
    literal slice of the note.
    """
    tokens = [(m.start() + start, m.end() + start) for m in TOKEN.finditer(note[start:end])]
    while tokens and note[tokens[-1][0]:tokens[-1][1]].casefold() in STOP_WORDS:
        tokens.pop()
    return tokens[-1][1] if tokens else start


def find_medication(note: str, gazetteer: frozenset) -> tuple[ExtractedField, int]:
    """The first `<form> <Name>` mention; the gazetteer is only a fallback.

    Returns the field and the offset where it ended, so dose and frequency can
    be looked for after it rather than anywhere in the note.
    """
    match = MEDICATION.search(note)
    if match:
        start, end = match.span(1)
        end = trim_name(note, start, end)
        if end > start:
            name = note[start:end]
            # A trailing strength belongs to `dose` (FIELD_RULES), so "Dolo 650"
            # yields medication "Dolo". The evidence keeps the full span.
            stripped = TRAILING_STRENGTH.sub("", name)
            return found(note, start, end, value=stripped or name), end
    if gazetteer:
        lowered = note.casefold()
        best = None
        for name in gazetteer:
            if len(name) < 5 or " " in name:
                continue                      # single long tokens only: cheap and safer
            position = lowered.find(name)
            if position >= 0 and (best is None or position < best[0]):
                best = (position, position + len(name))
        if best:
            return found(note, *best), best[1]
    return NOT_STATED_FIELD, 0


def find_dose(note: str, after: int, medication: ExtractedField) -> ExtractedField:
    """The strength of the medication that was chosen, not of any other drug.

    When the medication capture itself ended in a bare number ("Amoxicillin
    500"), the search starts *at* that number rather than after it, so a unit
    dictated next ("500 mg") is not left behind.
    """
    number_at = None
    if medication.status is Status.FOUND:
        tail = TRAILING_STRENGTH.search(medication.evidence)
        if tail:
            number_at = (after - len(medication.evidence)) + tail.start(1)
    start_at = number_at if number_at is not None else after
    window = note[start_at:start_at + 80]
    for match in DOSE_WITH_UNIT.finditer(window):
        begin, stop = start_at + match.start(), start_at + match.end()
        if COUNT_THEN_FORM.match(note, begin):
            continue                          # "1 tablet" is a quantity, not a strength
        return found(note, begin, stop)
    # "Dolo 650": a bare number inside the product name, with no unit dictated.
    if number_at is not None and not COUNT_THEN_FORM.match(note, number_at):
        return found(note, number_at, number_at + len(tail.group(1)))
    return NOT_STATED_FIELD


def find_frequency(note: str, after: int) -> ExtractedField:
    window_start = after
    candidates = []
    for pattern in (FREQUENCY, N_TIMES, REGIMEN):
        match = pattern.search(note, window_start)
        if match:
            candidates.append(match.span())
    if not candidates:
        return NOT_STATED_FIELD
    start, end = min(candidates)
    return found(note, start, end)


def find_allergy(note: str) -> ExtractedField:
    for match in ALLERGY.finditer(note):
        before = note[max(0, match.start() - 30):match.start()]
        if NEGATION.search(before):
            continue                          # "no known allergies to ..." is not an allergy
        start, end = match.span(1)
        return found(note, start, end)
    return NOT_STATED_FIELD


def extract_one(note: str, gazetteer: frozenset) -> ClinicalExtraction:
    medication, after = find_medication(note, gazetteer)
    return ClinicalExtraction(
        medication=medication,
        dose=find_dose(note, after, medication),
        frequency=find_frequency(note, after),
        allergy=find_allergy(note),
    )


def tuning_subsample(case_ids: list[str], n: int = TUNE_N, seed: int = TUNE_SEED) -> list[str]:
    return sorted(random.Random(seed).sample(sorted(case_ids), min(n, len(case_ids))))


def run(gold_path: Path, out_dir: Path, gazetteer_path: Path | None,
        split: str) -> tuple[Path, dict]:
    gold = json.loads(gold_path.read_text(encoding="utf-8"))
    cases = gold["cases"]
    gazetteer = load_gazetteer(gazetteer_path)
    tune = set(tuning_subsample([c["case_id"] for c in cases]))
    if split == "tune":
        selected = [c for c in cases if c["case_id"] in tune]
    elif split == "report":
        selected = [c for c in cases if c["case_id"] not in tune]
    else:
        selected = cases

    arm = "gazetteer" if gazetteer else "pattern-only"
    out_dir.mkdir(parents=True, exist_ok=True)
    gated_lines, ungated_lines, codes = [], [], {}
    for case in selected:
        note = case["source_text"]
        started = time.perf_counter()
        extraction = extract_one(note, gazetteer)
        scan = scan_input(note)
        shared = dict(note=case["case_id"], model_label=f"baseline:{arm}",
                      usage={"called": False, "billed_usd": 0.0},
                      provenance={"called": False, "extractor": f"regex/{arm}",
                                  "prompt_fingerprint": None},
                      api_ms=0.0, started=started)
        payload, code, _ = build_payload(extraction, note, scan, True, **shared)
        raw, _, _ = build_payload(extraction, note, scan, False, **shared)
        payload["case_id"] = raw["case_id"] = case["case_id"]
        gated_lines.append(json.dumps(payload))
        ungated_lines.append(json.dumps(raw))
        codes[str(code)] = codes.get(str(code), 0) + 1

    (out_dir / "gated.jsonl").write_text("\n".join(gated_lines) + "\n", encoding="utf-8")
    (out_dir / "ungated.jsonl").write_text("\n".join(ungated_lines) + "\n", encoding="utf-8")
    manifest = {
        "run_id": out_dir.name,
        "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "mode": "baseline",
        "reportable": split != "tune",
        "experiment": f"baseline-{arm}",
        "model": f"baseline:{arm}",
        "prompt_fingerprint": None,
        "extractor": {
            "kind": "regex" + ("+gazetteer" if gazetteer else ""),
            "gazetteer": _rel(gazetteer_path) if gazetteer_path else None,
            "gazetteer_names": len(gazetteer),
            "selection_rule": "first <form> <Name> mention in the note",
        },
        "gold": {"path": _rel(gold_path), "cases_total": len(cases),
                 "cases_run": len(selected)},
        "split": {"which": split, "tuning_n": TUNE_N, "tuning_seed": TUNE_SEED,
                  "tuning_case_ids": sorted(tune)},
        "spend": {"ceiling_usd": None, "billed_this_run_usd": 0.0},
        "exit_counts": codes,
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n",
                                           encoding="utf-8")
    return out_dir, manifest


def gazetteer_coverage(gold_path: Path, gazetteer: frozenset) -> dict:
    """How much of this corpus the external list can even name.

    Reported because it is the baseline's real result: RxNorm is a US
    ingredient vocabulary and these are Indian brand dictations.
    """
    cases = json.loads(gold_path.read_text(encoding="utf-8"))["cases"]
    values = [c["ground_truth"]["medication"]["value"] for c in cases
              if c["ground_truth"]["medication"]["status"] == "found"]
    covered = [v for v in values
               if (set(extract._tokens(v)) - DOSAGE_FORMS) & gazetteer
               or v.strip().casefold() in gazetteer]
    return {"gold_medications": len(values), "distinct": len(set(values)),
            "covered_by_gazetteer": len(covered),
            "coverage": round(len(covered) / len(values), 4) if values else None}


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--gold", type=Path, default=SEALED_PATH)
    parser.add_argument("--gazetteer", type=Path, default=None,
                        help=f"a name list, e.g. {DEFAULT_GAZETTEER.relative_to(ROOT)}")
    parser.add_argument("--split", choices=["tune", "report", "all"], default="all",
                        help="tune: the 20-case subsample; report: the other 47")
    parser.add_argument("--out", type=Path, default=None, help="output directory")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--debug", action="store_true")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        if not args.gold.is_file():
            print(f"gold file not found: {args.gold}", file=sys.stderr)
            return EXIT_UNUSABLE
        if args.gazetteer is not None and not args.gazetteer.is_file():
            print(f"gazetteer not found: {args.gazetteer}; "
                  "run data/gazetteer/fetch_rxnorm.py or omit --gazetteer", file=sys.stderr)
            return EXIT_UNUSABLE
        arm = "gazetteer" if args.gazetteer else "pattern"
        out_dir = args.out or RESULTS_DIR / f"baseline-{arm}-{args.split}"
        out_dir, manifest = run(args.gold, out_dir, args.gazetteer, args.split)
    except Exception as exc:
        print(f"ERROR ERR_INTERNAL (exit {EXIT_INTERNAL}): {type(exc).__name__}", file=sys.stderr)
        if args.debug:
            traceback.print_exc()
        return EXIT_INTERNAL
    if not args.quiet:
        print(f"baseline {manifest['extractor']['kind']} over "
              f"{manifest['gold']['cases_run']} cases ({args.split}) -> {out_dir}",
              file=sys.stderr)
        if args.gazetteer:
            coverage = gazetteer_coverage(args.gold, load_gazetteer(args.gazetteer))
            print(f"gazetteer names {manifest['extractor']['gazetteer_names']}; it can name "
                  f"{coverage['covered_by_gazetteer']}/{coverage['gold_medications']} "
                  f"({coverage['coverage']:.1%}) of the gold medications", file=sys.stderr)
        print("score it with: ./.venv/bin/python evals/score_arm.py "
              f"{_rel(out_dir)}", file=sys.stderr)
    sys.stdout.write(str(out_dir) + "\n")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
