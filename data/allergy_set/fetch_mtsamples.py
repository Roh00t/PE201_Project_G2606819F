#!/usr/bin/env python3
"""Pull allergy-bearing notes from MTSamples for the leakage report.

    ./.venv/bin/python data/allergy_set/fetch_mtsamples.py              # writes candidates.json
    ./.venv/bin/python data/allergy_set/fetch_mtsamples.py --max-rows 600   # a quick look

`project_proposal.md` §6 promises two things this corpus provides and Eka Care
cannot. First, an **allergy** field with actual positives: all 67 Eka Care notes
contain exactly one allergy mention and it is a denial, so allergy recall there
is unmeasurable. Second, the **leakage** report §6 commits to, because MTSamples
notes carry ALL-CAPS section headers - `ALLERGIES:`, `MEDICATIONS:` - and a model
that reads the header rather than the sentence will score worse once the headers
are stripped. That before-and-after is the point.

## Provenance, recorded the same way gold-v1 records it

Source: `harishnair04/mtsamples` on Hugging Face, read through the public
datasets-server rows API - the same mechanism and the same discipline as the Eka
Care pull. Every selected row keeps its `row_idx` and an md5 of its
transcription, so drift between this pull and any later one is detectable rather
than silent. MTSamples itself is a public collection of de-identified sample
transcriptions.

## The filter, stated exactly

A row is a candidate when all of these hold:

  1. its transcription is between `--min-chars` and `--max-chars` long - long
     enough to be a real note, short enough to hand-label carefully;
  2. it carries an **ALL-CAPS** `ALLERGIES:` / `ALLERGY:` / `ALLERGIES TO
     MEDICATIONS:` header. Lower-case prose mentions are excluded because the
     header is the thing the leakage report strips;
  3. the clause after that header is **not a denial** and **names a substance**.
     "None", "NKA", "NKDA", "no known drug allergies" and "not known drug
     allergies" are what almost all of these notes say, and a denial is
     `not_stated` under `FIELD_RULES['allergy']`, not an allergy. "Refer to
     chart" points at a document and "seasonal allergies" names a season, so
     neither is a substance either;
  4. the clause is at most `--max-substance` characters, so the labelled span is
     a substance and not a paragraph;
  5. its transcription has not already been selected. MTSamples repeats notes
     across specialties, and a duplicate in an evaluation set inflates n while
     correlating errors across cases that only look independent. The duplicate
     rows each surviving note absorbed are recorded in `duplicate_row_idx`.

Yield is low by nature: roughly one row in 150 states a positive allergy under a
header. That is worth knowing on its own - it is why this set is small, and why
the allergy field could not simply be measured on whatever corpus was to hand.

Standard library only. Network access is needed once; `candidates.json` is
committed so the labelling and the evaluation run offline afterwards.
"""

import argparse
import hashlib
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
OUT = HERE / "candidates.json"

DATASET = "harishnair04/mtsamples"
CONFIG, SPLIT = "default", "train"
ROWS_API = "https://datasets-server.huggingface.co/rows"
PAGE = 100
TIMEOUT_S = 90

EXIT_OK = 0
EXIT_FAILED = 3

# ALL-CAPS only: the header is what gets stripped, so a lower-case prose mention
# is a different phenomenon and is deliberately out of scope.
HEADER = re.compile(r"\bALLERG(?:Y|IES)(?:\s+TO\s+MEDICATIONS?)?\s*:\s*,?\s*")
# Searched anywhere in the clause, not anchored: "She has no known medicine
# allergies" is a denial that an anchored test walks straight past. The list grew
# after a first pass put NKA, "NOT KNOWN DRUG ALLERGIES" and "Refer to chart"
# into the candidate pool - a filter whose job is to find positives has to be
# judged on the false positives it lets through, so each variant is named. The
# "no allergies" branch is written before the word-boundary alternation and ends
# in \w* on purpose: `no\s+allerg\b` cannot match "NO ALLERGIES", because there
# is no boundary between "allerg" and "ies", and that let one denial through.
DENIAL = re.compile(
    r"\bno\s+(?:known\s+)?(?:drug\s+|medication\s+|food\s+|medicine\s+)?allerg\w*"
    r"|\b(?:none|nka|nkda|nkma|no\s+known|not\s+known|denies|denied|negative|"
    r"no\s+drug|no\s+medication|no\s+food|nil|unknown|not\s+aware)\b", re.I)
# A pointer or a category is not a substance. FIELD_RULES['allergy'] wants "a drug
# or other substance", so "Refer to chart" names nothing and "seasonal allergies"
# names a season.
NO_SUBSTANCE = re.compile(
    r"\b(?:refer\s+to|see\s+(?:chart|list|above)|per\s+chart|as\s+(?:above|outlined)|"
    r"seasonal|environmental|unable\s+to|chart\b|multiple\b|various\b)", re.I)
MEDS_HEADER = re.compile(r"\bMEDICATIONS?\s*:")


def fetch_page(offset: int, length: int) -> dict:
    query = urllib.parse.urlencode({"dataset": DATASET, "config": CONFIG, "split": SPLIT,
                                    "offset": offset, "length": length})
    request = urllib.request.Request(f"{ROWS_API}?{query}",
                                    headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
        return json.loads(response.read().decode("utf-8"))


def allergy_clause(text: str) -> tuple:
    """(header, substance) for the first positive ALL-CAPS allergy header."""
    for match in HEADER.finditer(text):
        clause = re.split(r"[.;]|\n", text[match.end():], maxsplit=1)[0].strip().rstrip(",")
        if not clause or DENIAL.search(clause) or NO_SUBSTANCE.search(clause):
            continue
        return match.group(0).strip(), clause
    return None, None


def select(rows, min_chars: int, max_chars: int, max_substance: int) -> list:
    """Candidates, deduplicated by transcription content.

    MTSamples repeats the same transcription under several specialties - rows
    117, 3016 and 3832 are one note, and so are 1448/3179 and 1449/2969. Left in,
    duplicates inflate n and correlate errors across what look like independent
    cases, which is the quiet way an evaluation set stops measuring anything. The
    lowest row_idx wins, so the choice is deterministic.
    """
    out, seen = [], {}
    for row_idx, row in rows:
        text = (row.get("transcription") or "").strip()
        if not (min_chars <= len(text) <= max_chars):
            continue
        header, substance = allergy_clause(text)
        if not substance or len(substance) > max_substance:
            continue
        digest = hashlib.md5(text.encode("utf-8")).hexdigest()
        if digest in seen:
            seen[digest].append(row_idx)
            continue
        seen[digest] = []
        out.append({
            "row_idx": row_idx,
            "text_md5": digest,
            "chars": len(text),
            "words": len(text.split()),
            "specialty": (row.get("medical_specialty") or "").strip(),
            "sample_name": (row.get("sample_name") or "").strip(),
            "allergy_header": header,
            "allergy_substance": substance,
            "has_medications_header": bool(MEDS_HEADER.search(text)),
            "source_text": text,
        })
    for candidate in out:
        candidate["duplicate_row_idx"] = seen.get(candidate["text_md5"], [])
    return out


def refilter(args) -> int:
    """Tighten the filter without re-pulling 4,000 rows.

    Sound only because the filter has become strictly stricter: the saved pool is
    a superset of what the new rule accepts. A looser rule would need a fresh
    pull, and the provenance below says which happened so the distinction is not
    left to memory.
    """
    if not OUT.is_file():
        print(f"{OUT} not found: pull once before re-filtering", file=sys.stderr)
        return EXIT_FAILED
    document = json.loads(OUT.read_text(encoding="utf-8"))
    before = document["candidates"]
    rows = [(c["row_idx"], {"transcription": c["source_text"],
                            "medical_specialty": c.get("specialty"),
                            "sample_name": c.get("sample_name")}) for c in before]
    kept = select(rows, args.min_chars, args.max_chars, args.max_substance)
    dropped = sorted({c["row_idx"] for c in before} - {c["row_idx"] for c in kept})
    document["provenance"].setdefault("refilters", []).append({
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "reason": "the first filter admitted denials (NKA, 'NOT KNOWN DRUG "
                  "ALLERGIES') and non-substances ('Refer to chart', 'seasonal')",
        "kept": len(kept), "dropped_row_idx": dropped,
        "denial": DENIAL.pattern, "no_substance": NO_SUBSTANCE.pattern,
    })
    document["candidates"] = kept
    OUT.write_text(json.dumps(document, indent=1) + "\n", encoding="utf-8")
    print(f"re-filtered: {len(before)} -> {len(kept)} candidates "
          f"({len(dropped)} dropped)", file=sys.stderr)
    sys.stdout.write(str(OUT) + "\n")
    return EXIT_OK


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--max-rows", type=int, default=5000)
    parser.add_argument("--min-chars", type=int, default=400)
    parser.add_argument("--max-chars", type=int, default=3000)
    parser.add_argument("--max-substance", type=int, default=90)
    parser.add_argument("--refilter", action="store_true",
                        help="re-apply the filter to the committed candidates.json "
                             "instead of pulling again, and record that this is "
                             "what happened")
    args = parser.parse_args(argv)

    if args.refilter:
        return refilter(args)

    rows, scanned = [], 0
    try:
        first = fetch_page(0, 1)
        total = int(first.get("num_rows_total") or 0)
        limit = min(total, args.max_rows)
        print(f"{DATASET}: {total} rows; scanning {limit}", file=sys.stderr)
        for offset in range(0, limit, PAGE):
            page = fetch_page(offset, min(PAGE, limit - offset))
            rows += [(item["row_idx"], item["row"]) for item in page["rows"]]
            scanned = len(rows)
            print(f"\r  scanned {scanned}/{limit}", end="", file=sys.stderr, flush=True)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, KeyError) as exc:
        print(f"\nfetch failed after {scanned} rows: {type(exc).__name__}", file=sys.stderr)
        if not rows:
            return EXIT_FAILED
        print("continuing with what arrived", file=sys.stderr)

    candidates = select(rows, args.min_chars, args.max_chars, args.max_substance)
    document = {
        "provenance": {
            "dataset": DATASET, "config": CONFIG, "split": SPLIT,
            "via": "datasets-server rows API",
            "pulled_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "rows_scanned": len(rows),
            "rows_in_split": total,
            "filter": {"min_chars": args.min_chars, "max_chars": args.max_chars,
                       "max_substance_chars": args.max_substance,
                       "header": HEADER.pattern, "denial": DENIAL.pattern,
                       "no_substance": NO_SUBSTANCE.pattern,
                       "note": "ALL-CAPS header only; denial and no-substance "
                               "searched anywhere in the clause, not anchored"},
            "yield": f"{len(candidates)}/{len(rows)}",
        },
        "candidates": candidates,
    }
    OUT.write_text(json.dumps(document, indent=1) + "\n", encoding="utf-8")
    print(f"\n{len(candidates)} candidates from {len(rows)} rows "
          f"-> {OUT.relative_to(ROOT)}", file=sys.stderr)
    by_specialty = {}
    for candidate in candidates:
        by_specialty[candidate["specialty"]] = by_specialty.get(candidate["specialty"], 0) + 1
    for specialty, count in sorted(by_specialty.items(), key=lambda kv: -kv[1])[:8]:
        print(f"  {count:3}  {specialty}", file=sys.stderr)
    sys.stdout.write(str(OUT) + "\n")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
