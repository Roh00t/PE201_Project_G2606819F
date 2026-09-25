#!/usr/bin/env python3
"""The near-distribution arm: the same consults, dictated differently.

    ./.venv/bin/python evals/paraphrase.py report          # what would change, no writes
    ./.venv/bin/python evals/paraphrase.py validate
    ./.venv/bin/python evals/paraphrase.py validate --seal --labeller <name>

`docs/forensic_audit.md` graded Dimension C **Partial**: the far-distribution
perturbation (ALL-CAPS header stripping, §6.6) was built and measured, and the
near-distribution half of the same claim did not exist. This is that half. For a
system whose input is dictated speech, small surface variation is the more
operationally likely shift of the two - two physicians describing one consult do
not produce the same string.

**The labels do not move, and that is enforced rather than intended.** Every
transform here operates strictly OUTSIDE the spans that carry a gold value. The
note around `Gabantin GRS`, `300 milligram` and `one at night` is re-dictated;
those twelve characters, those eleven and those twelve are untouched. So
`paraphrase-v1` carries byte-identical `value` labels to gold-v2, the two sets
are paired case for case and field for field, and a McNemar over them is exact.
A paraphrase that loses a value is refused at build time, not discovered at
scoring time (`refuse_if_value_lost`).

**Why that is the interesting experiment rather than a weaker one.** §6.6 found
the model leaning on a structural crutch: strip the ALL-CAPS header and allergy
recall falls from 0.900 to 0.650. The prose around a value is the same kind of
crutch - `Tablet` before a drug name, `Advices` before a plan, a tidy sentence
boundary after a dose. Holding the value fixed and re-dictating everything else
is what isolates the crutch from the fact.

**This is not a violation of CLAUDE.md §2.4.** That rule forbids the *pipeline*
from mutating a physician's dictation to force a gate to pass, and it still
holds: `src/extract.py` treats its input as read-only and nothing here is
imported by it. This builds a separate, separately sealed, explicitly derived
evaluation corpus, exactly as `evals/label_allergy_v1.py` does for the stripped
variant. The Eka Care originals are not edited; `gold_v1.json` is not touched.

Deterministic: one seed, no model call, no network. Rebuilding gives the same
corpus, and `tests/test_paraphrase.py:Determinism` pins that.

stdout: one JSON summary. stderr: the table.
Exit codes: 0 done; 1 not ready to seal; 2 refused; 6 internal.
"""

import argparse
import hashlib
import json
import os
import random
import re
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "evals"))

from extract import (CRITICAL_FIELDS, ExtractedField, GoldCase, GoldField,  # noqa: E402
                     GoldSet, Status, prompt_fingerprint, verify_evidence,
                     verify_value)

GOLD_DIR = ROOT / "data" / "gold_labels"
SOURCE_PATH = GOLD_DIR / "gold_v2.json"
V1_PATH = GOLD_DIR / "paraphrase_v1.json"
V1_SHA_PATH = GOLD_DIR / "paraphrase_v1.sha256"
SCHEMA_VERSION = "paraphrase-v1"
SEED = 42

EXIT_OK, EXIT_NOT_READY, EXIT_REFUSED, EXIT_INTERNAL = 0, 1, 2, 6


# ---------------------------------------------------------------------------
# Protecting the values
# ---------------------------------------------------------------------------

def protected_spans(text: str, values: list[str]) -> list[tuple[int, int]]:
    """Every occurrence of every gold value, merged into disjoint ranges.

    Case-insensitive, because a value labelled `Vernace` can be dictated
    `vernace` and both occurrences must be protected: a transform that edited
    the lower-case one would leave the label technically present and the note
    incoherent.
    """
    spans = []
    for value in values:
        if not value:
            continue
        for match in re.finditer(re.escape(value), text, re.IGNORECASE):
            spans.append((match.start(), match.end()))
    if not spans:
        return []
    spans.sort()
    merged = [spans[0]]
    for start, end in spans[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


def segments(text: str, spans: list[tuple[int, int]]):
    """(is_protected, chunk) over the whole string, in order."""
    out, cursor = [], 0
    for start, end in spans:
        if start > cursor:
            out.append((False, text[cursor:start]))
        out.append((True, text[start:end]))
        cursor = end
    if cursor < len(text):
        out.append((False, text[cursor:]))
    return out


# ---------------------------------------------------------------------------
# The transforms, each modelling one way dictation varies
# ---------------------------------------------------------------------------
# Every one is str -> str over an UNPROTECTED chunk. They are named because the
# point of the arm is attribution: if recall falls, the report says which kind of
# variation did it, and "the paraphrase broke it" is not an answer.

DOSAGE_FORM = re.compile(r"\b(?:Tablet|Tab\.?|Cap\.?|Capsule|Syrup|Syp\.?|Inj\.?|"
                         r"Injection|Ointment)\s+", re.IGNORECASE)
MEAL_TIMING = [(re.compile(r"\bafter\s+food\b", re.I), "post meals"),
               (re.compile(r"\bbefore\s+food\b", re.I), "pre meals"),
               (re.compile(r"\bafter\s+meals?\b", re.I), "post food"),
               (re.compile(r"\bafter\s+breakfast\b", re.I), "post breakfast")]
PLAN_VERBS = [(re.compile(r"\bAdvices?\b"), "Advised"),
              (re.compile(r"\bDiagnosis is\b", re.I), "Impression is"),
              (re.compile(r"\bDiagnosis looked like\b", re.I), "Impression was"),
              (re.compile(r"\bOn examination\b", re.I), "On exam"),
              (re.compile(r"\bComplaint of\b", re.I), "Complains of"),
              (re.compile(r"\bPatient has\b", re.I), "Pt has")]
FILLERS = ("uh, ", "okay so ", "right, ", "so ", "yeah ")


def drop_dosage_form(chunk: str, rng) -> str:
    """`Tablet paracetamol` -> `paracetamol`. The crutch most like §6.6's header:
    a formatting cue that announces a drug name is coming."""
    return DOSAGE_FORM.sub("", chunk)


def rephrase_meal_timing(chunk: str, rng) -> str:
    for pattern, replacement in MEAL_TIMING:
        chunk = pattern.sub(replacement, chunk)
    return chunk


def rephrase_plan_verbs(chunk: str, rng) -> str:
    for pattern, replacement in PLAN_VERBS:
        chunk = pattern.sub(replacement, chunk)
    return chunk


def insert_filler(chunk: str, rng) -> str:
    """Dictation hesitations at a sentence start. Real transcripts carry them;
    the Eka Care rows have been lightly cleaned of them."""
    def once(match):
        return match.group(0) + rng.choice(FILLERS) if rng.random() < 0.35 else match.group(0)
    return re.sub(r"(?<=[.!?])\s+", once, chunk)


def punctuation_drift(chunk: str, rng) -> str:
    """Doubled spaces collapse, some sentence periods are lost. Both are ordinary
    ASR artefacts and both remove a boundary the model might be leaning on."""
    chunk = re.sub(r"[ \t]{2,}", " ", chunk)
    return re.sub(r"\.\s+(?=[a-z])", " ", chunk)


def lowercase_drift(chunk: str, rng) -> str:
    """ASR that does not capitalise reliably."""
    def once(match):
        word = match.group(0)
        return word.lower() if rng.random() < 0.3 else word
    return re.sub(r"(?<=[.!?]\s)[A-Z][a-z]+", once, chunk)


TRANSFORMS = (
    ("drop_dosage_form", drop_dosage_form),
    ("rephrase_meal_timing", rephrase_meal_timing),
    ("rephrase_plan_verbs", rephrase_plan_verbs),
    ("insert_filler", insert_filler),
    ("punctuation_drift", punctuation_drift),
    ("lowercase_drift", lowercase_drift),
)


def paraphrase(text: str, values: list[str], rng) -> tuple[str, list[str]]:
    """Re-dictate everything except the value spans. Returns (text, fired)."""
    spans = protected_spans(text, values)
    parts = segments(text, spans)
    fired = []
    for name, transform in TRANSFORMS:
        changed = False
        rebuilt = []
        for is_protected, chunk in parts:
            if is_protected:
                rebuilt.append((True, chunk))
                continue
            moved = transform(chunk, rng)
            changed = changed or moved != chunk
            rebuilt.append((False, moved))
        parts = rebuilt
        if changed:
            fired.append(name)
    return "".join(chunk for _, chunk in parts), fired


MIN_VARIED_SHARE = 0.80


def refuse_if_value_lost(case_id: str, after: str, values: list[str]) -> list[str]:
    """A paraphrase that dropped a label is a bug, not a harder test case."""
    return [f"{case_id}: paraphrase lost the gold value {value!r}"
            for value in values if value and value.lower() not in after.lower()]


def refuse_if_corpus_barely_moved(varied: int, total: int) -> list[str]:
    """The guard belongs at the corpus level, not the case.

    A case can legitimately be unchanged: case_036 carries no gold value and
    matches no trigger, so there is nothing to protect and nothing to re-dictate.
    Keeping it preserves the pairing and costs nothing - like a concordant pair,
    it carries no information either way. What would be fatal is an arm where
    most cases are unchanged, because the headline would then be a measurement of
    gold-v2 wearing a different name.
    """
    share = varied / total if total else 0.0
    if share < MIN_VARIED_SHARE:
        return [f"only {varied}/{total} cases ({share:.0%}) actually changed; below "
                f"{MIN_VARIED_SHARE:.0%} this arm is gold-v2 under another name"]
    return []


# ---------------------------------------------------------------------------
# Building the set
# ---------------------------------------------------------------------------

def carry_evidence(original: GoldField, note: str) -> GoldField:
    """Keep gold-v2's evidence span when it survived the re-dictation, and fall
    back to the value itself when it did not.

    The value is what `scoring.value_matches` compares, so this choice never
    moves a score. It decides only whether the labelling validator can run the
    production gate over this variant, which is the check that proves the set is
    labelled to the same standard as every other one.
    """
    if original.status is not Status.FOUND:
        return GoldField(value=None, evidence=None, status=Status.NOT_STATED)
    evidence = original.evidence or original.value or ""
    if evidence.lower() not in note.lower():
        evidence = original.value or ""
    # Recover the note's own casing, so the stored evidence is verbatim.
    found = re.search(re.escape(evidence), note, re.IGNORECASE)
    return GoldField(value=original.value,
                     evidence=found.group(0) if found else evidence,
                     status=Status.FOUND)


def build() -> tuple[GoldSet, dict]:
    source = json.loads(SOURCE_PATH.read_text(encoding="utf-8"))
    rng = random.Random(SEED)
    cases, fired_counts, problems, unchanged = [], {}, [], []
    for raw in source["cases"]:
        original = GoldCase.model_validate(raw)
        values = [f.value for f in original.ground_truth.values()
                  if f.status is Status.FOUND and f.value]
        note, fired = paraphrase(original.source_text, values, rng)
        problems += refuse_if_value_lost(original.case_id, note, values)
        if note == original.source_text:
            unchanged.append(original.case_id)
        for name in fired:
            fired_counts[name] = fired_counts.get(name, 0) + 1
        cases.append(GoldCase(
            case_id=original.case_id,
            source_text=note,
            ground_truth={name: carry_evidence(original.ground_truth[name], note)
                          for name in CRITICAL_FIELDS},
            labeller=original.labeller,
            labelled_at=original.labelled_at,
            notes=f"{original.notes or ''} #paraphrase transforms={'+'.join(fired) or 'none'}".strip()))
    provenance = {
        "dataset": source["provenance"]["dataset"],
        "derived_from": {"version": source["schema_version"],
                         "sha256": hashlib.sha256(
                             SOURCE_PATH.read_bytes()).hexdigest()},
        "arm": "near-distribution surface variation",
        "answers": "docs/forensic_audit.md section 3.2 - the near-distribution half "
                   "of the adversarial-testing claim, which did not exist",
        "seed": SEED,
        "transforms": [name for name, _ in TRANSFORMS],
        "transform_case_counts": fired_counts,
        "cases_unchanged": unchanged,
        "invariant": "every transform acts strictly outside the spans carrying a "
                     "gold value, so the value labels are byte-identical to "
                     "gold-v2 and the two sets are paired field for field",
        "labelling_rules": {
            "values": "unchanged from gold-v2, by construction",
            "evidence": "gold-v2's span where it survived the re-dictation, the "
                        "value itself otherwise; scoring compares values, so this "
                        "choice cannot move a score",
        },
        "measures": ["recall under near-distribution surface variation, paired "
                     "against the gold-v2 live run"],
        "does_not_measure": ["semantic paraphrase (the facts are stated in the "
                             "same words)", "code-switched or non-Latin dictation",
                             "Singapore polyclinic phrasing specifically"],
    }
    problems += refuse_if_corpus_barely_moved(len(cases) - len(unchanged), len(cases))
    report = {"problems": problems, "transform_case_counts": fired_counts,
              "cases": len(cases), "unchanged": unchanged,
              "varied": len(cases) - len(unchanged)}
    return GoldSet(schema_version=SCHEMA_VERSION, provenance=provenance,
                   cases=cases), report


def validate(goldset: GoldSet) -> list[str]:
    """The production gate, over this variant. Same function the pipeline runs."""
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
                problems.append(f"{case.case_id}.{name}: {gold.evidence!r} is not "
                                f"verbatim in the paraphrase ({span.code})")
            bad = verify_value(name, probe)
            if bad is not None:
                problems.append(f"{case.case_id}.{name}: value fails the pipeline's "
                                f"gate ({bad.code})")
    return problems


def labels_match_source(goldset: GoldSet) -> list[str]:
    """The pairing invariant, checked rather than trusted."""
    source = json.loads(SOURCE_PATH.read_text(encoding="utf-8"))
    by_id = {c["case_id"]: c for c in source["cases"]}
    problems = []
    if [c.case_id for c in goldset.cases] != list(by_id):
        problems.append("the case list differs from gold-v2, so the sets are not paired")
    for case in goldset.cases:
        for name in CRITICAL_FIELDS:
            mine = case.ground_truth[name]
            theirs = by_id[case.case_id]["ground_truth"][name]
            if (mine.value or None) != (theirs["value"] or None):
                problems.append(f"{case.case_id}.{name}: value drifted from gold-v2 "
                                f"({theirs['value']!r} -> {mine.value!r})")
    return problems


def seal(goldset: GoldSet, labeller: str) -> str:
    if V1_PATH.exists():
        raise SystemExit(f"{V1_PATH} already exists and is sealed; a correction goes "
                         "into a new version, never over the top of this one")
    provenance = dict(goldset.provenance)
    provenance.update({"sealed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                       "sealed_by": labeller,
                       "prompt_fingerprint": prompt_fingerprint()})
    sealed = GoldSet(schema_version=goldset.schema_version, provenance=provenance,
                     cases=goldset.cases)
    V1_PATH.write_text(sealed.model_dump_json(indent=1) + "\n", encoding="utf-8")
    digest = hashlib.sha256(V1_PATH.read_bytes()).hexdigest()
    V1_SHA_PATH.write_text(f"{digest}  {V1_PATH.name}\n", encoding="utf-8")
    for target in (V1_PATH, V1_SHA_PATH):
        os.chmod(target, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
    return digest


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=["report", "validate"])
    parser.add_argument("--seal", action="store_true")
    parser.add_argument("--labeller", default=None)
    args = parser.parse_args(argv)

    goldset, report = build()
    problems = report["problems"] + labels_match_source(goldset) + validate(goldset)

    print(f"{report['varied']}/{report['cases']} cases re-dictated from "
          f"{SOURCE_PATH.name}"
          + (f"; unchanged: {', '.join(report['unchanged'])}" if report["unchanged"] else ""),
          file=sys.stderr)
    for name, _ in TRANSFORMS:
        print(f"  {name:24s} fired on {report['transform_case_counts'].get(name, 0):>3} cases",
              file=sys.stderr)
    if problems:
        print(f"  {len(problems)} problems:", file=sys.stderr)
        for line in problems[:20]:
            print(f"    {line}", file=sys.stderr)

    if args.command == "report":
        sample = goldset.cases[0]
        print(f"\n  example {sample.case_id}:\n    {sample.source_text[:200]!r}",
              file=sys.stderr)
        sys.stdout.write(json.dumps(
            {"cases": report["cases"], "varied": report["varied"],
             "unchanged": report["unchanged"],
             "transforms": report["transform_case_counts"],
             "problems": len(problems), "sealed": False}, indent=2) + "\n")
        return EXIT_OK

    if problems:
        print("refusing to seal: fix the problems above", file=sys.stderr)
        return EXIT_NOT_READY
    if not args.seal:
        sys.stdout.write(json.dumps({"cases": report["cases"], "problems": 0,
                                     "sealed": False}, indent=2) + "\n")
        return EXIT_OK
    if not args.labeller:
        print("sealing requires --labeller: a derived corpus still has an author",
              file=sys.stderr)
        return EXIT_REFUSED
    digest = seal(goldset, args.labeller)
    print(f"sealed {V1_PATH.name}  sha256 {digest}", file=sys.stderr)
    sys.stdout.write(json.dumps({"cases": report["cases"], "problems": 0,
                                 "sealed": True, "sha256": digest,
                                 "schema_version": SCHEMA_VERSION}, indent=2) + "\n")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
