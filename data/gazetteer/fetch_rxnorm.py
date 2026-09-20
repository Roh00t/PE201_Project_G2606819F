#!/usr/bin/env python3
"""Fetch the RxNorm ingredient list for the non-AI baseline's gazetteer.

    ./.venv/bin/python data/gazetteer/fetch_rxnorm.py

Writes `rxnorm_ingredients.txt` beside this script: one lower-cased name per
line, with a provenance header naming the source, the endpoint and the
retrieval date.

Why the list is fetched rather than typed. `evals/baseline.py` is the non-AI
comparator this project committed to in `project_proposal.md` section 4, and a
baseline is only informative if it was not built with knowledge of the answers.
A drug list written from memory by whoever has already read the gold labels is
contaminated, and would flatter the baseline exactly where the gold set is
hardest. RxNorm is external, versioned, publicly citable, and was chosen in the
proposal before any label existed.

It also has a known limitation that is itself a finding, so it is stated here
rather than discovered later: RxNorm is a US drug vocabulary and `tty=IN`
returns *ingredients*, not brand names. The Eka Care corpus is Indian dictation
full of local brands ("Dolo", "Moxclav", "Cepodem"), most of which RxNorm has
no entry for. `evals/baseline.py` reports that coverage explicitly.

Standard library only, so the repository keeps its dependency surface
(OWASP LLM04). Network access is needed once; the output file is committed so
the baseline runs offline afterwards.
"""

import json
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "rxnorm_ingredients.txt"
SOURCE = "https://rxnav.nlm.nih.gov/REST/allconcepts.json?tty=IN"
HOMEPAGE = "https://lhncbc.nlm.nih.gov/RxNav/"
TIMEOUT_S = 60

EXIT_OK = 0
EXIT_FAILED = 3


def fetch(url: str) -> dict:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
        return json.loads(response.read().decode("utf-8"))


def main(argv=None) -> int:
    try:
        payload = fetch(SOURCE)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        print(f"could not fetch {SOURCE}: {type(exc).__name__}", file=sys.stderr)
        print("the baseline still runs without a gazetteer (pattern-only arm)", file=sys.stderr)
        return EXIT_FAILED

    concepts = (payload.get("minConceptGroup") or {}).get("minConcept") or []
    names = sorted({(c.get("name") or "").strip().casefold()
                    for c in concepts if (c.get("name") or "").strip()})
    retrieved = datetime.now(timezone.utc).isoformat(timespec="seconds")
    header = [
        "# RxNorm ingredient names (tty=IN), for evals/baseline.py",
        f"# source:    {SOURCE}",
        f"# about:     {HOMEPAGE}",
        "# publisher: U.S. National Library of Medicine, RxNav",
        f"# retrieved: {retrieved}",
        f"# concepts:  {len(concepts)} returned, {len(names)} distinct names",
        "# licence:   RxNorm is in the public domain; RxNav API terms apply.",
        "#",
        "# This list is INGREDIENTS, not brands. It is external to this project on",
        "# purpose: a gazetteer written from memory by someone who has already read",
        "# the gold labels would make the baseline look better than it is.",
    ]
    OUT.write_text("\n".join(header) + "\n" + "\n".join(names) + "\n", encoding="utf-8")
    print(f"{len(names)} names -> {OUT.relative_to(HERE.parent.parent)}", file=sys.stderr)
    sys.stdout.write(str(OUT) + "\n")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
