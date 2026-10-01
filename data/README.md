# `data/` — the corpora, and why each one is sealed

Five answer keys live here, all hash-sealed and mode `0444`. This file explains where each came
from, what sealing actually enforces, and the one finding in this directory that overturned an
earlier interpretation of itself.

Verify every seal in one command:

```bash
cd data/gold_labels && shasum -a 256 -c *.sha256
```

All five must print `OK`. If one does not, the measurement that quoted it is void — not
"approximately right".

---

## 1. What is in here

| Path | What it is |
| :--- | :--- |
| `gold_labels/gold_v1.json` | the original 268 hand labels. **Immutable** |
| `gold_labels/gold_v2.json` | gold-v1 with 18 corrections, plus slice tags |
| `gold_labels/allergy_v1.json` | 20 MTSamples notes carrying a positive allergy |
| `gold_labels/allergy_v1_stripped.json` | the same 20 notes and the **same labels**, ALL-CAPS headers removed |
| `gold_labels/paraphrase_v1.json` | the 67 consults re-dictated around byte-identical gold-v2 values |
| `gold_labels/*.sha256` | the seal for each of the above |
| `gazetteer/rxnorm_ingredients.txt` | 14,689 RxNorm ingredient names, for the non-AI baseline |
| `gazetteer/fetch_rxnorm.py` | how that list was pulled |
| `allergy_set/fetch_mtsamples.py` | how the allergy corpus was selected, including the denial filter |
| `allergy_set/candidates.json` | the allergy candidates found, labelled and unlabelled |

---

## 2. Provenance of the primary corpus

`ekacare/clinical_note_generation_dataset`, revision `662c58a1d03255e26461519be4c9c4e597fdd3fd`,
`test` split, pulled 2026-09-16 through the datasets-server rows API.

**The dataset card declares `language: en` and the per-row check does not bear that out.** Of 156
rows, 67 are Latin script, 88 contain Devanagari (roughly 44 Hindi and 44 Marathi by marker words)
and 1 is another script. A `script: latin` filter was applied and the excluded row indices are
recorded in the file's own `provenance.filter` block, so the exclusion is auditable rather than
asserted.

**67 consults × 4 fields = 268 field decisions.** Those are the denominators every pooled rate in
this project divides by. Of them, gold-v2 marks **148** as `found` and the rest `not_stated` — which
is why answering `not_stated` to everything already agrees with gold on **120 of 268 (44.8%)**.

The notes are synthetic-but-realistic dictation from a public research corpus, de-identified at
source, and they carry literal `<PII>` placeholders where an identifier was removed. Nothing here is
a real patient record. The patient names on the demonstration screens are invented and the pages
say so.

---

## 3. What a seal actually enforces

Sealing is not just a checksum beside a file. Three things happen together, and loading refuses if
any one of them disagrees:

1. **A SHA-256** written to `<name>.sha256`.
2. **The matching `schema_version` stamped *into* the JSON.** A file whose declared version does not
   match the registry entry that names it is refused at load. This is what stops `gold_v2.json`
   being quietly swapped in where gold-v1 was scored.
3. **Mode `0444`.** Write-once. Re-sealing a file in place means removing the seal deliberately,
   which is a visible act rather than an accident.

The registry is `evals/run_ekacare.py:REGISTRY`, and each entry carries the version, its path, its
hash file, **the corpus it is allowed to score**, and the schema version its file must declare.
That last pair is the point: a version is selectable rather than reachable only by loosening a
guard, so adding gold-v3 later means adding a registry entry, not editing a check.

```bash
python evals/run_ekacare.py --gold-version gold-v1      # the default
python evals/run_ekacare.py --gold-version allergy-v1-stripped
python evals/label_gold_v1.py validate --seal           # seal a labelled set
```

| Version | SHA-256 (first 16) | Sealed |
| :--- | :--- | :--- |
| `gold-v1` | `1f594e46bb9bee88` | 2026-09-20 |
| `gold-v2` | `d5e90f5a016a5420` | 2026-09-21, by `rohit` |
| `allergy-v1` | `10725be95aac93ec` | 2026-09-21 |
| `allergy-v1-stripped` | `1d02348ecf948304` | 2026-09-21 |
| `paraphrase-v1` | `fa3fa8bf46fe8b8f` | 2026-09-29 |

### `gold_v1.json` is never edited

Corrections go to a new version. `gold-v2` carries **18**, and they are of two kinds, kept apart on
purpose:

- **16 are `rule`** — the label contradicted the project's own field rules, so the correction is
  mechanically derivable and needs no judgement. Example, `case_001`: the label's value named
  `chymoral forte` while its evidence span quoted `paracetamol`. Both phrases are in the note, and
  `extract.verify_value` rejects that pair, so the label was self-inconsistent.
- **2 are `review`** — these repair an *intent* rather than apply a rule, so they carry a name:

```bash
python evals/label_gold_v2.py validate --seal --labeller <name> \
    --confirm case_010.medication --confirm case_010.frequency
```

**A corrected answer key raises the score without the extractor changing**, so the two effects are
always reported apart. Measured, of the 8.5-point move from 0.645 to 0.730: **7.8 points are the
labels (p = 0.008) and 0.7 is sampling noise that does not separate (p = 1.000)**.

The majority-class baseline **moves with the answer key** for the same reason — 113/268 against
gold-v1, 120/268 against gold-v2 — because several corrections turned a disputed dose into
`not_stated`. Quote the one belonging to the gold an arm was actually scored against.

---

## 4. The allergy corpus, and the finding that corrected itself

`gold-v2` contains **zero** positive allergies in 67 consults. A field with no positive instances
cannot be evaluated, so 20 MTSamples notes with a documented allergy were selected and labelled
(`allergy-v1`), and then a second copy was built with **every ALL-CAPS section header removed** and
**the labels left byte-identical** (`allergy-v1-stripped`).

Paired, the two give the leakage report:

> **Allergy recall falls 0.900 → 0.650 when the headers are stripped.**

The obvious reading is that the model leans on formatting rather than clinical content. Auditing all
thirty gates afterwards showed why none of them fired: **every gate in this system is a precision
control, and none is a recall control.** They catch an invented value; they cannot catch a missed
one.

### The correction

That reading was then tested, and it is wrong in an important way.

- **201 ALL-CAPS headers survive in the intact corpus against 1 in the stripped one.** The strip is
  near-total, not incidental.
- **`evals/label_allergy_v1.py` uses one `LABELS` dict for both variants and never reads the
  stripped text.** The stripped set's answers are therefore *carried over* from the intact notes —
  they are not derivable from the notes they are being scored against.

So the header was the **clinical signal** for the allergy field, not a formatting crutch the model
should have been able to do without. `0.650` is a **ceiling set by the input**, not a defect waiting
for a gate. A recall control was specified and measured against exactly this, and refused:
sensitivity **0 of 4**, because its trigger word `\ballerg\w*` *is* the header that stripping
removes — it matches 20 of 20 intact notes and none of the four misses. Circular by construction.
`guardrails.md` §6.10 keeps the numbers.

**No control can recover information the text no longer contains.** That sentence is the finding.

The remaining allergy candidates in `allergy_set/candidates.json` are unlabelled; labelling them
would widen the corpus but changes nothing about the above.

---

## 5. The near-distribution corpus

`paraphrase-v1` re-dictates the same 67 consults through six named transforms — the dosage-form cue
dropped, meal timing rephrased, plan verbs rephrased, fillers inserted, punctuation drift,
lowercase drift — applied **strictly outside every gold value span**. The value labels stay
byte-identical to gold-v2, which is what makes the paired McNemar against the gold-v2 live run exact
rather than approximate.

Two build-time refusals keep the arm honest: `refuse_if_value_lost` blocks a build that drops a
label, and `refuse_if_corpus_barely_moved` blocks one where under 80% of cases changed — a corpus
that barely moved is gold-v2 wearing a different name.

**Result: p = 0.6072. That is a null, not an equivalence.** 15 discordant pairs cannot resolve an
effect under roughly 8 points. Reported beside §4, it localises the dependency to the *header*
rather than to formatting in general.

---

## 6. The gazetteer

14,689 RxNorm ingredient names, pulled by `gazetteer/fetch_rxnorm.py`, used only by the non-AI
baseline in `evals/baseline.py`. It is **not** used by `src/extract.py` — the pipeline has no drug
lookup, by design, because a gazetteer hit is not evidence that a word appears in *this* note.

Worth knowing what it bought: scored against gold-v2, the gazetteer-assisted baseline and the
pattern-only baseline reach **identical recall (0.4324)**, and the gazetteer is **worse on
precision** (0.5289 against 0.5714). Fourteen thousand drug names added nothing and cost accuracy.
