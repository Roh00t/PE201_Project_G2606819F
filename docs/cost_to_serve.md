# MediExtract — cost to serve, ROI, and build vs buy

Every figure here is measured from run `20260920T065552Z-gemini-2.5-flash` (67 live calls
against the sealed gold set), from the later `20260920T133717Z` run on the corrected prompt, or
from this repository's own history. Assumptions are labelled as
assumptions and carry the study that would settle them. The framework is the Class 5 / C2
cost-to-serve model, and MediExtract is that framework's **v4 human-in-the-loop archetype**:
`C_agent + (1 − S) · C_human`, adapted below because this design never skips the human.

---

## 1. What one note costs, measured

Single-call token cost, the C2 formula, at Gemini 2.5 Flash list prices
(`P_in` = $0.30 / M tokens, `P_out` = $2.50 / M tokens):

```
Cost_call = N_in × P_in/1e6  +  N_out × P_out/1e6
```

| Quantity | Measured over 67 live calls |
| :--- | :--- |
| input tokens | mean **777**, median 762, range 683–1,117 |
| output tokens | mean **145**, median 145, range 129–160 |
| reasoning tokens | **0** — `reasoning: {effort: "none"}` is honoured, and it is verified rather than assumed |
| cost per note | mean **$0.000594**, range $0.000527–$0.000703 |
| cost for the batch | **$0.039828** |
| cost per note, current prompt (`a92d2abc`) | $0.000615 (+3.5%: the medication rule is three lines longer) |
| notes per US dollar | **1,682** |

**The ledger is validated against the provider's own accounting.** For 65 of 67 calls the
list-price model reproduced OpenRouter's reported charge to the cent it reports; two differed by
$0.000001 through rounding. Total drift **+0.005%**. A spend ceiling is only as trustworthy as
its price table, so this is the check that makes the $8 ceiling meaningful rather than notional.

**The output cap is a real bound, not a formality.** `MAX_OUTPUT_TOKENS = 1024` puts a hard
ceiling of $0.00256 on any single note's output; measured output is 145 tokens, 14% of the cap.
A runaway generation is bounded before the budget notices.

**Correcting the proposal.** Section 5 projected ~2,000 input and ~250 output tokens at
~$0.0012 per note — 2× the measured cost. The gap is the JSON schema: it is 6,137 characters,
yet measured input (777 tokens) is about the system instruction plus the note alone, which is
consistent with the schema not being billed as prompt tokens. That inference is not yet proven;
the A/B that settles it is to strip the schema descriptions and re-measure input tokens.

## 2. How it scales

At 24 notes a day, 250 days:

| Unit | Notes / year | Inference cost |
| :--- | ---: | ---: |
| one physician | 6,000 | **$3.57** |
| 100 physicians | 600,000 | $357 |
| 1,000 physicians | 6,000,000 | $3,566 |

The $8 project ceiling would have covered **13,468 notes**. Lifetime spend across the whole
project is **$0.082276 over 136 calls** — two full batch runs, one schema probe and a smoke test
— which is 1.0% of the ceiling. The budget was never the binding constraint; hand-labelling time
was, and that is worth saying plainly because it is the opposite of what the pricing discussion
in the proposal assumed.

## 3. The number that actually decides the business case

It is not dollars. This system is a drafting aid with mandatory physician sign-off, so the
human is in the loop on every note by design; the cost model is therefore not "agent or human"
but:

```
Cost_note = Cost_call + Cost_verify  +  corrections × Cost_correct
                        (always paid)   (measured: 0.97 per note)
```

Measured per note, counting a correction as any field that is shown and wrong, or blank when
the dictation did state it:

| | Measured |
| :--- | :--- |
| notes needing **no** correction (S) | **30 / 67 = 0.448** |
| field decisions needing correction | 65 / 268 = 0.243 |
| corrections per note | **0.97** (0 on 30 notes, 1 on 16, 2 on 14, 3 on 7, never 4) |
| fields shown and wrong, per note | 0.75 → **~4,478 per physician-year** |

That last row is the real risk statement. At present the system shows a physician roughly
4,500 confidently wrong fields a year. That is precisely why this is an administrative drafting
aid with mandatory review and no autonomous write-back, and why the silent-failure rate — not
recall — is the metric to drive to zero.

## 4. ROI, and the sensitivity that matters

**Assumption, stated as one:** verification takes ~10 s per note and a correction ~30 s.
Neither is measured. The study that settles it is a time-and-motion comparison with five
physicians, retyping against verifying, which this project has not run. Everything below moves
linearly with those two numbers, so treat the ratios as robust and the absolute dollars as
indicative.

Per note, at three fully-loaded clinician rates:

| Clinician cost | Inference | Verify (10 s) | Corrections (0.97 × 30 s) | Total | Inference share |
| :--- | ---: | ---: | ---: | ---: | ---: |
| SGD 60/h | $0.000594 | $0.167 | $0.485 | $0.652 | **0.091%** |
| SGD 100/h | $0.000594 | $0.278 | $0.809 | $1.087 | **0.055%** |
| SGD 150/h | $0.000594 | $0.417 | $1.213 | $1.630 | **0.036%** |

**Sensitivity — the analysis the C2 capsule asks the End-of-Course project to produce.**
Compare halving the token price against improving field accuracy by five percentage points:

| Change | Saving per note (SGD 100/h) |
| :--- | ---: |
| token price −50% | $0.0003 |
| field accuracy +5 pp | $0.167 |
| **ratio** | **561×** (337× at SGD 60/h, 842× at SGD 150/h) |

So: **token price is not a decision variable in this workflow; accuracy is, by roughly three
orders of magnitude.** That is the C2 conclusion — accuracy beats price — arriving as a
measured result rather than a slogan, and it decides the engineering priority.

**Priced against that scale, the schema change is the whole business case.** The measurement in
`evals/diagnose.py` shows that where the labeller and the model name the same drug, dose is
right 24 times in 26 and frequency 30 in 30. If moving from one medication slot to a list took
corrections from 0.97 to ~0.25 per note — a target derived from that conditioned measurement,
not a result — the saving at SGD 100/h is about $0.60 per note, or **$3,600 per
physician-year, against $3.57 of inference**. One engineering change is worth a thousand times
the entire inference bill.

## 5. Build vs buy, layer by layer, re-priced

| Layer | Decision | Why, with what it actually cost |
| :--- | :--- | :--- |
| Interface & serving | **Own** | `demo/build_review.py` → one static HTML file, no server, no dependencies. The verification view is the product; renting a generic form would lose the evidence-beside-value layout the whole design exists for. |
| Orchestration | **Own** | One file, `src/extract.py`, no framework. The payload and every request parameter are line-by-line auditable, which is what made the cost model checkable against the provider to 0.005%. |
| Model | **Rent** | `google/gemini-2.5-flash` via OpenRouter on the OpenAI SDK. $0.000594 a note and 1,216 ms median: nothing self-hosted competes at this volume (§6). |
| Data & retrieval | **Rent / none** | A public dataset and no retrieval layer — the answer is always inside the note, so RAG would add cost and a failure mode for no recall. |
| Evaluation & observability | **Own** | The sealed gold set, `evals/run_ekacare.py`, `evals/scoring.py`, `evals/diagnose.py`. This is the layer that cannot be bought: it encodes the clinical definition of correct. It is also where most of the build effort went, which is the honest summary of this project. |

**OpenRouter versus the direct Gemini API.** Measured markup in this run: none — the
provider-reported charge matched list price. What the routing layer bought: one key, a model
switch that is a CLI flag rather than a rewrite, `provider.data_collection: "deny"` as a
request parameter, and a per-call cost figure returned in the response, which is what the spend
ledger reconciles against. What it costs: one more hop and one more dependency in the supply
chain (OWASP LLM04), mitigated by pinned SDK versions and a recorded `served_model` per call.

## 6. Rent or self-host — the break-even

An L4/A10-class on-demand instance at **USD 0.50–1.20/hour** (assumption; on-demand cloud list
prices, not negotiated). At $0.000594 a note, one dollar of API buys 1,682 notes, so:

- break-even at $1.00/hour ≈ **1,682 notes/hour sustained** ≈ 40,000 notes a day
- at 24 notes per physician-day that is roughly **1,700 physicians** before a self-hosted
  endpoint is merely cost-comparable

And cost is the wrong reason anyway: inference is 0.05% of the per-note cost (§4), so
self-hosting to save it is irrational. The two real reasons to self-host are **data residency**
— Singapore PDPA obligations on live patient data, where a region-pinned or on-premise endpoint
may be required regardless of price — and removing a third-party dependency. Either decision has
to be paid for in accuracy, and a cheaper token is worthless if recall falls: the same harness
that produced every number here is what would measure that, which is the argument for having
built it.

## 7. Time to deploy, measured from this repository

| Milestone | Date | Elapsed |
| :--- | :--- | :--- |
| repository initialised | 2026-09-16 | — |
| working end-to-end slice (Colab MVP, one note) | 2026-09-17 | 1 day |
| single-file pipeline with gates, ledger, exit codes | 2026-09-18 | 2 days |
| 67 cases hand-labelled and sealed | 2026-09-20 | 4 days |
| first full live batch, 67 notes, 0 harness errors | 2026-09-20 | **5 days** |

Five days from empty repository to a measured evaluation over a sealed gold set. The
distribution of that time is the finding: the model call was working on day one, and four of
the five days went into evaluation and labelling.

## 8. Scalability, and the limits enforced in code

| Concern | What is enforced, and where |
| :--- | :--- |
| Per-call latency | SDK timeout 15 s, `max_retries=1`. First run: p50 1,216 ms, p95 1,608 ms, max 2,442 ms, all 67 inside the 3,000 ms budget. Current run: p50 1,213 ms, p95 1,600 ms, one outlier at 11,993 ms, so **66 of 67**. `within_budget` is recorded per note and now means API plus local time; measuring only the local clock reported that 12-second call as inside the budget |
| Output size | `MAX_OUTPUT_TOKENS = 1024`, hard ceiling $0.00256 per note |
| Input size | `MAX_NOTE_CHARS = 20_000`; the largest note in the corpus is 9,450 |
| Runaway spend | Pre-flight worst-case check against a file-locked ledger; a $8 ceiling that refuses before calling, not after |
| Repeated failure | Circuit breaker: 3 consecutive system failures, or >5% after 20 calls, halts the run with its own exit code |
| Re-running cost | Per-case cache keyed to model and prompt fingerprint — a resumed run never pays twice, and a cache built by a different system is refused rather than mixed |

**Not measured:** sustained throughput per API key, provider rate limits, and behaviour under
concurrency. The batch loop is deliberately serial, so the 67-note run took 86 seconds and
nothing about parallel scaling has been tested. Stated rather than estimated.

## 9. What these numbers cannot tell you

- Verification and correction times are **assumptions**, not measurements (§4). The ratios hold
  under any plausible value; the absolute dollars do not.
- The clinician hourly rates are illustrative ranges, not a sourced Singapore figure.
- GPU pricing is on-demand list, not negotiated or reserved.
- Allergy extraction has **no** cost or accuracy basis at all: the corpus contains zero allergy
  positives, so that quarter of the schema is unpriced and unmeasured.
- Everything here is single-user and single-region. Integration with a live EHR, the clinical
  safety case, and the governance process are the real deployment costs, and none of them are
  inference costs.
