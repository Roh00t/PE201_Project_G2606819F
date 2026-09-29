#!/usr/bin/env python3
"""Two controls that were specified, measured, and refused. The measurement.

    ./.venv/bin/python evals/refused_controls.py              # writes the artefact
    ./.venv/bin/python evals/refused_controls.py --report      # re-reads it

No model calls. Everything here is computed from sealed corpora and finished runs
already on disk, which is why a refusal costs nothing to reproduce.

`guardrails.md` §6.10 cites these numbers and `tests/test_guardrails_doc.py`
checks them against this artefact, so a refusal cannot drift into prose the way an
unmeasured claim can.

## Why two refusals are worth more than two shipped controls

Both were proposed to reduce the 0.2517 silent-failure rate. Neither can, and the
reasons are different and instructive:

**The proximity gate** (wipe a field when an attribution, temporal or
contemplation cue sits near its evidence) targets a failure mode this corpus does
not contain. Across 67 Eka Care notes the words `father`, `mother`, `stopped`,
`discontinued` and `was on` occur **zero** times. Broaden the lexicon until it
fires and it inverts: in dictated prescriptions the cue *is* the positive signal -
"start Sibelium 10 mg at night", "has been taking Dolo 650" - so the gate wipes
correct answers and catches none.

**S-16** (flag a field the model left empty when the note mentions the thing) is
circular, and this is the finding worth the most. Its trigger word is
`\\ballerg\\w*` - and on the stripped corpus that word **is the header that was
stripped**. It fires on 20 of 20 intact notes, where the model already finds the
allergy, and on none of the four it missed. The control is loudest where it is
unnecessary and silent where it is needed.

**And that reframes §6.6.** After stripping, nothing in the text marks `Vicodin.`
as an allergy rather than another medication: 201 ALL-CAPS headers survive in the
intact corpus and 1 in the stripped one. The stripped variant's labels are carried
over from the intact notes - `evals/label_allergy_v1.py` uses one `LABELS` dict
for both and never reads the stripped text - so **the answers are not derivable
from the stripped notes at all**. The header was not a crutch the model leaned on
unnecessarily; it was the only signal separating an allergy from a medication. No
recall control can recover information the text no longer contains.

stdout: one JSON summary. stderr: the tables.
Exit codes: 0 measured; 2 inputs unusable; 6 internal error.
"""

import argparse
import json
import re
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "evals"))

from extract import CRITICAL_FIELDS  # noqa: E402
from scoring import value_matches  # noqa: E402

GOLD_DIR = ROOT / "data" / "gold_labels"
RESULTS = ROOT / "evals" / "results"
DEFAULT_OUT = RESULTS / "refused-controls"

EXIT_OK, EXIT_UNUSABLE, EXIT_INTERNAL = 0, 2, 6

# ---------------------------------------------------------------------------
# S-16, as specified in guardrails.md
# ---------------------------------------------------------------------------
# Copied from the specification rather than re-derived, so the measurement is of
# the control that was proposed and not of a variant that happens to work. It is
# NOT defined as `needs_second_pass`: naming it that would make
# `tests/test_guardrails_doc.py` treat the specification fence as shipped code and
# enforce identity with it, which would be a claim that this is implemented. It is
# refused, not implemented.
ALLERGY_HINTS = re.compile(
    r"\ballerg\w*|\bintoleran\w*|\breaction to\b|\bsensitivity to\b", re.I)

# The denial pattern the specification names, quoted from
# data/allergy_set/fetch_mtsamples.py:DENIAL. Quoted rather than imported because
# `src/` and `evals/` do not import from `data/`, and because the measurement must
# be of the pattern as it stood when the corpora were sealed.
DENIAL = re.compile(
    r"\bno\s+(?:known\s+)?(?:drug\s+|medication\s+|food\s+|medicine\s+)?allerg\w*"
    r"|\b(?:none|nka|nkda|nkma|no\s+known|not\s+known|denies|denied|negative|"
    r"no\s+drug|no\s+medication|no\s+food|nil|unknown|not\s+aware)\b", re.I)

CAPS_HEADER = re.compile(r"\b[A-Z][A-Z /]{3,}:")


def s16_trigger(note: str, allergy_value, denial=DENIAL) -> bool:
    """The specified trigger: an empty allergy, a hint in the note, no denial near it."""
    if allergy_value not in (None, ""):
        return False
    hint = ALLERGY_HINTS.search(note)
    if hint is None:
        return False
    window = note[max(0, hint.start() - 60):hint.end() + 60]
    return denial.search(window) is None


# ---------------------------------------------------------------------------
# The proximity gate, as specified
# ---------------------------------------------------------------------------
# Two lexicons. The first is the one proposed; the second is what the corpus
# actually says, and the contrast between them is the finding.
LITERAL_CUES = ("father", "mother", "son", "daughter", "husband", "wife",
                "brother", "sister", "stopped", "discontinued", "ceased",
                "until", "was on", "consider", "may start", "plan to start")
EXTENDED_CUES = LITERAL_CUES + (
    "previously", "was taking", "has been taking", "already on", "prior",
    "earlier", "in the past", "taper", "off", "stop", "suggested", "whether",
    "should", "may", "plan", "advised", "start", "family history")
WINDOWS = (5, 10, 15, 25)


def lexicon(cues) -> re.Pattern:
    return re.compile(r"\b(?:" + "|".join(re.escape(c) for c in cues) + r")\b", re.I)


def cue_near(note: str, evidence, pattern: re.Pattern, words: int | None) -> bool:
    """Is a cue within `words` tokens of the evidence? `None` means anywhere."""
    if not evidence:
        return False
    start = note.lower().find(evidence.lower())
    if start < 0:
        return False
    if words is None:
        return bool(pattern.search(note))
    before = note[:start].split()[-words:]
    after = note[start + len(evidence):].split()[:words]
    return bool(pattern.search(" ".join(before + after)))


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def load_gold(name: str) -> dict:
    return {c["case_id"]: c for c in
            json.loads((GOLD_DIR / f"{name}.json").read_text(encoding="utf-8"))["cases"]}


def load_run(name: str) -> dict:
    return {json.loads(line)["case_id"]: json.loads(line) for line in
            (RESULTS / name / "gated.jsonl").read_text(encoding="utf-8").splitlines()
            if line}


def verified_fields(gold: dict, run: dict):
    """(case_id, field, right, evidence) for every field the gate passed as VERIFIED."""
    for case_id, case in gold.items():
        if case_id not in run:
            continue
        codes = {g["field"]: g["code"] for g in run[case_id]["gate"]}
        for name in CRITICAL_FIELDS:
            if codes.get(name) != "VERIFIED":
                continue
            got = run[case_id]["extraction"][name] or {}
            truth = case["ground_truth"][name]
            right = (truth["status"] == "found"
                     and value_matches(name, got.get("value") or "",
                                       truth.get("value") or ""))
            yield case_id, name, right, got.get("evidence")


# ---------------------------------------------------------------------------
# Measurement 1: the proximity gate
# ---------------------------------------------------------------------------

def measure_proximity() -> dict:
    gold, run = load_gold("gold_v2"), load_run("gold-v2-live")
    fields = list(verified_fields(gold, run))
    silent = [(c, f, e) for c, f, right, e in fields if not right]
    correct = [(c, f, e) for c, f, right, e in fields if right]

    sweeps = {}
    for label, cues in (("literal", LITERAL_CUES), ("extended", EXTENDED_CUES)):
        pattern = lexicon(cues)
        rows = []
        for w in (*WINDOWS, None):
            caught = sum(1 for c, _, e in silent
                         if cue_near(gold[c]["source_text"], e, pattern, w))
            destroyed = sum(1 for c, _, e in correct
                            if cue_near(gold[c]["source_text"], e, pattern, w))
            rows.append({"window_words": w, "silent_caught": caught,
                         "correct_destroyed": destroyed})
        sweeps[label] = rows

    prevalence = {cue: sum(1 for c in gold.values()
                           if re.search(r"\b" + re.escape(cue) + r"\b",
                                        c["source_text"], re.I))
                  for cue in LITERAL_CUES}
    victims = [f"{c}.{f}" for c, f, e in correct
               if cue_near(gold[c]["source_text"], e, lexicon(EXTENDED_CUES), 5)]

    return {
        "verdict": "REFUSED",
        "population": {"silent_failures": len(silent), "correct_verified": len(correct)},
        "sweep": sweeps,
        "literal_cue_prevalence_in_67_notes": prevalence,
        "notes_containing_any_literal_cue": sum(
            1 for c in gold.values() if lexicon(LITERAL_CUES).search(c["source_text"])),
        "correct_fields_a_5_word_extended_gate_would_wipe": sorted(victims),
        "why": ("The literal lexicon catches nothing at any window because the words "
                "are absent from the corpus. The extended lexicon fires, and inverts: "
                "in a dictated prescription the cue introduces the drug being "
                "prescribed, so the gate wipes correct answers."),
    }


# ---------------------------------------------------------------------------
# Measurement 2: S-16
# ---------------------------------------------------------------------------

def measure_s16() -> dict:
    arms = {}
    for run_name, gold_name, role in (
            ("gold-v2-live", "gold_v2", "specificity: 0 allergy positives, so any firing is a false alarm"),
            ("leak-allergy-v1", "allergy_v1", "headers intact"),
            ("leak-allergy-v1-stripped", "allergy_v1_stripped", "sensitivity: the corpus where allergies are missed")):
        gold, run = load_gold(gold_name), load_run(run_name)
        fired, missed, caught = [], [], []
        for case_id, case in gold.items():
            if case_id not in run:
                continue
            got = (run[case_id]["extraction"]["allergy"] or {}).get("value")
            truth = case["ground_truth"]["allergy"]
            if s16_trigger(case["source_text"], got):
                fired.append(case_id)
            if truth["status"] == "found" and not got:
                missed.append(case_id)
                if s16_trigger(case["source_text"], got):
                    caught.append(case_id)
        arms[run_name] = {
            "role": role, "cases": len(gold),
            "allergy_positives_in_gold": sum(
                1 for c in gold.values() if c["ground_truth"]["allergy"]["status"] == "found"),
            "trigger_fired": len(fired), "fired_on": sorted(fired),
            "allergies_missed": len(missed), "missed": sorted(missed),
            "missed_and_flagged": len(caught),
            "hint_matches_note": sum(1 for c in gold.values()
                                     if ALLERGY_HINTS.search(c["source_text"])),
            "caps_headers_surviving": sum(len(CAPS_HEADER.findall(c["source_text"]))
                                          for c in gold.values()),
        }

    stripped = arms["leak-allergy-v1-stripped"]
    return {
        "verdict": "REFUSED",
        "arms": arms,
        "sensitivity": {"flagged": stripped["missed_and_flagged"],
                        "missed": stripped["allergies_missed"]},
        "why": ("The trigger word is the header that stripping removes. "
                "ALLERGY_HINTS matches 20 of 20 intact notes - where the model "
                "already finds the allergy - and none of the notes where it was "
                "missed. The control is loudest where it is unnecessary and silent "
                "where it is needed."),
        "reframes_6_6": (
            "After stripping, nothing marks a substance as an allergy rather than a "
            "medication: 201 ALL-CAPS headers survive intact against 1 stripped. The "
            "stripped variant's labels are carried over from the intact notes "
            "(evals/label_allergy_v1.py uses one LABELS dict for both and never reads "
            "the stripped text), so the answers are not derivable from the stripped "
            "notes. The header was the clinical signal, not a formatting crutch, and "
            "no recall control can recover information the text no longer contains."),
    }


def headline(proximity: dict, s16: dict) -> dict:
    """The scalars `guardrails.md` §6.10 cites, flat so the doc test can reach them."""
    def at(sweep, window, key):
        for row in sweep:
            if row["window_words"] == window:
                return row[key]
        raise KeyError(window)

    stripped = s16["arms"]["leak-allergy-v1-stripped"]
    intact = s16["arms"]["leak-allergy-v1"]
    return {
        "proximity_literal_caught_5w": at(proximity["sweep"]["literal"], 5, "silent_caught"),
        "proximity_literal_destroyed_25w": at(proximity["sweep"]["literal"], 25,
                                               "correct_destroyed"),
        "proximity_extended_caught_5w": at(proximity["sweep"]["extended"], 5, "silent_caught"),
        "proximity_extended_destroyed_5w": at(proximity["sweep"]["extended"], 5,
                                               "correct_destroyed"),
        "proximity_extended_destroyed_10w": at(proximity["sweep"]["extended"], 10,
                                                "correct_destroyed"),
        "literal_cues_absent_from_every_note": sum(
            1 for n in proximity["literal_cue_prevalence_in_67_notes"].values() if n == 0),
        "s16_sensitivity_flagged": s16["sensitivity"]["flagged"],
        "s16_allergies_missed": s16["sensitivity"]["missed"],
        "s16_hint_matches_intact": intact["hint_matches_note"],
        "s16_hint_matches_stripped": stripped["hint_matches_note"],
        "caps_headers_intact": intact["caps_headers_surviving"],
        "caps_headers_stripped": stripped["caps_headers_surviving"],
    }


def render(result: dict) -> str:
    lines = ["Two controls, specified and refused. No model calls.", ""]
    p = result["proximity_gate"]
    lines.append(f"PROXIMITY GATE - {p['verdict']}")
    lines.append(f"  population: {p['population']['silent_failures']} silent, "
                 f"{p['population']['correct_verified']} correct verified")
    for label in ("literal", "extended"):
        lines.append(f"  {label} lexicon:")
        lines.append("    window    caught   destroyed")
        for row in p["sweep"][label]:
            w = row["window_words"]
            lines.append(f"    {str(w) + ' words' if w else 'whole note':<11}"
                         f"{row['silent_caught']:>4}      {row['correct_destroyed']:>4}")
    absent = [c for c, n in p["literal_cue_prevalence_in_67_notes"].items() if n == 0]
    lines.append(f"  words absent from all 67 notes: {len(absent)} of "
                 f"{len(p['literal_cue_prevalence_in_67_notes'])} "
                 f"({', '.join(absent[:6])}...)")
    lines.append("")
    s = result["s16"]
    lines.append(f"S-16 RECALL CONTROL - {s['verdict']}")
    for name, arm in s["arms"].items():
        lines.append(f"  {name}")
        lines.append(f"    {arm['role']}")
        lines.append(f"    hint matches {arm['hint_matches_note']}/{arm['cases']} notes, "
                     f"ALL-CAPS headers surviving {arm['caps_headers_surviving']}")
        lines.append(f"    trigger fired {arm['trigger_fired']}, allergies missed "
                     f"{arm['allergies_missed']}, of those flagged "
                     f"{arm['missed_and_flagged']}")
    lines.append(f"  -> sensitivity {s['sensitivity']['flagged']}/{s['sensitivity']['missed']}")
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--report", action="store_true",
                        help="re-read the saved artefact instead of recomputing")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args(argv)

    try:
        if args.report:
            result = json.loads((args.out / "measurement.json").read_text(encoding="utf-8"))
        else:
            proximity, s16 = measure_proximity(), measure_s16()
            result = {
                "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "no_model_calls": True,
                # Flat scalars for the figures guardrails.md quotes. The sweeps
                # below are lists, and the doc test walks a path of string keys,
                # so a cited number has to be reachable without indexing a list.
                "headline": headline(proximity, s16),
                "proximity_gate": proximity,
                "s16": s16,
            }
            args.out.mkdir(parents=True, exist_ok=True)
            (args.out / "measurement.json").write_text(
                json.dumps(result, indent=2) + "\n", encoding="utf-8")
    except FileNotFoundError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return EXIT_UNUSABLE
    except Exception as exc:
        print(f"ERROR ERR_INTERNAL (exit {EXIT_INTERNAL}): {type(exc).__name__}",
              file=sys.stderr)
        if args.debug:
            traceback.print_exc()
        return EXIT_INTERNAL

    print(render(result), file=sys.stderr)
    sys.stdout.write(json.dumps(result, indent=2) + "\n")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
