# MediExtract: business and technical trade-off analysis

*PE6201 deliverable 1 of 3. ~1,030 words, against a 1,200-word limit. Every figure here is
reproducible from an artefact in this repository; `tests/test_guardrails_doc.py` fails if a quoted
metric drifts from the file it came from.*

**The decision being made.** Dr. Aisha, a general practitioner in a Singapore polyclinic, dictates
a consult in ninety seconds, then retypes medication, dose and frequency into the EHR to close the
chart. Ambient scribes already produce fluent prose; none show her *where* a value came from, and
she will not sign an AI-generated dose she cannot verify in under three seconds. Every trade-off
below follows from optimising verification speed over fluency.

## Technical trade-offs

| Decision | Chosen | Rejected | The evidence that decided it |
| :--- | :--- | :--- | :--- |
| Extractor | Foundation model, structured output | Rules/lookup as primary | 0.730 pooled recall against **0.381** for a committed regex + 14,689-name RxNorm baseline |
| Gates | Deterministic Python | Model self-check | A wrong dose is expensive; `if` statements are auditable and free |
| Retrieval | None | RAG | The answer is always inside the note. Retrieval adds a failure mode (wrong chunk) for zero possible recall |
| Orchestration | One call, no tools | Multi-agent | Changes the *threat model*, not just the bill |
| Framework | None, one file | LangChain | Every request parameter auditable; cost model verified to **0.005%** drift |
| Hosting | Rented API | Self-hosted Qwen | Reasoned, not measured. Crossover ≈ **1,700 physicians** |

**Why rejecting agents is load-bearing.** OWASP's component-versus-actor boundary means the LLM Top
10 (2026) applies to a single-pass extractor and the Agentic Top 10 does not. Adding one tool would
change what the system *is*, invalidating the entire security specification. That is a governance
saving, not a latency one.

**The gates are the product.** A field that cannot be grounded is wiped to `null` — never to a
display string. Precision 0.750, silent-failure rate 0.252 — **31 of those 36 fields are one
defect, drug selection in a multi-drug note, owned by the schema and reachable by no gate**;
two controls proposed to reduce it were measured and refused. Enforcing verbatim evidence spans
roughly doubles output tokens, about **$1.90 per physician-year**: the explicit cash price of the
safety property.

## Business trade-offs

| Line | Measured |
| :--- | ---: |
| Cost per note | **$0.000617** |
| Cost per *correct* field | **$0.000383** |
| Per physician-year | **$3.58** |
| Median latency | 1,293 ms (66/67 inside 3 s) |

**Accuracy beats price by three orders of magnitude.** At SGD 100/h clinician time, five percentage
points of accuracy are worth **561×** a token-price saving. Token price is therefore not a decision
variable in this workflow — which inverts the usual build-vs-buy instinct. Self-hosting to save
inference cost would optimise the wrong term.

**The highest-value change is a schema change, not a model change.** Where labeller and model name
the same drug, dose is correct 24/26 and frequency 30/30 — the residual error is almost entirely
the single medication slot. Moving to a medication *list* is projected to save ~**$3,600 per
physician-year** in clinician time against a **$3.58** annual inference bill.

## The trade-offs that cost us something

**Structural dependency.** Stripping ALL-CAPS section headers drops allergy recall **0.900 →
0.650** (McNemar p = 0.0625, the exact floor at n = 20). Enumerating all thirty gates showed why
none fired: **every gate is a precision control; none is a recall control.** Re-dictating the same
consults around byte-identical labels moved recall 0.730 → 0.750 (p = 0.6072, a null), localising
the dependency to the header specifically rather than to formatting in general.

**An LLM judge was built and refused.** The adoption gate was written before the code: 0.9
agreement on *both* classes. Measured 0.9467 where gold says correct, **0.2093** where gold says
wrong — and a judge answering "correct" to everything scores 0.8396 against the real judge's
0.8284. It does not beat a rubber stamp. Automated evaluation was rejected; hand labelling still
stands behind every prompt change. Cost of learning this: $0.0696.

**Prompt-level defence is not a control.** A live red-team run ($0.0173 of a $0.50 allocation)
found the model **obeyed 5 of 10 prompt injections**, planting `warfarin 10 mg daily` and
`5000 mg` into fields. The deterministic tripwire downgraded all five to review, so **none reached
a payload as verified**. With the encoding pre-filter disabled, 3 of 16 homoglyph vectors put a
lookalike (`mеtformin`, Cyrillic е) into a field marked `found` — the grounding gates cannot catch
these, because a homoglyph in the note *is* verbatim in the note. Defence in depth, measured: the
probabilistic layer failed, the deterministic layer held.

## What was given up

- **Recall for precision.** Abstention is the default on anything ungroundable. A blank costs a
  manual entry; a wrong dose costs more.
- **Breadth for auditability.** One slot per field, not a list — carrying most of the residual
  error, kept because changing it invalidates the sealed labels.
- **Automation for trust.** No judge, no self-grading; every score traces to a hand-labelled
  answer key.
- **Statistical power for honesty.** n = 67. Wilson intervals on every rate, a paired test on every
  delta, and a large p-value reported as *the evaluation is too small to tell* rather than as
  equivalence.

## Verification

519 tests pass standard and under `python -O`, which strips `assert` — the reason no runtime gate
may be one. `tests/test_guardrails_doc.py` parses the security specification with Python's `ast`
module and fails if a quoted metric no longer equals the artefact it names. Five corpora are
hash-sealed at `0444` and tagged. Total project spend: **$0.32 of an $8 ceiling**.
