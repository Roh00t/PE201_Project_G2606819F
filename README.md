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

`ekacare/clinical_note_generation_dataset` at revision `662c58a`:

| | |
| --- | --- |
| rows | **156** |
| configs | 1 (`default`) |
| splits | 1 (`test`) |
| language | **English, 100% of rows** as declared by the dataset card (`language:en`) |
| language filtering applied | **none - none was required** |
| access | gated; licence accepted per Hugging Face account |

There is no separate "English subset": the whole split is the English set,
and every row in it is eligible for sampling. The 100% figure is the card's
declaration. `template` also runs a per-row script check (Devanagari
codepoints, non-ASCII letters, romanised-Hindi marker tokens) and writes the
result to `provenance.language_check`. That check is the measured
confirmation. If it flags code-mixed rows, report them rather than quietly
dropping them.

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
./.venv/bin/python evals/label_gold_v1.py profile --n 30 --seed 42
./.venv/bin/python evals/label_gold_v1.py template --n 30 --seed 42
./.venv/bin/python evals/label_gold_v1.py label --labeller <name>
./.venv/bin/python evals/label_gold_v1.py stats
./.venv/bin/python evals/label_gold_v1.py validate --seal
git tag -a gold-v1 -m "frozen gold labels, 30 cases, 4 critical fields"
```

`profile` takes the same arguments as `template` and writes nothing. Use it to
look before committing to a draw.

**Sampling.** `template` pulls all 156 rows, profiles them, and draws a seeded
random sample of 30 without replacement (`--seed 42` by default; `--head`
takes the first 30 instead). The printed table puts the full split, the
head 30 and the seeded 30 side by side: length, word count, speaker turns, and
the share of rows with dose, frequency, dosage-form, drug-name and allergy
mentions. Any draw that drifts past a threshold is named, line by line.

The seed, the selected `row_idx`, the Python version, the population and
sample profiles, and any drift flags all go into `provenance`. **Choose the
seed before looking, and don't re-roll it to make the numbers look nicer.** A
seed picked after looking is a selected sample. If the draw drifts, report it.

The entity figures are regex proxies, computed deterministically before any
model has run. They show whether the sample looks like the population. They
are not ground truth - labelling is.

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
