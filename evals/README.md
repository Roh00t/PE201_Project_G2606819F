# `evals/` — how every number in this repository was produced

Nothing in here extracts. `src/extract.py` is the pipeline; these are the instruments pointed at it.
Each file below is one deliberate separation, and §2 is the one that matters most.

Everything except §5 runs **offline, with no API key, at zero cost**, because it reads saved
artefacts rather than calling anything.

---

## 1. The scripts

| File | What it does | Spends |
| :--- | :--- | :--- |
| `run_ekacare.py` | the batch runner and the **sealed-gold registry**. Writes `gated.jsonl`, `ungated.jsonl`, `manifest.json` | yes |
| `scoring.py` | the **pre-registered scorer**. Recall, precision, abstention, silent failure | no |
| `score_arm.py` | scores any saved run, and prints the majority-class baseline beside it | no |
| `metrics.py` | compares two runs; exact McNemar, Wilson intervals, κ and AC₁ | no |
| `diagnose.py` | attributes every failure to the layer that owns the fix | no |
| `baseline.py` | the non-AI comparator: regex, and regex + a 14,689-name gazetteer | no |
| `spend_guard.py` | **refuses** a call that would breach a cap; audits every call made | no |
| `lowcode_arm.py` | the console/low-code control arm, built from the same cached calls | no |
| `refused_controls.py` | measures the two controls that were specified and rejected | no |
| `paraphrase.py` | builds and seals the near-distribution corpus | no |
| `live_redteam.py` | the live adversarial battery: refusal, bypass, injection | yes |
| `judge_calibration.py` | the LLM-as-judge suite, and the gate it failed | yes |
| `judge_rubric.py` | the judge's prompt and schema, kept apart from its runner | no |
| `label_gold_v1.py`, `label_gold_v2.py`, `label_allergy_v1.py` | labelling and sealing | no |
| `probes/probe_v4_schema.py` | the v4 medication-array spike | tiny |

---

## 2. Two separations that are not stylistic

### Enforcement and measurement are different modules, on purpose

`spend_guard.py` **can refuse a call**. `metrics.py` **only reads finished artefacts** and is
structurally incapable of making one. A module that can refuse a call must not also be the module
that scores it, because then a bug in the scoring can silence the refusal and nothing notices.

`spend_guard.py` is also never imported by `src/extract.py`, so the pipeline remains the single
standalone file CLAUDE.md §1.1 requires.

### Exactly one writer per call

`extract.call_model` records what it spends in the project ledger. A script that calls the endpoint
directly must pass `CostGuard(records_to_ledger=True)` or its spend never reaches the $8 ceiling
meant to bound it. There are two bounds and they are different things: the **$8 project ceiling**
belongs to `extract.SpendLedger`, and **`--run-cap-usd`** (default $1.00) is this run's, checked
against worst case *before every call*, because a loop inside one run would stay under the project
ceiling while consuming it.

---

## 3. The scoring math

The unit is **the field**, not the case: 67 consults × 4 fields = **268 field decisions**.

Read from the gate **code**, never from whether a value happens to be null:

```
proposed   code == "VERIFIED" or code.startswith("REVIEW")      — shown with a value
wiped      code.startswith("ABSTAIN") and not forced            — the gate declined
correct    proposed, gold status is `found`, and the values match under value_matches()
```

| Metric | Definition | gold-v2 |
| :--- | :--- | ---: |
| **recall** | `correct / gold found` | **0.7297** |
| **precision** | `correct / proposed` | **0.7500** |
| abstention rate | `wiped / (wiped + proposed)` | 0.0336 |
| abstention precision | wiped fields whose *ungated* value was wrong / wiped | **1.000** |
| gate false-positive rate | correct values destroyed by a gate / had something to lose | **0.000** |
| **silent-failure rate** | `VERIFIED fields that are wrong / VERIFIED` | **0.2517** |

Three deliberate exclusions, each there to stop one number laundering another:

- **An error envelope is never scored.** It counts in `errored_cases`, so an upstream failure cannot
  pose as an abstention.
- **Encoding-anomaly wipes count in `forced`, not `wiped`.** They never reached a model, so they are
  misses for recall but must not flatter the abstention rate, which is meant to measure the gates'
  judgement on real model output.
- **Nothing is scored from null-ness.** This is why the low-code arm needed care: `ungated.jsonl`
  retains the `gate` array, so copying it verbatim scores *identically* to the coded pipeline — the
  verdicts are reported and simply not applied. The first version of that arm compared the system
  with itself.

### A rate always carries a comparator

`score_arm.py` prints the majority-class baseline beside every arm, because a recall figure with
nothing beside it says nothing: **answering `not_stated` to everything already agrees with gold on
120 of 268 field decisions (44.8%)**.

That comparator **moves with the answer key** — 113/268 against gold-v1, 120/268 against gold-v2.
`score_arm.py` defaults to gold-v1, which scores the gold-v2 run at 0.6516 instead of 0.7297, so
pass `--gold` explicitly:

```bash
python evals/score_arm.py evals/results/gold-v2-live --gold data/gold_labels/gold_v2.json
```

`diagnose.py` had the same defect and no longer does: it reads the version the run's own
`manifest.json` records, because the documented bare invocation was attributing a gold-v2 run
against v1 labels and reporting 43 silent failures instead of 36.

---

## 4. The statistics, and what McNemar does and does not say

Two runs over the same gold set are **paired**. Only the fields that *changed* carry information, so
the test is an **exact McNemar** over the discordant pairs. Fisher's exact is also printed and
labelled *not applicable*, so the paired and unpaired answers can be seen to differ.

```bash
python evals/metrics.py evals/results/<run_a> evals/results/<run_b> \
    --gold data/gold_labels/gold_v2.json
```

**Report the p-value with the delta, every time.** This rule exists because it was broken once: a
prompt correction was reported as a 4.5-point recall improvement, and paired it is 12 flips one way
against 5 the other, **p = 0.14** — not separated at α 0.05.

### Against the non-AI baseline, the gap does separate

This is the one comparison in the project where it does, and it is worth stating precisely. Both
arms ran the same 67 consults against gold-v2:

| Scope | Regex | MediExtract | Δ | Flips right/wrong | p | |
| :--- | ---: | ---: | ---: | :--- | ---: | :--- |
| **pooled** | 43.2% | **73.0%** | +29.7pp | 52 / 8 | **5.2e-09** | separated |
| medication | 31.6% | 70.2% | +38.6pp | 24 / 2 | 1.0e-05 | separated |
| dose | 23.7% | 65.8% | +42.1pp | 18 / 2 | 4.0e-04 | separated |
| frequency | 69.8% | 81.1% | +11.3pp | 10 / 4 | 0.180 | **not separated** |

So the supportable claim is **not** "the model beats rules". It is: *the model beats rules on drug
name and dose, decisively; on frequency — where a regex was already strong — this evaluation cannot
tell.* A change can be real in one field and noise overall, and it is reported at the level the
evidence supports.

### Elsewhere, the tests mostly return nulls — and that is a result

| Comparison | Result |
| :--- | :--- |
| Prompt correction, 2026-09-20 | p = 0.14, **not separated** |
| Near-distribution paraphrase arm | p = 0.6072, **a null** |
| Low-code arm, recall | 0 flips, p = 1.000 — the gates cost no correct answers |
| Label correction alone (gold-v1 → v2) | p = 0.008, **separated**; the sampling half p = 1.000 |

**A large p-value never means two systems are equally good.** It means the evaluation is too small
to tell, which is a fact about the evaluation. The power floor here is 2/2⁵ = 0.0625, and three
identical runs in early work scored 37, 41 and 49 of 60 with the model held fixed — a spread wider
than most gaps between different models.

Two claims, two confidence levels, never blurred:

- **A named defect disappearing is a count.** "Same drug, strength appended" went from 8 occurrences
  to 0. Solid.
- **A recall delta is a rate.** It needs a test before it is called an improvement.

### κ and AC₁ are both reported, with their chance terms

On the judge-calibration data they differ by 0.59 — **0.1967 against 0.7827** — because both raters
accept about 84% of decisions, and Cohen's chance term is 0.7863 while Gwet's is 0.2103. Quoting
whichever is more flattering would be the whole failure mode, so both appear with the terms that
produced them.

---

## 5. Where the failures actually are

```bash
python evals/diagnose.py evals/results/gold-v2-live        # reads the run's own gold
```

Attributed by the layer that owns the fix, over the 36 silent failures:

| Cause | n | Owner |
| :--- | ---: | :--- |
| `MED_DIFFERENT_DRUG` — a different drug from the same note | **17** | schema (one slot, a median of three drugs) |
| `FREQ_DIFFERENT_REGIMEN` — the regimen of the drug it picked | 8 | schema (cascade) |
| `DOSE_OTHER_DRUG` — the strength of the drug it picked | 6 | schema (cascade) |
| `DOSE_GOLD_SILENT` | 3 | labels or model (disputed) |
| `FREQ_PHRASE_LENGTH` | 2 | labels |

**31 of 36 (86%) are one defect: drug selection in a multi-drug note** — 17 directly and 14 as the
cascade of reporting the dose and schedule of the drug it named. **Zero are owned by the model layer
and zero by the gate layer**, and all 36 returned values are present in the note, so there is no
hallucination in the set.

`diagnose.py` sizes the prize exactly: **recall 0.990 (101/102) where the drug choice agrees, 0.196
(9/46) where it does not.** That is a conditioned figure and never the headline — 0.7297 is the
number quoted unqualified. The fix is a medication *list*, and it needs a gold-v3 with multi-drug
containment labels.

Two controls aimed at this were specified, measured and **refused**; `refused_controls.py` keeps the
numbers so the next person to propose one meets the measurement first.

---

## 6. Cost control

```bash
python evals/run_ekacare.py --experiment my-change --run-cap-usd 0.25
```

A breach exits **5**, a code shared with nothing else. `spend_guard.py` audits every call — tokens
including cached, list-price estimate against the provider's charge, latency, attempts, endpoint,
provider, model requested versus served, response id — to
`data/cache/metrics/<run_id>.jsonl`, with an aggregate `metrics.json` beside the run.

The whole 67-case run costs **$0.041**, or **$0.000617 per note**. Quote cost per *correct field*
rather than cost per call: a system that fails cheaply looks cheap only on the second measure.
