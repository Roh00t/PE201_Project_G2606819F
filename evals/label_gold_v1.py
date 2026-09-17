#!/usr/bin/env python3
"""Hand-labelling infrastructure for the gold-v1 evaluation set.

Five subcommands:

    profile    length / word / entity distribution over every row; writes nothing
    template   profile, draw a seeded sample of N, write gold_v1_template.json
    label      interactive pass over the cases, with the verbatim gate on
    validate   refuse to let an incomplete or unverifiable file be tagged
    stats      progress and label distribution

The gate applied while labelling is imported from src/guardrails.py. It is
the identical function the pipeline runs at inference time, not a
reimplementation of it. If a human cannot produce a span that survives the
gate, the model will not be asked to either - that is the point, and it is
why the gold set has to be built with the gate switched on rather than
cleaned up afterwards.

Typical week 1:

    # HF_TOKEN=<token> in .env at the repo root
    python evals/label_gold_v1.py profile
    python evals/label_gold_v1.py template            # = --script latin --n all
    python evals/label_gold_v1.py label --labeller rohit
    python evals/label_gold_v1.py validate --seal
    git tag -a gold-v1 ...          (validate prints the exact commands)

The whole 156-row split is profiled before anything is written. The gold set
is then every Latin-script row (67); `--n 30 --seed 42` draws a seeded sample
from that pool instead, and `--script any` includes the Devanagari rows.
"""

import argparse
import hashlib
import json
import os
import platform
import random
import re
import statistics
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from guardrails import BLANKED, verify_evidence  # noqa: E402
from schema import (  # noqa: E402
    CRITICAL_FIELDS, FIELD_RULES, GoldCase, GoldField, GoldSet, Status, render_field_rules,
)

DATASET = "ekacare/clinical_note_generation_dataset"
DATASET_REVISION = "662c58a1d03255e26461519be4c9c4e597fdd3fd"  # pin it or it is not reproducible
TEXT_FIELD = "text"
ID_FIELD = "session_id"
MD5_FIELD = "text_md5"
RUBRICS_FIELD = "rubrics"

# The dataset ships its own reference labels inside each row's rubric text,
# as "Category ID: <name>" lines. Mapping them onto our four fields gives a
# real entity count to profile with, instead of guessing by regex.
REFERENCE_CATEGORIES = {
    "medication": {"medication_name"},
    "dose": {"medication_dose"},
    "frequency": {"medication_frequency"},
    "allergy": {"drugAllergy_name", "foodOtherAllergy_name"},
    "current_med": {"current_medication_name"},
}
RUBRIC_CATEGORY = re.compile(r"Category ID:\s*(\S+)")

API = "https://datasets-server.huggingface.co"
EXPECTED_ROWS = 156  # per the dataset card at DATASET_REVISION
DEFAULT_SEED = 42
# Decided 2026-09-16 after profiling: the gold set is every Latin-script row.
DEFAULT_SCRIPT = "latin"
DEFAULT_N = "all"

# Rows whose text the assistant read (first ~420 chars) during format
# inspection on 2026-09-16, before the pool was chosen. They were outside the
# seed-42 mixed sample at the time and are gold cases now. Recorded so the
# exposure is on file rather than remembered. No prompt wording was taken
# from them; the examples in FIELD_RULES come from the rubric boilerplate
# that is identical across all 156 rows.
INSPECTED_ROW_IDX = [0, 2, 3, 9, 13]

# Representativeness thresholds for the sample-vs-population check.
MEDIAN_DRIFT = 0.25        # sample median words/turns more than 25% off
PREVALENCE_DRIFT = 0.15    # an entity proxy present in 15 points more/fewer rows
CODE_MIXED_MIN_HITS = 3    # romanised-Hindi marker tokens before we flag a row

GOLD_DIR = ROOT / "data" / "gold_labels"
TEMPLATE_PATH = GOLD_DIR / "gold_v1_template.json"
SEALED_PATH = GOLD_DIR / "gold_v1.json"

SCRIPT = "./.venv/bin/python evals/label_gold_v1.py"
# No `export HF_TOKEN=...` here: the token is read from .env, and a pasted
# placeholder export would override it.
TEMPLATE_COMMAND = f"{SCRIPT} template --script {DEFAULT_SCRIPT} --n {DEFAULT_N}"

STATUS_KEYS = {
    "f": Status.FOUND,
    "n": Status.NOT_STATED,
    "u": Status.UNSURE,
}


def _rel(path: Path) -> str:
    """Display paths relative to the repo when we can, absolute when we cannot."""
    path = Path(path)
    try:
        return str(path.resolve().relative_to(ROOT))
    except ValueError:
        return str(path)


# ---------------------------------------------------------------------------
# Ingestion - always the whole split. Selection happens afterwards, so the
# profile describes the population the sample is drawn from.
# ---------------------------------------------------------------------------

ROWS_PAGE = 100  # datasets-server hard cap per request


def _looks_like_placeholder(token: str) -> bool:
    return "..." in token or "<" in token or len(token) < 20


def _hf_token() -> str | None:
    for var in ("HF_TOKEN", "HUGGINGFACE_HUB_TOKEN", "HUGGING_FACE_HUB_TOKEN"):
        if os.environ.get(var):
            # A shell variable beats .env, so a pasted `export HF_TOKEN=hf_...`
            # silently masks the real token for the rest of that session.
            if _looks_like_placeholder(os.environ[var]):
                sys.exit(
                    f"{var} in this shell is set to the placeholder {os.environ[var]!r}, "
                    "and a shell variable takes precedence over .env.\n"
                    f"Clear it with:  unset {var}\n"
                    "then re-run - the token in .env will be used."
                )
            return os.environ[var]
    cached = Path.home() / ".cache" / "huggingface" / "token"
    if cached.is_file() and cached.read_text().strip():
        return cached.read_text().strip()
    dotenv = ROOT / ".env"
    if dotenv.is_file():
        for line in dotenv.read_text().splitlines():
            key, _, value = line.strip().partition("=")
            if key.strip() == "HF_TOKEN" and value:
                token = value.strip().strip("'\"")
                if _looks_like_placeholder(token):
                    sys.exit(f"HF_TOKEN in {_rel(dotenv)} is a placeholder - paste the real token.")
                return token
    return None


GATED_HELP = f"""\
{DATASET} is a GATED dataset. Two things must both be true:

  1. the account has accepted the terms - sign in, open
     https://huggingface.co/datasets/{DATASET}
     and click through the access prompt (approval is automatic)

  2. the token is allowed to read gated repos it does not own. Either
     - a classic token with role "Read", or
     - a fine-grained token with "Read access to contents of all public
       gated repos you can access" ticked under User permissions
     Create or edit at https://huggingface.co/settings/tokens

  then put the real token in .env at the repo root as HF_TOKEN=<token>\
"""


def _diagnose_token(token: str) -> str:
    """Name the likely cause instead of listing every possible one."""
    request = urllib.request.Request(
        "https://huggingface.co/api/whoami-v2",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            who = json.load(response)
    except urllib.error.HTTPError as exc:
        return f"diagnosis: the token itself was rejected by whoami ({exc.code}) - it is invalid or revoked."
    except urllib.error.URLError:
        return "diagnosis: could not reach huggingface.co to inspect the token."

    access = who.get("auth", {}).get("accessToken", {})
    role = access.get("role")
    lines = [f"diagnosis: token is valid, account '{who.get('name')}', role '{role}'."]
    if role == "fineGrained":
        scopes = (access.get("fineGrained") or {}).get("global") or []
        if not any("gated" in s or s.startswith("repo") for s in scopes):
            lines.append(
                f"  its global scopes are {scopes} - nothing there reads gated repos\n"
                "  owned by someone else. That alone produces this error: fix step 2."
            )
    else:
        lines.append("  token scope looks sufficient, so step 1 (accepting the terms) is the likely gap.")
    return "\n".join(lines)


def _api_get(path: str, token: str | None) -> dict:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    request = urllib.request.Request(f"{API}/{path}", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:300]
        if exc.code in (401, 403) or "gated" in body:
            diagnosis = _diagnose_token(token) if token else "diagnosis: no token was sent."
            sys.exit(f"Hugging Face returned {exc.code}: {body}\n\n{diagnosis}\n\n{GATED_HELP}")
        sys.exit(f"Hugging Face returned {exc.code}: {body}")
    except urllib.error.URLError as exc:
        sys.exit(f"Could not reach {API}: {exc.reason}")


def discover_split(token: str | None) -> tuple[str, str]:
    """Do not hardcode config/split names we have never actually seen."""
    payload = _api_get(f"splits?dataset={urllib.parse.quote(DATASET, safe='')}", token)
    splits = payload.get("splits", [])
    if not splits:
        sys.exit(f"No splits reported for {DATASET}: {payload}")
    preferred = next((s for s in splits if s.get("split") == "test"), splits[0])
    return preferred["config"], preferred["split"]


def _base_provenance(**extra) -> dict:
    return {
        "dataset": DATASET,
        "revision": DATASET_REVISION,
        "config": "default",
        "split": "test",
        "rows_in_split": EXPECTED_ROWS,
        # Overwritten in cmd_template with what the per-row check measured.
        "language": "dataset card declares language:en (not verified per row here)",
        "pulled_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        **extra,
    }


def fetch_all_rows(token: str) -> tuple[list[dict], dict]:
    config, split = discover_split(token)
    rows: list[dict] = []
    offset, total = 0, None
    while total is None or offset < total:
        query = urllib.parse.urlencode(
            {"dataset": DATASET, "config": config, "split": split,
             "offset": offset, "length": ROWS_PAGE}
        )
        payload = _api_get(f"rows?{query}", token)
        total = payload.get("num_rows_total") or 0
        page = payload.get("rows", [])
        if not page:
            break

        # The rows API truncates oversized cells. A truncated transcript
        # turns every evidence span past the cut into a false negative, so
        # this is fatal rather than a warning.
        truncated = [r["row_idx"] for r in page if TEXT_FIELD in (r.get("truncated_cells") or [])]
        if truncated:
            sys.exit(
                f"The rows API truncated `{TEXT_FIELD}` on rows {truncated}. Re-pull with\n"
                "  pip install datasets && python evals/label_gold_v1.py template --via datasets"
            )
        for record in page:
            rows.append({**record["row"], "_row_idx": record["row_idx"]})
        offset += len(page)

    return rows, _base_provenance(
        config=config, split=split, rows_in_split=total,
        via="datasets-server rows API",
        # The rows API only serves the default branch. The md5 per row in
        # row_map is what actually pins the text; this says so plainly.
        revision_enforced=False,
    )


def fetch_via_datasets() -> tuple[list[dict], dict]:
    """Fallback that reads the parquet shards directly. Needs `pip install datasets`."""
    try:
        from datasets import load_dataset
    except ImportError:
        sys.exit("`--via datasets` needs: pip install datasets")

    dataset = load_dataset(DATASET, split="test", revision=DATASET_REVISION, token=_hf_token())
    rows = [{**dataset[i], "_row_idx": i} for i in range(len(dataset))]
    return rows, _base_provenance(
        rows_in_split=len(rows), via="datasets library (parquet)", revision_enforced=True,
    )


def rows_from_local_notes(directory: Path) -> tuple[list[dict], dict]:
    """Every .txt in a directory, as rows.

    Used to exercise this tool without touching the gated dataset, and later
    for the MTSamples stress set, whose text we do not redistribute.
    """
    directory = directory.resolve()
    paths = sorted(directory.glob("*.txt"))
    if not paths:
        sys.exit(f"No .txt files in {directory}")
    rows = []
    for index, path in enumerate(paths):
        text = path.read_text()
        rows.append({
            ID_FIELD: path.stem,
            TEXT_FIELD: text,
            MD5_FIELD: hashlib.md5(text.encode()).hexdigest(),
            "_row_idx": index,
        })
    return rows, {
        "dataset": f"local:{_rel(directory)}",
        "revision": None,
        "rows_in_split": len(rows),
        "language": "not asserted for local notes; see language_check",
        "via": "local .txt files",
        "pulled_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def load_rows(args) -> tuple[list[dict], dict]:
    if args.from_local_notes:
        return rows_from_local_notes(Path(args.from_local_notes))
    if args.via == "datasets":
        return fetch_via_datasets()
    token = _hf_token()
    if not token:
        sys.exit(f"No Hugging Face token found.\n\n{GATED_HELP}")
    return fetch_all_rows(token)


# ---------------------------------------------------------------------------
# Profiling - deterministic, no model. The gold set has to be chosen before
# any model has seen these notes, so the profile cannot use one either.
# ---------------------------------------------------------------------------
#
# Two kinds of entity count. REFERENCE counts come from the dataset's own
# rubric categories and are the ones to trust. The regex PROXIES are
# English-only and undercount (a dictated "paracetamol 500" has no unit);
# they remain for local notes, which have no rubrics. Neither says what is in
# a given note - that is what labelling is for.

ENTITY_PROXIES = {
    "dose": re.compile(
        r"\b\d+(?:\.\d+)?\s?(?:mg|mcg|µg|ug|gm|g|ml|iu|units?)\b", re.I),
    "frequency": re.compile(
        # Compound phrases first, so "twice daily" is one mention, not two.
        r"\b(?:once|twice|thrice)\s+(?:a\s+)?(?:daily|day|weekly|week)\b"
        r"|\b(?:od|bd|bid|tds|tid|qid|qds|qd|hs|qhs|prn|sos|once|twice|thrice|"
        r"daily|nightly|weekly)\b"
        r"|\b[01][-–][01][-–][01]\b"                # 1-0-1 regimen notation
        r"|\b\d+\s*times?\s+(?:a|per)\s+(?:day|week)\b"
        r"|\bevery\s+\d+\s*(?:hours?|hrs?)\b", re.I),
    "dosage_form": re.compile(
        r"\b(?:tab(?:let)?s?|cap(?:sule)?s?|syrups?|inj(?:ection)?s?|drops?|"
        r"ointments?|creams?|inhalers?|sachets?)\b", re.I),
    "drug_suffix": re.compile(
        r"\b[a-z]{3,}(?:cillin|mycin|floxacin|azole|pril|sartan|olol|dipine|"
        r"statin|formin|gliptin|gliflozin|tidine|cycline|profen|fenac|cetamol|"
        r"zepam|triptan|lukast|tadine)\b", re.I),
    "allergy_any": re.compile(r"\ballerg\w*|\bnkda\b", re.I),
    "allergy_negated": re.compile(
        r"\b(?:no|denies|denied|nil|not|without|none)\b[^.?!\n]{0,40}\ballerg\w*"
        r"|\bnkda\b|\bno known drug", re.I),
}

# Distinctive romanised-Hindi tokens. Deliberately excludes short words that
# collide with English or with clinical shorthand.
HINGLISH_MARKERS = frozenset(
    "hai hain nahi nahin haan kya aap aapko mujhe mera meri dard bukhar dawai "
    "dawa theek thik accha acha kaise kitne abhi bahut karo karna lena lijiye "
    "raha rahi gaya hua hui".split()
)
# Marker words that separate the two Devanagari languages seen in this
# split. Heuristic: counts, not a language-ID model.
MARATHI_MARKERS = frozenset("आहे आहेत म्हणजे झाले काय मला तुम्ही होते आणि असं नाहीये".split())
HINDI_MARKERS = frozenset("है हैं नहीं मुझे आप क्या रहा रही और था".split())

SPEAKER_TURN = re.compile(r"^\s*\[?[A-Za-z][A-Za-z .]{0,24}\]?\s*:", re.M)
WORD = re.compile(r"[A-Za-zऀ-ॿ']+")


def _script_group(text: str) -> str:
    if any("\u0900" <= ch <= "\u097F" for ch in text):
        return "devanagari"
    if any(ch.isalpha() and not ch.isascii() for ch in text):
        return "other-script"
    return "latin"


def _reference_counts(row: dict) -> dict:
    categories = RUBRIC_CATEGORY.findall(row.get(RUBRICS_FIELD) or "")
    return {
        field: sum(1 for c in categories if c in names)
        for field, names in REFERENCE_CATEGORIES.items()
    }


def _profile_row(row: dict) -> dict:
    text = row.get(TEXT_FIELD) or ""
    words = WORD.findall(text)
    lowered = [w.lower() for w in words]
    tokens = set(text.replace("\u0964", " ").split())
    recorded_md5 = row.get(MD5_FIELD)
    return {
        "script": _script_group(text),
        "reference": _reference_counts(row),
        "marathi_hits": len(tokens & MARATHI_MARKERS),
        "hindi_hits": len(tokens & HINDI_MARKERS),
        "row_idx": row["_row_idx"],
        "chars": len(text),
        "words": len(words),
        "turns": len(SPEAKER_TURN.findall(text)),
        "entities": {name: len(rx.findall(text)) for name, rx in ENTITY_PROXIES.items()},
        "devanagari": any("ऀ" <= ch <= "ॿ" for ch in text),
        "non_ascii_letters": sum(1 for ch in text if ch.isalpha() and not ch.isascii()),
        "hinglish_hits": sum(1 for w in lowered if w in HINGLISH_MARKERS),
        "md5": recorded_md5 or hashlib.md5(text.encode()).hexdigest(),
        "md5_ok": recorded_md5 is None or recorded_md5 == hashlib.md5(text.encode()).hexdigest(),
    }


def _quantiles(values: list[int]) -> dict:
    if not values:
        return {}
    ordered = sorted(values)
    cuts = statistics.quantiles(ordered, n=4) if len(ordered) > 1 else [ordered[0]] * 3
    return {
        "min": ordered[0], "p25": round(cuts[0]), "median": round(statistics.median(ordered)),
        "p75": round(cuts[2]), "max": ordered[-1], "mean": round(statistics.fmean(ordered), 1),
    }


def summarise(profiles: list[dict]) -> dict:
    n = len(profiles)
    code_mixed = [p["row_idx"] for p in profiles if p["hinglish_hits"] >= CODE_MIXED_MIN_HITS]
    return {
        "rows": n,
        "chars": _quantiles([p["chars"] for p in profiles]),
        "words": _quantiles([p["words"] for p in profiles]),
        "turns": _quantiles([p["turns"] for p in profiles]),
        "reference_prevalence": {
            name: round(sum(1 for p in profiles if p["reference"][name]) / n, 3)
            for name in REFERENCE_CATEGORIES
        },
        "medications_per_row": _quantiles([p["reference"]["medication"] for p in profiles]),
        "entity_prevalence": {
            name: round(sum(1 for p in profiles if p["entities"][name]) / n, 3)
            for name in ENTITY_PROXIES
        },
        "entity_mean_per_row": {
            name: round(statistics.fmean(p["entities"][name] for p in profiles), 2)
            for name in ENTITY_PROXIES
        },
        "language_check": {
            "script_groups": {
                g: sum(1 for p in profiles if p["script"] == g)
                for g in ("latin", "devanagari", "other-script")
            },
            "devanagari_likely_marathi": sum(
                1 for p in profiles if p["devanagari"] and p["marathi_hits"] > p["hindi_hits"]),
            "devanagari_likely_hindi": sum(
                1 for p in profiles if p["devanagari"] and p["marathi_hits"] <= p["hindi_hits"]),
            "rows_with_devanagari": sum(1 for p in profiles if p["devanagari"]),
            "rows_possibly_code_mixed": len(code_mixed),
            "code_mixed_row_idx": code_mixed[:20],
            "rows_ascii_letters_only": sum(1 for p in profiles if p["non_ascii_letters"] == 0),
            "method": (
                f"Devanagari codepoints, non-ASCII letters, >= {CODE_MIXED_MIN_HITS} "
                "romanised-Hindi marker tokens per row, and Marathi-vs-Hindi marker "
                "words. A heuristic, not a language-ID model."
            ),
        },
        "integrity": {
            "md5_mismatches": sum(1 for p in profiles if not p["md5_ok"]),
            "duplicate_texts": n - len({p["md5"] for p in profiles}),
        },
    }


def select_indices(total: int, n: int, seed: int | None) -> list[int]:
    """Seeded sample of positions, returned sorted so case_001 is the lowest row."""
    if n > total:
        sys.exit(f"asked for {n} cases but the pool only has {total} rows")
    if seed is None or n == total:
        return list(range(n))
    return sorted(random.Random(seed).sample(range(total), n))


def representativeness(population: dict, sample: dict) -> list[str]:
    """Plain-language flags where the sample drifts from the population."""
    warnings = []
    for metric in ("words", "turns"):
        pop, smp = population[metric].get("median"), sample[metric].get("median")
        if pop and abs(smp - pop) / pop > MEDIAN_DRIFT:
            warnings.append(
                f"median {metric} is {smp} vs {pop} in the full split "
                f"({(smp - pop) / pop:+.0%})"
            )
    for name, pop in population["reference_prevalence"].items():
        smp = sample["reference_prevalence"][name]
        if abs(smp - pop) > PREVALENCE_DRIFT:
            warnings.append(
                f"reference {name} is in {smp:.0%} of sampled rows vs {pop:.0%} in the pool"
            )
    for name, pop in population["entity_prevalence"].items():
        smp = sample["entity_prevalence"][name]
        if abs(smp - pop) > PREVALENCE_DRIFT:
            warnings.append(
                f"{name} appears in {smp:.0%} of sampled rows vs {pop:.0%} overall"
            )
    return warnings


def print_profile(base_label: str, base: dict, candidates: dict[str, dict],
                  context: dict[str, dict] | None = None) -> None:
    """Context columns (shown only), the base, then candidates checked against it."""
    context = context or {}
    table = {**context, base_label: base, **candidates}
    names = list(table)
    full = next(iter(table.values()))
    width = 12

    def row(label: str, values: list) -> None:
        print(f"  {label:<24}" + "".join(f"{str(v):>{width}}" for v in values))

    rule = "  " + "-" * (24 + width * len(names))
    print("\nDATASET PROFILE  (deterministic - no model has seen these notes)")
    print("=" * (26 + width * len(names)))
    row("", names)
    row("rows", [table[k]["rows"] for k in names])
    for metric in ("chars", "words"):
        for stat in ("min", "median", "mean", "max"):
            row(f"{metric} {stat}", [table[k][metric].get(stat, "-") for k in names])
    print(rule)
    print("  rows with a REFERENCE label (from the dataset's own rubrics)")
    for name in REFERENCE_CATEGORIES:
        row(f"  {name}", [f"{table[k]['reference_prevalence'][name]:.0%}" for k in names])
    row("  medications/row median", [table[k]["medications_per_row"].get("median", "-") for k in names])
    row("  medications/row max", [table[k]["medications_per_row"].get("max", "-") for k in names])
    print("  rows matching a regex proxy (weaker - English-only patterns)")
    for name in ENTITY_PROXIES:
        row(f"  {name}", [f"{table[k]['entity_prevalence'][name]:.0%}" for k in names])
    print(rule)
    print("  script")
    for group in ("latin", "devanagari", "other-script"):
        row(f"  {group}", [table[k]["language_check"]["script_groups"][group] for k in names])
    lang = full["language_check"]
    print(f"  devanagari split ({names[0]}): ~{lang['devanagari_likely_hindi']} Hindi, "
          f"~{lang['devanagari_likely_marathi']} Marathi (marker words)")
    if lang["code_mixed_row_idx"]:
        print(f"  romanised code-mixed candidates ({names[0]}, row_idx): {lang['code_mixed_row_idx']}")
    integ = full["integrity"]
    print(f"  integrity ({names[0]}): {integ['md5_mismatches']} md5 mismatches, "
          f"{integ['duplicate_texts']} duplicate texts")
    if candidates:
        print(rule)
    for label, summary in candidates.items():
        flags = representativeness(base, summary)
        if not flags:
            print(f"  {label:<12} representative of '{base_label}' on every check")
            continue
        print(f"  {label:<12} DRIFTS from '{base_label}' on {len(flags)} check(s):")
        for flag in flags:
            print(f"               - {flag}")
    print()


# ---------------------------------------------------------------------------
# File IO
# ---------------------------------------------------------------------------

def load_goldset(path: Path) -> GoldSet:
    if not path.is_file():
        sys.exit(f"{_rel(path)} not found. Generate it first:\n  {TEMPLATE_COMMAND}")
    return GoldSet.model_validate_json(path.read_text())


def save_goldset(goldset: GoldSet, path: Path) -> None:
    """Atomic write. Losing 40 minutes of labelling to a half-written file
    is the kind of thing that only has to happen once."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(goldset.model_dump(mode="json"), indent=2) + "\n")
    tmp.replace(path)


# ---------------------------------------------------------------------------
# The gate, plus one labelling-only convenience
# ---------------------------------------------------------------------------

def gate(name: str, field: GoldField, source_text: str):
    """Identical gate to the inference pipeline - see module docstring."""
    return verify_evidence(name, field.to_extracted_field(), source_text)


def snap_to_source(evidence: str, source_text: str) -> str | None:
    """Recover the true verbatim span from a hand-typed approximation.

    Only for labelling: a human retyping a span from a wrapped terminal
    gets the whitespace wrong constantly, and that is not the failure mode
    the gate exists to catch. NEVER call this on model output - snapping a
    model's span to the source would forge exactly the verification the
    gate is supposed to provide.
    """
    tokens = [re.escape(t) for t in evidence.split()]
    if not tokens:
        return None
    match = re.search(r"\s+".join(tokens), source_text, flags=re.IGNORECASE)
    return match.group(0) if match else None


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

def _profile_and_select(args, rows: list[dict]) -> dict:
    """Profile the whole split, filter to the pool, draw from the pool.

    The sample is checked for drift against the pool it was drawn from; the
    full split is printed alongside for context.
    """
    profiles = [_profile_row(r) for r in rows]
    full = summarise(profiles)

    keep = [i for i, p in enumerate(profiles) if args.script == "any" or p["script"] == args.script]
    pool_rows = [rows[i] for i in keep]
    pool_profiles = [profiles[i] for i in keep]
    pool = summarise(pool_profiles)

    n = len(pool_rows) if args.n == "all" else args.n
    whole_pool = n == len(pool_rows)
    seed = None if (args.head or whole_pool) else args.seed

    head_idx = select_indices(len(pool_rows), n, None)
    chosen_idx = select_indices(len(pool_rows), n, seed)

    if whole_pool:
        # Nothing is being sampled, so there is nothing to drift.
        chosen_label, candidates = "pool", {}
    else:
        chosen_label = f"head {n}" if seed is None else f"seed {seed}"
        candidates = {f"head {n}": summarise([pool_profiles[i] for i in head_idx])}
        if seed is not None:
            candidates[chosen_label] = summarise([pool_profiles[i] for i in chosen_idx])

    if args.script == "any":
        print_profile(f"all {len(rows)}", full, candidates)
    else:
        print_profile(f"{args.script} pool", pool, candidates, context={f"all {len(rows)}": full})

    return {
        "seed": seed,
        "whole_pool": whole_pool,
        "full": full,
        "pool": pool,
        "sample": pool if whole_pool else candidates[chosen_label],
        "pool_rows": pool_rows,
        "pool_profiles": pool_profiles,
        "chosen_idx": chosen_idx,
        "filter": {
            "script": args.script,
            "rows_before": len(rows),
            "rows_after": len(pool_rows),
            "excluded_row_idx": sorted(
                set(r["_row_idx"] for r in rows) - set(r["_row_idx"] for r in pool_rows)
            ),
        },
    }


def _language_statement(full: dict, script: str, pool_size: int) -> str:
    lang = full["language_check"]
    groups = lang["script_groups"]
    return (
        f"The dataset card declares language:en, but the per-row script check does not "
        f"bear that out: {groups['latin']}/{full['rows']} rows are Latin script, "
        f"{groups['devanagari']} contain Devanagari (~{lang['devanagari_likely_hindi']} "
        f"Hindi, ~{lang['devanagari_likely_marathi']} Marathi by marker words) and "
        f"{groups['other-script']} another script. "
        + ("No language filter was applied."
           if script == "any" else
           f"Script filter '{script}' applied: {pool_size} rows eligible.")
    )


def _check_row_count(provenance: dict, rows: list[dict]) -> None:
    if provenance.get("dataset") == DATASET and len(rows) != EXPECTED_ROWS:
        print(
            f"WARNING: expected {EXPECTED_ROWS} rows in {DATASET}, got {len(rows)}. "
            "The dataset has changed since this tool was written - the provenance "
            "and README row counts are now wrong.\n"
        )


def cmd_profile(args) -> int:
    rows, provenance = load_rows(args)
    _check_row_count(provenance, rows)
    print(f"{len(rows)} rows from {provenance['dataset']} via {provenance['via']}")
    _profile_and_select(args, rows)
    command = f"{SCRIPT} template --script {args.script} --n {args.n}"
    if args.n != "all" and not args.head:
        command += f" --seed {args.seed}"
    print("nothing written. To generate the template:")
    print(f"  {command}")
    return 0


def cmd_template(args) -> int:
    out = Path(args.out) if args.out else TEMPLATE_PATH
    if out.exists() and not args.force:
        sys.exit(f"{_rel(out)} exists. Pass --force to overwrite (this discards any labels in it).")

    rows, provenance = load_rows(args)
    _check_row_count(provenance, rows)
    print(f"{len(rows)} rows from {provenance['dataset']} via {provenance['via']}")
    picked = _profile_and_select(args, rows)
    seed, sample, chosen_idx = picked["seed"], picked["sample"], picked["chosen_idx"]
    pool_rows, profiles = picked["pool_rows"], picked["pool_profiles"]

    cases, row_map = [], {}
    for position, index in enumerate(chosen_idx, start=1):
        row = pool_rows[index]
        case_id = f"case_{position:03d}"
        text = row[TEXT_FIELD]
        cases.append(GoldCase.blank_case(case_id, text))
        row_map[case_id] = {
            "row_idx": row["_row_idx"],
            "source_id": row.get(ID_FIELD),
            "text_md5": profiles[index]["md5"],
            "chars": profiles[index]["chars"],
            "words": profiles[index]["words"],
            "script": profiles[index]["script"],
        }

    flags = representativeness(picked["pool"], sample)
    provenance["language"] = _language_statement(
        picked["full"], args.script, picked["filter"]["rows_after"])
    exposed = sorted(set(INSPECTED_ROW_IDX) & {pool_rows[i]["_row_idx"] for i in chosen_idx})
    if picked["whole_pool"]:
        method = "entire pool - no sampling"
    elif seed is None:
        method = "head"
    else:
        method = "seeded random sample without replacement"
    provenance.update({
        "filter": picked["filter"],
        "exposure": {
            "row_idx_read_by_assistant_before_selection": exposed,
            "extent": "first ~420 characters of text, plus the full rubric of row 0",
            "when": "2026-09-16, during format inspection, before the pool was chosen",
            "prompt_impact": (
                "none intended: FIELD_RULES examples come from rubric boilerplate "
                "identical across all rows. The 'unit often omitted' note in the "
                "dose rule was informed by row 0."
            ),
        },
        "field_rules": dict(FIELD_RULES),
        "selection": {
            "method": method,
            "seed": seed,
            "n": len(cases),
            "rng": "python random.Random(seed).sample(range(rows), n), sorted",
            "python": platform.python_version(),
            "drawn_from": f"{picked['filter']['rows_after']}-row '{args.script}' pool",
            "row_idx": [pool_rows[i]["_row_idx"] for i in chosen_idx],
        },
        "language_check": picked["full"]["language_check"],
        "profile": {
            "full_split": picked["full"],
            "pool": picked["pool"],
            "sample": sample,
            "representativeness_flags": flags,
            "note": ("reference_prevalence comes from the dataset's own rubric categories; "
                     "entity_prevalence is a weaker English-only regex proxy. Both computed "
                     "before any model ran."),
        },
        "row_map": row_map,
        "fields": list(CRITICAL_FIELDS),
    })
    goldset = GoldSet(provenance=provenance, cases=cases)
    save_goldset(goldset, out)

    _, total = goldset.progress()
    print(f"wrote {_rel(out)}")
    print(f"  {len(cases)} cases x {len(CRITICAL_FIELDS)} fields = {total} labels to make")
    print(f"  selection: {provenance['selection']['method']}"
          + (f", seed {seed}" if seed is not None else ""))
    print(f"  row_idx:   {provenance['selection']['row_idx']}")
    if exposed:
        print(f"  exposure:  rows {exposed} were read during format inspection "
              "- recorded in provenance.exposure")
    if flags:
        print("\n  the sample drifts from the population on: " + "; ".join(flags))
        print("  Report that in the write-up. Do not re-roll the seed until the numbers")
        print("  look nicer - a seed chosen after looking is a selected sample.")
    print_label_instructions(out)
    return 0


def _prompt(text: str) -> str:
    try:
        return input(text).strip()
    except EOFError:
        print()
        raise SystemExit("stdin closed - nothing lost, progress is saved per case.")


def label_one_field(name: str, source_text: str, existing: GoldField) -> GoldField | None:
    """Returns the field, or None to leave it unlabelled and move on."""
    current = ""
    if existing.is_labelled:
        current = f"  [current: {existing.status.value}"
        current += f" / {existing.value!r}]" if existing.value else "]"

    while True:
        choice = _prompt(f"  {name:<11}{current}\n    status (f)ound (n)ot-stated (u)nsure (s)kip (q)uit (?)rule > ").lower()
        if choice == "?":
            print(f"    {name}: {FIELD_RULES[name]}")
            continue
        if choice == "q":
            raise KeyboardInterrupt
        if choice == "s":
            return None
        if choice not in STATUS_KEYS:
            print("    -> answer f, n, u, s, q or ?")
            continue

        status = STATUS_KEYS[choice]
        if status is Status.NOT_STATED:
            return GoldField(value=None, evidence=None, status=status)

        while True:
            evidence = _prompt("    evidence (verbatim span, blank to re-choose status) > ")
            if not evidence:
                break

            candidate = GoldField(value=evidence, evidence=evidence, status=status)
            result = gate(name, candidate, source_text)
            if result.outcome != BLANKED:
                value = _prompt(f"    value [{evidence}] > ") or evidence
                return GoldField(value=value, evidence=evidence, status=status)

            print(f"    REJECTED: {result.reason}")
            snapped = snap_to_source(evidence, source_text)
            if snapped and snapped != evidence:
                print(f"    the note actually reads: {snapped!r}")
                if _prompt("    use that? [Y/n] > ").lower() in ("", "y", "yes"):
                    value = _prompt(f"    value [{snapped}] > ") or snapped
                    return GoldField(value=value, evidence=snapped, status=status)
            else:
                print("    no similar span in the note either - check you are on the right case")


def cmd_label(args) -> int:
    path = Path(args.file) if args.file else TEMPLATE_PATH
    goldset = load_goldset(path)

    queue = [c for c in goldset.cases if args.redo or not c.is_complete]
    if args.case:
        queue = [c for c in goldset.cases if c.case_id == args.case]
        if not queue:
            sys.exit(f"no such case: {args.case}")

    if not queue:
        print("every case is already complete. --redo to revisit, or:")
        print(f"  {SCRIPT} validate --seal")
        return 0

    print(f"{len(queue)} case(s) to label in {_rel(path)}")
    print("ctrl-c or 'q' quits; the file is saved after every case.\n")
    print("FIELD RULES - the same text the extraction model is given")
    print(render_field_rules())
    print("\nType ? at any status prompt to see that field's rule again.")
    print("Record in the case note WHY you picked the medication you did.\n")

    try:
        for position, case in enumerate(queue, start=1):
            print("=" * 78)
            print(f"{case.case_id}   ({position}/{len(queue)})   {len(case.source_text)} chars")
            print("=" * 78)
            # Printed raw and unwrapped on purpose: reflowing the note would
            # show whitespace that is not in source_text, and a span copied
            # from a reflowed display fails the gate for no good reason.
            print(case.source_text)
            print("-" * 78)

            for name in CRITICAL_FIELDS:
                if case.ground_truth[name].is_labelled and not args.redo:
                    continue
                field = label_one_field(name, case.source_text, case.ground_truth[name])
                if field is not None:
                    case.ground_truth[name] = field

            if args.labeller:
                case.labeller = args.labeller
            case.labelled_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            note = _prompt("  case note (optional, e.g. why unsure) > ")
            if note:
                case.notes = note

            save_goldset(goldset, path)
            done, total = goldset.progress()
            print(f"  saved. {done}/{total} labels, {len(goldset.complete_cases)}/{len(goldset.cases)} cases complete\n")
    except KeyboardInterrupt:
        save_goldset(goldset, path)
        done, total = goldset.progress()
        print(f"\n\nstopped. saved {done}/{total} labels to {_rel(path)}")
        return 0

    print("all queued cases labelled. validate before tagging:")
    print(f"  {SCRIPT} validate --seal")
    return 0


def cmd_validate(args) -> int:
    path = Path(args.file) if args.file else TEMPLATE_PATH
    goldset = load_goldset(path)

    problems, near_misses = [], []
    for case in goldset.cases:
        expected = (goldset.provenance.get("row_map") or {}).get(case.case_id, {}).get("text_md5")
        if expected and hashlib.md5(case.source_text.encode()).hexdigest() != expected:
            problems.append(f"{case.case_id}: source_text no longer matches the md5 recorded at pull time")
        for name in case.unlabelled():
            problems.append(f"{case.case_id}.{name}: unlabelled")
        for name, field in case.ground_truth.items():
            if not field.is_labelled:
                continue
            result = gate(name, field, case.source_text)
            if result.outcome == BLANKED:
                problems.append(f"{case.case_id}.{name}: {result.reason}")
            if result.near_miss:
                near_misses.append(f"{case.case_id}.{name}")

    done, total = goldset.progress()
    counts = {s.value: 0 for s in Status}
    for case in goldset.cases:
        for field in case.ground_truth.values():
            if field.is_labelled:
                counts[field.status.value] += 1

    print(f"{_rel(path)}")
    print(f"  cases            {len(goldset.complete_cases)}/{len(goldset.cases)} complete")
    print(f"  labels           {done}/{total}")
    print("  status mix       " + "  ".join(f"{k}={v}" for k, v in counts.items()))
    print(f"  gate failures    {len([p for p in problems if 'unlabelled' not in p])}")
    if near_misses:
        print(f"  near misses      {len(near_misses)} (verbatim, but only just: {', '.join(near_misses[:5])})")

    if problems:
        print(f"\nNOT READY - {len(problems)} problem(s):")
        for problem in problems[:25]:
            print(f"  - {problem}")
        if len(problems) > 25:
            print(f"  ... and {len(problems) - 25} more")
        return 1

    print("\nREADY - every field is labelled and every span is verbatim.")
    if args.seal:
        # Only notes pulled from the real dataset can become gold-v1. A
        # fixture or smoke-test file sealed to the canonical path would be
        # scored as if it were the gold set.
        source = goldset.provenance.get("dataset")
        if source != DATASET:
            sys.exit(
                f"refusing to seal: this file came from {source!r}, not {DATASET}.\n"
                f"Only a template generated from the dataset can become {_rel(SEALED_PATH)}."
            )
        if SEALED_PATH.exists() and not args.force:
            sys.exit(f"{_rel(SEALED_PATH)} already exists. --force to overwrite.")
        goldset.provenance["sealed_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        save_goldset(goldset, SEALED_PATH)
        print(f"sealed to {_rel(SEALED_PATH)}")
    print_seal_instructions(sealed=args.seal, cases=len(goldset.cases))
    return 0


def cmd_stats(args) -> int:
    path = Path(args.file) if args.file else TEMPLATE_PATH
    goldset = load_goldset(path)
    done, total = goldset.progress()
    print(f"{_rel(path)}: {done}/{total} labels, "
          f"{len(goldset.complete_cases)}/{len(goldset.cases)} cases complete")
    for name in CRITICAL_FIELDS:
        counts: dict[str, int] = {}
        for case in goldset.cases:
            field = case.ground_truth[name]
            key = field.status.value if field.is_labelled else "unlabelled"
            counts[key] = counts.get(key, 0) + 1
        print(f"  {name:<11} " + "  ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    return 0


def print_label_instructions(template: Path) -> None:
    file_arg = "" if template.resolve() == TEMPLATE_PATH.resolve() else f" --file {_rel(template)}"
    print("\nnext - label, check, seal:")
    print(f"  {SCRIPT} label{file_arg} --labeller <your-name>")
    print(f"  {SCRIPT} stats{file_arg}")
    print(f"  {SCRIPT} validate{file_arg} --seal")
    print("  (validate prints the git tag gold-v1 commands once every label passes)")


def print_seal_instructions(sealed: bool = False, cases: int | None = None) -> None:
    is_repo = (ROOT / ".git").exists()
    label = f"{cases} hand-labelled Eka Care cases" if cases else "hand-labelled Eka Care cases"
    print("\n" + "=" * 78)
    print("FREEZING gold-v1")
    print("=" * 78)
    if not sealed:
        print("\n1. copy the completed labels to the canonical path:\n")
        print("   cp data/gold_labels/gold_v1_template.json data/gold_labels/gold_v1.json")
        print("\n   (or re-run this command with --seal to have it written for you)")
    else:
        print("\n1. done - labels are at data/gold_labels/gold_v1.json")
    print("\n2. commit and tag, BEFORE any model has seen these notes:\n")
    if not is_repo:
        print("   git init")
        print("   git add -A")
        print(f"   git commit -m \"gold-v1: {label}\"")
    else:
        print("   git add data/gold_labels/gold_v1.json")
        print(f"   git commit -m \"gold-v1: {label}\"")
    print(f"   git tag -a gold-v1 -m \"frozen gold labels, {label}, 4 critical fields\"")
    print("\n3. from here on, treat gold_v1.json as read-only. If a label turns out")
    print("   to be wrong, fix it in a gold-v2 tag rather than editing gold-v1 -")
    print("   a recall number that moved because the labels moved is not a result.")
    print()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    def add_source_args(p):
        p.add_argument("--n", type=lambda v: v if v == "all" else int(v), default=DEFAULT_N,
                       help=f"cases to select, or 'all' for the whole pool (default {DEFAULT_N})")
        p.add_argument("--seed", type=int, default=DEFAULT_SEED,
                       help=f"seeded random sample over the whole split (default {DEFAULT_SEED})")
        p.add_argument("--head", action="store_true",
                       help="take the first N rows instead of a seeded sample")
        p.add_argument("--script", choices=["any", "latin"], default=DEFAULT_SCRIPT,
                       help="restrict the pool before sampling; 'latin' drops Devanagari "
                            f"and other non-Latin-script rows (default {DEFAULT_SCRIPT})")
        p.add_argument("--via", choices=["api", "datasets"], default="api")
        p.add_argument("--from-local-notes", metavar="DIR", help="use local .txt files instead")

    p = sub.add_parser("profile", help="profile every row and preview the selection; writes nothing")
    add_source_args(p)
    p.set_defaults(func=cmd_profile)

    p = sub.add_parser("template", help="profile, select, and write the blank labelling template")
    add_source_args(p)
    p.add_argument("--out", help=f"default {_rel(TEMPLATE_PATH)}")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_template)

    p = sub.add_parser("label", help="interactive labelling with the verbatim gate on")
    p.add_argument("--file")
    p.add_argument("--labeller")
    p.add_argument("--case", help="label a single case_id")
    p.add_argument("--redo", action="store_true", help="revisit already-labelled fields")
    p.set_defaults(func=cmd_label)

    p = sub.add_parser("validate", help="check completeness and verbatim spans")
    p.add_argument("--file")
    p.add_argument("--seal", action="store_true", help="also write data/gold_labels/gold_v1.json")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("stats", help="progress and label distribution")
    p.add_argument("--file")
    p.set_defaults(func=cmd_stats)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
