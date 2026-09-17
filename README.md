# MediExtract

Four critical fields out of a dictated clinical note, with a verbatim
evidence span for each, so a polyclinic physician can verify the whole
extraction in under three seconds.

Week 1 status: the smallest first version runs. Single note in, gated JSON out.

## Run it

```bash
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
export GEMINI_API_KEY=...                       # or put it in .env at the repo root

./.venv/bin/python src/extract.py --note gold/case_001.txt
```

No key, no credit, still exercises the safety gate (the canned response
contains a deliberate fabrication):

```bash
./.venv/bin/python src/extract.py --note gold/case_001.txt --mock
```

`stdout` is only ever the JSON payload; the gate table goes to `stderr`.
Exit code is 1 when the gate blanked at least one field, so the Week 3
harness can count abstentions without parsing anything.

Flags that matter later:

| flag | why |
| --- | --- |
| `--no-gate` | report gate verdicts but blank nothing — the abstention test |
| `--thinking-budget N` | 0 by default; thinking spends latency we do not have |
| `--json-only` | suppress the stderr table |

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
git tag -a gold-v1 -m "frozen gold labels, 67 hand-labelled Eka Care cases, 4 critical fields"
```

`profile` takes the same arguments as `template` and writes nothing. Use it to
look before committing to a draw.

**The gold set is all 67 Latin-script rows** (decided 2026-09-16, after
profiling), so nothing is sampled and nothing can drift: 268 labels over notes
with a median length of 58 words. Three of those rows (0, 2, 3) were partly
read during format inspection before the decision; `provenance.exposure`
records that.

**Field definitions** live once, in `FIELD_RULES` in `src/schema.py`. The
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
fixture can never land at `data/gold_labels/gold_v1.json`.

The rows API serves only the default branch, so on that path the pinned
revision is recorded but not enforced (`revision_enforced: false`). The
per-case md5 is what pins the text. `--via datasets` enforces the revision
but needs `pip install datasets`.

## What is here

```
src/schema.py      wire schema (Gemini) + gold annotation schema, and the one bridge between them
src/guardrails.py  deterministic gates, no model calls
src/extract.py     one constrained call + the gate + latency/cost accounting
evals/             label_gold_v1.py: profile / template / label / validate / stats
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

## Known gap (next gate, Week 2)

`evidence in source_text` proves the span is real. It does **not** prove the
`value` derived from it is right. A note saying `500 mg` with a verbatim span
of `500 mg` and a value of `50 mg` passes today. That is precisely what the
numeric/unit checker is for.

## Not yet verified

The live Gemini path has not been run — no API key was available in the
environment where this was built. The Pydantic-to-Gemini schema conversion
was verified offline (required fields, enum, property ordering all correct),
and the gate is covered end to end via `--mock`, but the first real
`--note` run is still unproven. Expect to spend a few cents settling it.
