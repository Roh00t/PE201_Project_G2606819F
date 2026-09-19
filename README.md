# MediExtract

Four critical fields out of a dictated clinical note, with a verbatim
evidence span for each, so a polyclinic physician can verify the whole
extraction in under three seconds.

Status: single-note pipeline and batch evaluation loop built and tested offline.
Inference goes through OpenRouter (`google/gemini-2.5-flash`) via the OpenAI SDK.

## Run it

```bash
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
# put OPENROUTER_API_KEY=<your key> in .env at the repo root (never commit it)

./.venv/bin/python src/extract.py --note gold/case_001.txt
```

Every request carries: a 15 s timeout and one retry (set on the SDK, whose
defaults are 2 retries and a 600 s read timeout); a 1,024-token output cap;
reasoning off; a strict JSON schema; and OpenRouter routing that only uses
providers honouring every parameter (`require_parameters`) and not collecting
prompts (`data_collection: deny`). For public or synthetic notes only,
`--provider-data-collection allow` widens routing.

No key, no credit, still exercises the safety gates (the canned response
contains deliberate fabrications):

```bash
./.venv/bin/python src/extract.py --note gold/case_001.txt --mock
```

`stdout` is only ever one JSON document: the extraction (`"status": "ok"`)
or an error envelope (`"status": "error"`, with a `code`). While the
pipeline runs, stdout is redirected to stderr, so a stray `print` from any
library cannot corrupt the JSON a downstream parser reads. A field wiped by a
gate is `null` in the extraction (a hard wipe); why it was wiped is a
`code` in the separate `gate` list. stdout never carries human-readable text:
the review screen maps a code to its label (for example
`BLANK (Abstained: Ungrounded)`) with `extract.DISPLAY`, and gate reasons,
error messages and evaluation tables go to stderr. An error envelope is
`{"status": "error", "code": ..., "data": null, ...}`, with the message on stderr.

A note carrying hidden or look-alike characters (bidirectional controls,
zero-width or other invisible characters, homoglyphs such as a Cyrillic `е`
inside `metformin`) is a **forced safe abstention**: every field is wiped with
`ABSTAIN_ENCODING_ANOMALY`, the model is not called, and the dictation is
never "cleaned" to rescue an extraction. Measured: 0 of the 156 Eka notes
trigger it; µg, Ménière, β-blocker, mg/m² and similar notation do not.

Flags that matter later:

| flag | why |
| --- | --- |
| `--no-gate` | report gate verdicts but wipe nothing — the abstention test |
| `--budget-usd N` | total spend ceiling across runs (default $8.00, or `MEDIEXTRACT_BUDGET_USD`); checked before every call; the ledger records OpenRouter's reported cost |
| `--provider-data-collection allow` | public or synthetic notes only; default is `deny` |
| `--json-only` | suppress the stderr table |

### Exit codes

Every outcome has its own code, so the Week 3 harness can count abstentions
off the exit code without mistaking a crash for one.

| code | meaning | stdout |
| --- | --- | --- |
| 0 | every field verified, not stated, or routed to review | extraction |
| 1 | at least one field wiped by a gate | extraction |
| 2 | input rejected: missing, unreadable, empty, over 20,000 characters, binary, not UTF-8 | envelope `ERR_INPUT_*` |
| 3 | upstream failure: timeout, unreachable, rate limited, auth, model unavailable (404), no eligible provider | envelope `ERR_UPSTREAM_*`, `ERR_RATE_LIMITED`, `ERR_MODEL_UNAVAILABLE`, `ERR_NO_ELIGIBLE_PROVIDER` |
| 4 | model output failed the schema on both attempts, or was truncated at the token cap | envelope `ERR_SCHEMA_INVALID`, `ERR_OUTPUT_TRUNCATED` |
| 5 | spend ceiling reached (the call was not made), or OpenRouter credit exhausted (402) | envelope `ERR_BUDGET_EXCEEDED`, `ERR_UPSTREAM_CREDITS_EXHAUSTED` |
| 6 | configuration or internal error | envelope `ERR_CONFIG_*`, `ERR_INTERNAL` |

argparse usage errors also exit 2, but print no envelope.

### Batch evaluation (`evals/run_ekacare.py`)

```bash
./.venv/bin/python evals/run_ekacare.py        # live: needs the sealed gold-v1 and OPENROUTER_API_KEY
./.venv/bin/python evals/run_ekacare.py --mock --gold data/gold_labels/gold_v1_template.json --limit 3
```

One model call per case (cached under `data/cache/runs/<run_id>/`, so
`--run-id <same id>` resumes without paying twice); the same output is gated
and ungated, so the abstention test costs no extra call; scores come from
`evals/scoring.py`. Results land in `evals/results/<run_id>/` (manifest,
gated and ungated JSONL, `scores.json`, `rows.csv` with formula-safe cells);
stdout is one JSON summary.

A live run refuses, before any call: any gold file other than the sealed
`data/gold_labels/gold_v1.json`; a file whose SHA-256 no longer matches
`gold_v1.sha256`; unlabelled fields; note text that drifted from its md5; a
prompt changed since the seal (unless `--experiment NAME`, reported
separately); and a worst case the remaining budget cannot cover. A circuit
breaker halts the run after 3 consecutive system failures, above a 5% error
rate after 20 calls, or at once on a spend refusal (batch exit codes: 0 done,
2 refused, 3 halted, 5 spend, 6 internal). Tag a case `#negation`,
`#attribution` or `#temporality` in its labelling note to put it in a slice.

### Tests

```bash
./.venv/bin/python -m unittest discover -s tests
```

Offline, no API key, no spend: the live-call paths run against a fake
client, or fail by design before any client exists. The suite also fails if
an `assert` statement appears under `src/` — `python -O` strips them, so a
gate written as one would silently stop running.

## Build the gold set (do this before any model sees the notes)

### The dataset

`ekacare/clinical_note_generation_dataset` at revision `662c58a`, measured
2026-09-16:

| | |
| --- | --- |
| rows | 156, one config (`default`), one split (`test`) |
| declared language | `language:en` on the dataset card |
| **measured script** | **67 Latin, 88 Devanagari (~44 Hindi, ~44 Marathi), 1 Telugu** |
| romanised code-mixed | 5 rows flagged (Hinglish written in Latin script) |
| format | unlabelled run-on speech or dictation; no speaker tags |
| reference labels | inside each row's `rubrics` text as `Category ID:` lines |
| access | gated; licence accepted per Hugging Face account |

**The card's `language:en` does not hold row by row.** Most rows are Hindi or
Marathi in Devanagari script, often with English medical terms transliterated
("हेवी हेडेड फील"). An English-only analysis needs a filter:
`--script latin` restricts the pool to the 67 Latin-script rows before
sampling. `provenance.language` and `provenance.filter` record what was
measured and which filter was applied.

What the reference labels say about the four fields:

| rows with a reference label | all 156 | Latin 67 |
| --- | --- | --- |
| medication | 56% | 81% |
| dose (the rubric means quantity, e.g. "1 tablet") | 26% | 40% |
| frequency | 53% | 79% |
| allergy (drug or food) | 4% | **3% - 2 rows** |
| medications per row with any (median / max) | 3 / 12 | 3 / 12 |

Two consequences for the evaluation:

- **Allergy recall can't be measured on this dataset.** Only 2 Latin-script
  rows carry an allergy label at all. Measure allergy on MTSamples instead.
- **Notes carry several medications**, but the extraction schema holds one. The
  labelling guide and the extraction prompt must use the same rule for which
  one counts.

### Access

You need both of these:

1. accept the terms on the dataset page while signed in
2. a token that can read gated repos you do not own. Either a classic **Read**
   token, or a fine-grained token with *Read access to contents of all public
   gated repos you can access* ticked. A fine-grained token scoped only to your
   own namespace gets a 404, and the tool says so.

Put the token in `.env` as `HF_TOKEN=<token>`; the tool reads it directly.
Don't `export HF_TOKEN=hf_...` with the literal placeholder: a shell variable
overrides `.env` for the rest of that session. The tool refuses placeholders
and tells you to `unset HF_TOKEN`.

### Steps

```bash
./.venv/bin/python evals/label_gold_v1.py profile              # writes nothing
./.venv/bin/python evals/label_gold_v1.py template             # = --script latin --n all
./.venv/bin/python evals/label_gold_v1.py label --labeller <name>
./.venv/bin/python evals/label_gold_v1.py stats
./.venv/bin/python evals/label_gold_v1.py validate --seal
git add data/gold_labels/gold_v1.json data/gold_labels/gold_v1.sha256
git tag -a gold-v1 -m "frozen gold labels, 67 hand-labelled Eka Care cases, 4 critical fields"
```

`profile` takes the same arguments as `template` and writes nothing. Use it to
look before committing to a draw.

**The gold set is all 67 Latin-script rows** (decided 2026-09-16, after
profiling), so nothing is sampled and nothing can drift: 268 labels over notes
with a median length of 58 words. Three of those rows (0, 2, 3) were partly
read during format inspection before the decision; `provenance.exposure`
records that.

**Field definitions** live once, in `FIELD_RULES` in `src/extract.py`. The
Gemini system prompt and the labelling session both render that same text, and
the template stores a copy in `provenance.field_rules`. Decisions baked in:
`medication` is the single most clinically significant one; `dose` is the
*strength* as dictated (a quantity like "1 tablet" is not a dose); `dose` and
`frequency` always describe that same medication. Type `?` at a status prompt
to see a rule again, and use the case note to say why you picked the
medication you did.

**Sampling, if you ever want a subset.** `template` pulls all 156 rows and profiles them. It then applies
`--script` and draws a seeded random sample of N from what remains, without
replacement (`--seed 42` by default; `--head` takes the first N instead). The printed table puts the full split, the
head 30 and the seeded 30 side by side: length, word count, speaker turns, and
the share of rows carrying each reference label, and the script mix. Any draw
that drifts from its pool past a threshold is named, line by line.

The seed, the selected `row_idx`, the Python version, the population and
sample profiles, and any drift flags all go into `provenance`. **Choose the
seed before looking, and don't re-roll it to make the numbers look nicer.** A
seed picked after looking is a selected sample. If the draw drifts, report it.

The reference figures come from the dataset's own rubric categories. The
regex-proxy rows are a weaker, English-only fallback: a dictated
"paracetamol 500" has no unit, so they undercount. Neither is ground truth -
labelling is.

**Labelling.** `label` refuses any evidence span that is not a verbatim
substring of the note, using the *same* `verify_evidence` the pipeline runs at
inference time. When the rejection is just retyped whitespace or casing, it
offers the real span from the source. When there is no similar span at all, it
says so, because that usually means you are on the wrong case.

**Sealing.** `validate` blocks the tag on unlabelled fields, on non-verbatim
spans, and when `source_text` has drifted from the md5 recorded at pull time.
`--seal` refuses any file that didn't come from the real dataset, so a
fixture can never land at `data/gold_labels/gold_v1.json`. Sealing is
**write-once**: it refuses if `gold_v1.json` exists (there is no overwrite
flag), records the prompt fingerprint and model, writes `gold_v1.sha256`, and
makes both files read-only. A wrong label is fixed in a new file sealed as
gold-v2, never by editing gold-v1.

The rows API serves only the default branch, so on that path the pinned
revision is recorded but not enforced (`revision_enforced: false`). The
per-case md5 is what pins the text. `--via datasets` enforces the revision
but needs `pip install datasets`.

## What is here

```
src/extract.py     the whole pipeline, one file (CLAUDE.md 1.1), in sections:
                   1 configuration, 2 schema (wire + gold), 3 prompt, 4 deterministic
                   gates, 5 spend ledger, 6-7 pipeline and CLI
tests/             offline unittest suite: gates, budget, CLI contract, scoring, batch loop, seal
evals/             label_gold_v1.py (profile / template / label / validate / stats),
                   run_ekacare.py (batch loop), scoring.py (metrics, Wilson CIs)
gold/              synthetic dictations for smoke tests. Real labels: data/gold_labels/.
```

## Design notes worth defending in the report

**Evidence is emitted before value.** With thinking disabled the token order
is the reasoning order, so the model must copy a span out of the dictation
before it may commit to a value. Asking for the value first invites it to
decide the answer and then hunt for justification.

**Nothing in the field schema is optional.** A Pydantic default drops the key
from `required` in the generated Gemini schema, which would let the model
silently omit `evidence` — the one field the design rests on.

**Near misses are logged, not forgiven.** A span that matches only after
whitespace/case normalisation is still blanked, but flagged `near_miss`. That
separates paraphrasing from fabrication, so Week 2 can decide whether to relax
the gate to normalised matching from counts rather than from vibes.

## Two types for a field, on purpose

`ExtractedField` is a wire schema: nothing optional, absence is the empty
string, because that is what Gemini's schema subset handles cleanly and because
a missing key there fails silently.

`GoldField` is a human artefact, where `status: null` means *no human has
looked at this yet* - which has to stay distinguishable from *a human looked and
the note does not state it*. Collapse those two and generating the template
hands you 120 free `not_stated` labels, every one of which scores as a correct
abstention. `GoldField.to_extracted_field()` is the only bridge, so scoring
compares like with like.

## Gates beyond the verbatim check

`evidence in source_text` proves the span is real. It does not prove the
`value` derived from it is right, so a second gate (`verify_value` in
`src/extract.py`) checks the value against the verified span:

- **dose** — every number and unit must match the span. `50 mg` against a
  span of `15 mg` is blanked, as are `mcg` against `mg` and a unit the
  physician never dictated.
- **frequency** — the value must be a phrase of the span, or a recognised
  equivalent of one (`bd` → `twice daily`).
- **medication, allergy** — every word of the value must appear in the span.

Known limit, pinned by a test: a value that is a verbatim sub-phrase of its
span still passes (`daily` out of `twice daily`). The physician sees the quote
beside it. The full control inventory is in `guardrails.md`.

## Not yet verified

No live call has been made from `src/`: there is no OpenRouter key in this
environment. Verified offline instead: the request (SDK timeout, retries,
token cap, strict schema, routing, data policy, no tools), every error
mapping, retries, truncation, fenced JSON, the ledger, the stdout guard, the
batch loop's refusals, resume, breaker and scoring (98 tests, also under
`python -O`). `google/gemini-2.5-flash` is listed by OpenRouter's public models
API (checked 2026-09-18, $0.30 / $2.50 per million tokens) - the 404 the Colab
prototype hit came from Google's direct API. The first live smoke test should
be one synthetic note (`src/extract.py --note gold/case_001.txt`); if routing
returns `ERR_NO_ELIGIBLE_PROVIDER`, the data policy found no provider, which
is a decision to take deliberately, not a flag to flip for patient data.
