#!/usr/bin/env python3
"""Derive gold-v2 from the sealed gold-v1 by applying declared corrections.

    ./.venv/bin/python evals/label_gold_v2.py plan                     # what would change, and why
    ./.venv/bin/python evals/label_gold_v2.py build                    # write data/gold_labels/gold_v2.json
    ./.venv/bin/python evals/label_gold_v2.py validate
    ./.venv/bin/python evals/label_gold_v2.py validate --seal --labeller <name>

CLAUDE.md 1.3: `gold_v1.json` is immutable and corrections belong in a new
version. This tool reads gold-v1 read-only, verifies its seal before touching
anything, and writes a complete gold-v2 with every correction recorded beside
the rule that requires it.

## Why each correction exists

`evals/diagnose.py` audits the labels against rules the gold set itself states
and finds 16 that contradict one. They are not model failures and they are not
scoring bugs - they are labelling defects, and three of them were penalising the
pipeline for obeying a rule the label broke:

  - `FIELD_RULES['dose']`: "A quantity per administration ('1 tablet', 'half')
    is NOT a dose; if only a quantity is given, dose is not_stated." Seven dose
    labels are quantities, comments or a frequency in the wrong slot.
  - `FIELD_RULES['frequency']`: "Timing relative to food ('after food') is not a
    frequency." Seven frequency labels carry meal timing or a course duration.
  - `SHARED_FIELD_RULE`: dose and frequency describe the medication named in
    `medication`, and if that medication has no dictated strength then dose is
    not_stated "even when another medication in the note has one".
  - A value must be supported by its own evidence span, which is the same check
    `extract.verify_value` applies to the model.

## Two kinds of correction, and only one of them is mechanical

`rule` corrections follow from the sentences above with no judgement: the label
says `half`, the rule says a quantity is not a dose, so the field becomes
not_stated. `review` corrections repair a label whose *intent* has to be read -
case_010 has a drug name sitting in the frequency slot and a frequency sitting
in the dose slot, which is legible but still an inference about what the
labeller meant.

**Sealing refuses while any `review` correction is unconfirmed.** Confirm them
with `--confirm case_010.medication` (repeatable), and the confirming labeller's
name is written into the provenance of each one. A correction nobody signed is
not a correction; it is a guess with better formatting.

stdout: JSON. stderr: the human report.
Exit codes: 0 done; 1 not ready to seal; 2 inputs refused; 6 internal error.
"""

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "evals"))

from extract import (  # noqa: E402
    CRITICAL_FIELDS, ExtractedField, GoldCase, GoldField, GoldSet, Status,
    prompt_fingerprint, verify_evidence, verify_value,
)

GOLD_DIR = ROOT / "data" / "gold_labels"
V1_PATH = GOLD_DIR / "gold_v1.json"
V1_SHA = GOLD_DIR / "gold_v1.sha256"
V2_PATH = GOLD_DIR / "gold_v2.json"
V2_SHA = GOLD_DIR / "gold_v2.sha256"

EXIT_OK = 0
EXIT_NOT_READY = 1
EXIT_REFUSED = 2
EXIT_INTERNAL = 6

RULE = "rule"        # follows from a stated field rule; no judgement
REVIEW = "review"    # repairs an intent; a human must sign it

NOT_STATED = {"value": None, "evidence": None, "status": "not_stated"}

# ---------------------------------------------------------------------------
# The corrections, in case order. Each is (case, field, change, kind, why).
# `change` is a partial update; anything it does not mention is kept.
# ---------------------------------------------------------------------------
CORRECTIONS = [
    ("case_001", "medication", {"evidence": "chymoral forte"}, RULE,
     "the value names chymoral forte but the span captured paracetamol; both "
     "phrases are in the note, and extract.verify_value rejects the pair"),
    ("case_001", "dose", NOT_STATED, RULE,
     "SHARED_FIELD_RULE: the 500 is paracetamol's strength, and chymoral forte "
     "has none dictated, so dose is not_stated even though the note has a number"),
    ("case_003", "dose", NOT_STATED, RULE,
     "'one' is a quantity per administration, which FIELD_RULES says is not a dose"),
    ("case_004", "dose", NOT_STATED, RULE,
     "'half' is named in FIELD_RULES as an example of what is NOT a dose"),
    ("case_009", "dose", NOT_STATED, RULE,
     "'one' is a quantity per administration, not a strength"),
    ("case_010", "medication", {"value": "augmentin", "evidence": "augmentin",
                                "status": "found"}, REVIEW,
     "the note prescribes augmentin and dolo 650, so medication is not "
     "not_stated; 'augmentin' appears in this case's frequency slot, which reads "
     "as a slot mix-up rather than a decision that no drug was prescribed"),
    ("case_010", "dose", NOT_STATED, RULE,
     "'twice a day' is a frequency, not a dose; and augmentin has no dictated "
     "strength, so dose is not_stated under SHARED_FIELD_RULE"),
    ("case_010", "frequency", {"value": "twice a day", "evidence": "twice a day"},
     REVIEW,
     "'augmentin' is a drug name and names no interval; the note says augmentin "
     "is taken twice a day"),
    ("case_012", "frequency", {"value": "twice a day"}, RULE,
     "FIELD_RULES: meal timing and a course duration are not part of a frequency"),
    ("case_015", "dose", NOT_STATED, RULE,
     "'one' is a quantity per administration, not a strength"),
    ("case_016", "dose", NOT_STATED, RULE,
     "'1 tablet' is named in FIELD_RULES as an example of what is NOT a dose"),
    ("case_034", "dose", NOT_STATED, RULE,
     "'no dosage' is a labelling comment in a value field, and the note dictates "
     "no strength for Dolo"),
    ("case_045", "frequency", {"value": "two times a day"}, RULE,
     "FIELD_RULES: 'after breakfast and dinner' is meal timing, not a frequency"),
    ("case_050", "frequency", {"value": "twice a day"}, RULE,
     "FIELD_RULES: 'after food' is not part of a frequency"),
    ("case_057", "frequency", {"value": "twice a day"}, RULE,
     "FIELD_RULES: 'before food' is not part of a frequency"),
    ("case_060", "frequency", {"value": "two times a day"}, RULE,
     "FIELD_RULES: meal timing is not part of a frequency"),
    ("case_061", "frequency", {"value": "twice a day"}, RULE,
     "FIELD_RULES: meal timing is not part of a frequency"),
    ("case_063", "frequency", {"value": "three times a day"}, RULE,
     "FIELD_RULES: 'after meal' is not part of a frequency"),
]

# ---------------------------------------------------------------------------
# Slice tags. Derived by pattern so they are reproducible, and the pattern is
# recorded in the provenance: a hand-assigned tag nobody can re-derive is not
# an evaluation slice, it is an opinion.
# ---------------------------------------------------------------------------
SLICE_PATTERNS = {
    "negation": r"\b(?:no|not|denies|denied|nil|without|never)\b",
    "attribution": r"\b(?:mother|father|wife|husband|son|daughter|brother|sister|family)\b",
    "temporality": r"\b(?:was on|used to|until|previously|stopped|discontinued|"
                   r"last (?:month|week|year))\b",
    "contemplation": r"\b(?:consider|if needed|may start|plan to start|as needed)\b",
}
# Three or more `<form> <Name>` mentions: the notes the single medication slot
# cannot represent, which is the slice the schema argument rests on.
MULTIDRUG = re.compile(
    r"\b(?:tablets?|tabs?|capsules?|caps?|syrups?|syp|injections?|inj|drops?)\b\.?\s+[A-Za-z]",
    re.I)
MULTIDRUG_MIN = 3


def sha256_of(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load_v1() -> GoldSet:
    """gold-v1, read-only, and only if it still matches its own seal."""
    if not V1_PATH.is_file():
        raise SystemExit(f"{V1_PATH} not found: nothing to derive gold-v2 from")
    if not V1_SHA.is_file():
        raise SystemExit(f"{V1_SHA} not found: gold-v1 was not sealed by the tool")
    recorded = V1_SHA.read_text(encoding="utf-8").split()[0]
    actual = sha256_of(V1_PATH)
    if recorded != actual:
        raise SystemExit(f"{V1_PATH} no longer matches {V1_SHA} ({actual} != {recorded}); "
                         "gold-v1 is immutable and something has edited it")
    return GoldSet.model_validate_json(V1_PATH.read_text(encoding="utf-8"))


def slice_tags(text: str) -> list:
    tags = [name for name, pattern in SLICE_PATTERNS.items()
            if re.search(pattern, text, re.I)]
    if len(MULTIDRUG.findall(text)) >= MULTIDRUG_MIN:
        tags.append("multidrug")
    return sorted(tags)


def apply_corrections(v1: GoldSet, confirmed: dict) -> tuple:
    """Build gold-v2's cases. Returns (cases, applied, unconfirmed)."""
    by_id = {case.case_id: case for case in v1.cases}
    missing = sorted({c for c, *_ in CORRECTIONS} - set(by_id))
    if missing:
        raise SystemExit(f"corrections name cases that are not in gold-v1: {missing}")

    updates, applied, unconfirmed = {}, [], []
    for case_id, field, change, kind, why in CORRECTIONS:
        key = f"{case_id}.{field}"
        signer = confirmed.get(key)
        if kind == REVIEW and not signer:
            unconfirmed.append({"correction": key, "kind": kind, "why": why})
            continue
        before = by_id[case_id].ground_truth[field]
        after = {"value": before.value, "evidence": before.evidence,
                 "status": before.status.value if before.status else None}
        after.update(change)
        updates.setdefault(case_id, {})[field] = after
        applied.append({
            "case_id": case_id, "field": field, "kind": kind, "rule": why,
            "from": {"value": before.value, "evidence": before.evidence,
                     "status": before.status.value if before.status else None},
            "to": after,
            "confirmed_by": signer if kind == REVIEW else "rule-derived",
        })

    cases = []
    for case in v1.cases:
        ground = {}
        for field in CRITICAL_FIELDS:
            patch = updates.get(case.case_id, {}).get(field)
            ground[field] = (GoldField(**patch) if patch
                             else case.ground_truth[field].model_copy())
        tags = slice_tags(case.source_text)
        note = " ".join(f"#{tag}" for tag in tags) if tags else None
        if case.notes:
            note = f"{case.notes}{(' ' + note) if note else ''}"
        cases.append(GoldCase(case_id=case.case_id, source_text=case.source_text,
                              ground_truth=ground, labeller=case.labeller,
                              labelled_at=case.labelled_at, notes=note))
    return cases, applied, unconfirmed


def build(confirmed: dict) -> tuple:
    v1 = load_v1()
    cases, applied, unconfirmed = apply_corrections(v1, confirmed)
    provenance = dict(v1.provenance)
    provenance.update({
        "derived_from": {"version": "gold-v1", "path": str(V1_PATH.relative_to(ROOT)),
                         "sha256": sha256_of(V1_PATH)},
        "corrections": applied,
        "corrections_pending_review": unconfirmed,
        "slice_tags": {"method": "regex over source_text, recorded so a slice is "
                                 "reproducible rather than an opinion",
                       "patterns": SLICE_PATTERNS,
                       "multidrug": f"at least {MULTIDRUG_MIN} '<form> <Name>' mentions"},
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "built_by": "evals/label_gold_v2.py",
    })
    provenance.pop("prompt_fingerprint", None)
    provenance.pop("sealed_at", None)
    provenance.pop("model_at_seal", None)
    return GoldSet(schema_version="gold-v2", provenance=provenance, cases=cases), \
        applied, unconfirmed


def validate(goldset: GoldSet) -> list:
    """Every label verbatim, and every corrected field now passing the same
    value gate the pipeline applies to the model."""
    problems = []
    for case in goldset.cases:
        for field in CRITICAL_FIELDS:
            gold = case.ground_truth[field]
            if not gold.is_labelled:
                problems.append(f"{case.case_id}.{field}: unlabelled")
                continue
            if gold.status is not Status.FOUND:
                continue
            probe = ExtractedField(evidence=gold.evidence or "", value=gold.value or "",
                                   status=Status.FOUND)
            span = verify_evidence(field, probe, case.source_text)
            if not span.ok:
                problems.append(f"{case.case_id}.{field}: evidence is not verbatim "
                                f"({span.code})")
            value = verify_value(field, probe)
            if value is not None:
                problems.append(f"{case.case_id}.{field}: value fails the pipeline's own "
                                f"gate ({value.code}): {gold.value!r} vs {gold.evidence!r}")
    return problems


def seal(goldset: GoldSet, labeller: str) -> None:
    """Write-once, read-only, hashed. The same discipline as gold-v1."""
    if V2_PATH.exists():
        raise SystemExit(f"{V2_PATH} already exists and is sealed; corrections to a sealed "
                         "version go into gold-v3, never over the top of this one")
    provenance = dict(goldset.provenance)
    provenance.update({
        "sealed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "sealed_by": labeller,
        "prompt_fingerprint": prompt_fingerprint(),
    })
    sealed = GoldSet(schema_version=goldset.schema_version, provenance=provenance,
                     cases=goldset.cases)
    V2_PATH.write_text(sealed.model_dump_json(indent=1) + "\n", encoding="utf-8")
    digest = sha256_of(V2_PATH)
    V2_SHA.write_text(f"{digest}  {V2_PATH.name}\n", encoding="utf-8")
    for path in (V2_PATH, V2_SHA):
        os.chmod(path, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
    print(f"sealed {V2_PATH.relative_to(ROOT)}  sha256 {digest}", file=sys.stderr)
    print(f"prompt fingerprint frozen: {prompt_fingerprint()}", file=sys.stderr)


def report(goldset: GoldSet, applied: list, unconfirmed: list, problems: list) -> None:
    out = sys.stderr
    print(f"\ngold-v2 derived from gold-v1 ({len(goldset.cases)} cases)", file=out)
    print(f"corrections applied: {len(applied)}  "
          f"({sum(1 for a in applied if a['kind'] == RULE)} rule-derived, "
          f"{sum(1 for a in applied if a['kind'] == REVIEW)} human-confirmed)", file=out)
    print("-" * 78, file=out)
    for entry in applied:
        before, after = entry["from"], entry["to"]
        changed = [k for k in ("value", "evidence", "status") if before[k] != after[k]]
        for key in changed:
            print(f"  {entry['case_id']} {entry['field']:11} {key:9} "
                  f"{before[key]!r} -> {after[key]!r}", file=out)
        print(f"      {entry['kind']}: {entry['rule']}", file=out)
    if unconfirmed:
        print(f"\n{len(unconfirmed)} correction(s) WAIT ON A HUMAN:", file=out)
        for entry in unconfirmed:
            print(f"  {entry['correction']}: {entry['why']}", file=out)
        print("\n  confirm with: --confirm "
              + " --confirm ".join(e["correction"] for e in unconfirmed)
              + " --labeller <name>", file=out)
    tagged = sum(1 for c in goldset.cases if c.notes and "#" in c.notes)
    counts = {}
    for case in goldset.cases:
        for tag in re.findall(r"#(\w+)", case.notes or ""):
            counts[tag] = counts.get(tag, 0) + 1
    print(f"\nslice tags on {tagged}/{len(goldset.cases)} cases: "
          + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())), file=out)
    if problems:
        print(f"\nNOT READY - {len(problems)} problem(s):", file=out)
        for problem in problems[:20]:
            print(f"  {problem}", file=out)
    elif unconfirmed:
        print("\nevery label is verbatim and every value passes the pipeline's own gate -"
              "\nbut the gate cannot see a drug name sitting in a frequency slot, which is"
              "\nexactly why the corrections above wait on a human rather than on a check.",
              file=out)
    else:
        print("\nevery label is verbatim and every value passes the pipeline's own gate",
              file=out)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=["plan", "build", "validate"])
    parser.add_argument("--seal", action="store_true", help="write the sealed gold-v2")
    parser.add_argument("--labeller", default=None, help="who confirms the review corrections")
    parser.add_argument("--confirm", action="append", default=[],
                        metavar="CASE.FIELD", help="sign off one review correction")
    parser.add_argument("--debug", action="store_true")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        if args.seal and not args.labeller:
            print("--seal requires --labeller: a correction nobody signed is a guess",
                  file=sys.stderr)
            return EXIT_REFUSED
        confirmed = {key: args.labeller for key in args.confirm}
        goldset, applied, unconfirmed = build(confirmed)
        problems = validate(goldset)
        report(goldset, applied, unconfirmed, problems)

        if args.command == "build":
            if unconfirmed or problems:
                print("\nrefusing to write an unfinished gold-v2", file=sys.stderr)
                return EXIT_NOT_READY
            staged = GOLD_DIR / "gold_v2_staged.json"
            staged.write_text(goldset.model_dump_json(indent=1) + "\n", encoding="utf-8")
            print(f"\nwrote {staged.relative_to(ROOT)} (unsealed; review it, then "
                  "validate --seal)", file=sys.stderr)
        if args.seal:
            if unconfirmed or problems:
                print("\nrefusing to seal", file=sys.stderr)
                return EXIT_NOT_READY
            seal(goldset, args.labeller)
    except SystemExit as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    except Exception as exc:
        print(f"ERROR ERR_INTERNAL: {type(exc).__name__}", file=sys.stderr)
        if args.debug:
            traceback.print_exc()
        return EXIT_INTERNAL

    summary = {
        "schema_version": goldset.schema_version,
        "cases": len(goldset.cases),
        "corrections_applied": len(applied),
        "corrections_pending_review": len(unconfirmed),
        "validation_problems": len(problems),
        "ready_to_seal": not (unconfirmed or problems),
        "sealed": bool(args.seal and not (unconfirmed or problems)),
    }
    sys.stdout.write(json.dumps(summary, indent=2) + "\n")
    return EXIT_OK if not (unconfirmed or problems) else EXIT_NOT_READY


if __name__ == "__main__":
    sys.exit(main())
