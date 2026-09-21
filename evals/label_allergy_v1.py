#!/usr/bin/env python3
"""Label the MTSamples allergy set, and build the headers-stripped twin.

    ./.venv/bin/python evals/label_allergy_v1.py validate
    ./.venv/bin/python evals/label_allergy_v1.py validate --seal --labeller <name>

Two sealed gold sets come out of one labelling pass:

  `allergy-v1`           the notes as MTSamples wrote them, ALL-CAPS section
                         headers intact
  `allergy-v1-stripped`  the same notes with every ALL-CAPS `HEADER:` removed,
                         the same case ids and **the same labels**

That is the leakage report `project_proposal.md` §6 promises. A model that reads
`ALLERGIES:` and copies the next word scores well on the first set and badly on
the second; one that reads the sentence scores the same on both. Because the two
sets share case ids and labels, `evals/metrics.py` compares them **paired**, so
the question is answered by the fields that changed rather than by two summary
rates.

## Why this corpus exists at all

Eka Care cannot measure the allergy field: across all 67 Latin-script notes there
is exactly one allergy mention and it is a denial, so gold-v1 has **zero** allergy
positives. Three quarters of this project's schema was measured and one quarter
was not, which is not a thing to leave in a report.

## The labelling rules, declared before any run

These differ from gold-v1's on purpose, and the difference is why the two sets
must not be pooled:

  allergy     the substance named after the ALL-CAPS header. Where several are
              listed, **the first**. The evidence span is the substance text
              only, never the header - that is what keeps one label verbatim in
              both variants.
  medication  **the first medication named in the MEDICATIONS section.** A
              positional rule, not gold-v1's "most clinically significant":
              ranking clinical significance across surgical and progress notes is
              a judgement the labeller here is not qualified to make, and a rule
              nobody can reproduce is worse than a narrow one. A note with no
              MEDICATIONS section, or one that says "None" or "Refer to chart",
              is not_stated. So is a medication the note says was *recently
              finished*, under the same temporality rule gold-v1 uses.
  dose        that medication's strength as dictated, and only its own. A
              quantity per administration is not a dose.
  frequency   that medication's interval as dictated. Food timing is not a
              frequency.

**Therefore: this set measures the allergy field and header leakage. It does not
measure medication selection**, and its medication numbers are not comparable
with gold-v1's. Stated here so nobody pools them by accident.

## What it still cannot tell you

The notes are American surgical and progress notes, not Singapore polyclinic
dictation. Performance here transfers to the deployment target only as far as the
genres resemble each other, which is not far. And one substance per note is the
same single-slot limitation the medication field has: a note listing "IODINE,
FISH OIL, FLEXERIL, BETADINE" is labelled `IODINE`, so a model naming BETADINE is
scored wrong for choosing differently rather than for being wrong.

stdout: JSON. stderr: the report.
Exit codes: 0 ready; 1 not ready to seal; 2 inputs refused; 6 internal error.
"""

import argparse
import hashlib
import json
import os
import random
import re
import stat
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from extract import (  # noqa: E402
    CRITICAL_FIELDS, ExtractedField, GoldCase, GoldField, GoldSet, Status,
    prompt_fingerprint, verify_evidence, verify_value,
)

GOLD_DIR = ROOT / "data" / "gold_labels"
CANDIDATES = ROOT / "data" / "allergy_set" / "candidates.json"
DATASET = "mtsamples"
SEED, N_WITH_MEDS, N_WITHOUT = 42, 15, 5

VARIANTS = {
    "allergy-v1": (GOLD_DIR / "allergy_v1.json", GOLD_DIR / "allergy_v1.sha256", False),
    "allergy-v1-stripped": (GOLD_DIR / "allergy_v1_stripped.json",
                            GOLD_DIR / "allergy_v1_stripped.sha256", True),
}

EXIT_OK, EXIT_NOT_READY, EXIT_REFUSED, EXIT_INTERNAL = 0, 1, 2, 6

# An ALL-CAPS section header, which is the leak. Requires the colon, so an
# ALL-CAPS substance such as "SULFA AND LATEX" is untouched.
SECTION_HEADER = re.compile(r"(?:(?<=^)|(?<=[,.]))\s*[A-Z][A-Z0-9 /&'\-]{2,}\s*:\s*,?\s*")

# row_idx -> the four fields. `None` means not_stated. Each evidence span is
# checked against the note - in both variants - before anything is sealed.
LABELS = {
    1:    ("Penicillin", None, None, None),
    53:   ("Penicillin", "Lisinopril", None, None),
    117:  ("SULFA", None, None, None),
    128:  ("penicillin", "Ritalin", "50", "a day"),
    301:  ("PENICILLIN", None, None, None),
    1317: ("Penicillin", "Synthroid", "0.112 mg", "daily"),
    1328: ("Penicillin", "Levothyroxine", "200 mcg", "q.d."),
    1346: ("Sulfa", "Levaquin", None, None),
    1349: ("Sulfa", "B12", "1000 mg", "monthly"),
    1449: ("Demerol", "Lotensin", None, None),
    1456: ("Vicodin", "Prilosec", "20 mg", "b.i.d."),
    1810: ("adhesive tape", None, None, None),
    1821: ("dairy products", None, None, None),
    1857: ("ACCUTANE", "Ancef", None, None),
    2419: ("aspirin", None, None, None),
    3158: ("Penicillin", "Lisinopril/hydrochlorothiazide", "20/25 mg", "q.d."),
    3367: ("Naprosyn", "Vioxx", "25 mg", "daily"),
    # "Recently finished Minocin and Duraphen II DM": discontinued, so not a
    # current medication under the same temporality rule gold-v1 applies.
    3375: ("Sulfa", None, None, None),
    3380: ("Lortab", None, None, None),
    3852: ("IODINE", None, None, None),
}


def strip_headers(text: str) -> str:
    """Remove ALL-CAPS section headers, leaving the prose and the substances."""
    return re.sub(r"\s{2,}", " ", SECTION_HEADER.sub(" ", text)).strip()


def sha256_of(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def selection() -> list:
    if not CANDIDATES.is_file():
        raise SystemExit(f"{CANDIDATES} not found: run data/allergy_set/fetch_mtsamples.py")
    document = json.loads(CANDIDATES.read_text(encoding="utf-8"))
    pool = document["candidates"]
    with_meds = [c for c in pool if c["has_medications_header"]]
    without = [c for c in pool if not c["has_medications_header"]]
    rng = random.Random(SEED)
    picked = rng.sample(with_meds, min(N_WITH_MEDS, len(with_meds))) + \
        rng.sample(without, min(N_WITHOUT, len(without)))
    return document["provenance"], sorted(picked, key=lambda c: c["row_idx"])


def field(value):
    if value is None:
        return GoldField(value=None, evidence=None, status=Status.NOT_STATED)
    return GoldField(value=value, evidence=value, status=Status.FOUND)


def build(strip: bool) -> tuple:
    source_provenance, picked = selection()
    missing = sorted({c["row_idx"] for c in picked} - set(LABELS))
    if missing:
        raise SystemExit(f"unlabelled rows in the selection: {missing}")
    cases = []
    for index, candidate in enumerate(picked, start=1):
        text = candidate["source_text"]
        note = strip_headers(text) if strip else text
        allergy, medication, dose, frequency = LABELS[candidate["row_idx"]]
        cases.append(GoldCase(
            case_id=f"allergy_{index:03d}",
            source_text=note,
            ground_truth={"medication": field(medication), "dose": field(dose),
                          "frequency": field(frequency), "allergy": field(allergy)},
            labeller="claude-opus-5", labelled_at="2026-09-21T00:00:00+00:00",
            notes=f"#allergy row_idx={candidate['row_idx']} "
                  f"specialty={candidate['specialty'] or 'unknown'}"
                  + (" #headers_stripped" if strip else " #headers_intact")))
    provenance = {
        "dataset": DATASET,
        "source": {k: source_provenance.get(k) for k in
                   ("dataset", "config", "split", "via", "pulled_at", "rows_scanned",
                    "rows_in_split", "filter", "yield")},
        "headers_stripped": strip,
        "strip_pattern": SECTION_HEADER.pattern if strip else None,
        "selection": {"seed": SEED, "with_medications_header": N_WITH_MEDS,
                      "without": N_WITHOUT,
                      "row_idx": [c["row_idx"] for c in picked],
                      "text_md5": {f"allergy_{i:03d}": c["text_md5"]
                                   for i, c in enumerate(picked, start=1)}},
        "labelling_rules": {
            "allergy": "the substance after the ALL-CAPS header; the first where "
                       "several are listed; the span excludes the header",
            "medication": "the FIRST medication in the MEDICATIONS section - a "
                          "positional rule, NOT gold-v1's 'most clinically "
                          "significant'; None/Refer to chart/recently finished "
                          "are not_stated",
            "dose": "that medication's strength as dictated; a quantity per "
                    "administration is not a dose",
            "frequency": "that medication's interval as dictated; food timing is "
                         "not a frequency",
            "not_comparable_with": "gold-v1 medication numbers, because the "
                                   "selection rule differs",
        },
        "measures": ["allergy recall", "ALL-CAPS header leakage (paired against "
                                       "the stripped twin)"],
        "does_not_measure": ["medication selection", "Singapore polyclinic dictation"],
    }
    return GoldSet(schema_version=("allergy-v1-stripped" if strip else "allergy-v1"),
                   provenance=provenance, cases=cases), picked


def validate(goldset: GoldSet) -> list:
    problems = []
    for case in goldset.cases:
        for name in CRITICAL_FIELDS:
            gold = case.ground_truth[name]
            if gold.status is not Status.FOUND:
                continue
            probe = ExtractedField(evidence=gold.evidence or "", value=gold.value or "",
                                   status=Status.FOUND)
            span = verify_evidence(name, probe, case.source_text)
            if not span.ok:
                problems.append(f"{case.case_id}.{name}: {gold.evidence!r} is not verbatim "
                                f"in this variant ({span.code})")
            bad = verify_value(name, probe)
            if bad is not None:
                problems.append(f"{case.case_id}.{name}: value fails the pipeline's gate "
                                f"({bad.code})")
    return problems


def seal(goldset: GoldSet, path: Path, sha_path: Path, labeller: str) -> str:
    if path.exists():
        raise SystemExit(f"{path} already exists and is sealed; a correction goes into a "
                         "new version, never over the top of this one")
    provenance = dict(goldset.provenance)
    provenance.update({"sealed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                       "sealed_by": labeller,
                       "prompt_fingerprint": prompt_fingerprint()})
    sealed = GoldSet(schema_version=goldset.schema_version, provenance=provenance,
                     cases=goldset.cases)
    path.write_text(sealed.model_dump_json(indent=1) + "\n", encoding="utf-8")
    digest = sha256_of(path)
    sha_path.write_text(f"{digest}  {path.name}\n", encoding="utf-8")
    for target in (path, sha_path):
        os.chmod(target, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
    return digest


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=["validate"])
    parser.add_argument("--seal", action="store_true")
    parser.add_argument("--labeller", default=None)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args(argv)
    summary = {}
    try:
        if args.seal and not args.labeller:
            print("--seal requires --labeller", file=sys.stderr)
            return EXIT_REFUSED
        all_problems = {}
        built = {}
        for version, (path, sha_path, strip) in VARIANTS.items():
            goldset, picked = build(strip)
            problems = validate(goldset)
            built[version] = (goldset, path, sha_path)
            all_problems[version] = problems
            found = sum(1 for c in goldset.cases for f in CRITICAL_FIELDS
                        if c.ground_truth[f].status is Status.FOUND)
            print(f"\n{version}: {len(goldset.cases)} cases, {found} labelled-found fields, "
                  f"median {sorted(len(c.source_text) for c in goldset.cases)[len(goldset.cases)//2]} chars",
                  file=sys.stderr)
            by_field = {f: sum(1 for c in goldset.cases
                               if c.ground_truth[f].status is Status.FOUND)
                        for f in CRITICAL_FIELDS}
            print(f"  found per field: {by_field}", file=sys.stderr)
            if problems:
                print(f"  NOT READY - {len(problems)} problem(s):", file=sys.stderr)
                for problem in problems[:12]:
                    print(f"    {problem}", file=sys.stderr)
            else:
                print("  every span verbatim, every value through the pipeline's own gate",
                      file=sys.stderr)
            summary[version] = {"cases": len(goldset.cases), "found_fields": found,
                                "by_field": by_field, "problems": len(problems)}

        ready = not any(all_problems.values())
        if args.seal and ready:
            for version, (goldset, path, sha_path) in built.items():
                digest = seal(goldset, path, sha_path, args.labeller)
                summary[version]["sha256"] = digest
                print(f"sealed {path.relative_to(ROOT)}  sha256 {digest}", file=sys.stderr)
        elif args.seal:
            print("\nrefusing to seal", file=sys.stderr)
    except SystemExit as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    except Exception as exc:
        print(f"ERROR ERR_INTERNAL: {type(exc).__name__}", file=sys.stderr)
        if args.debug:
            traceback.print_exc()
        return EXIT_INTERNAL
    sys.stdout.write(json.dumps(summary, indent=2) + "\n")
    return EXIT_OK if ready else EXIT_NOT_READY


if __name__ == "__main__":
    sys.exit(main())
