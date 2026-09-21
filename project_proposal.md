# MediExtract: Project Problem Statement (PE6201)

**Author:** Rohit Panda  
**Section:** B  
**Date:** 19-August-2026  
**Course:** PE6201 Emerging AI Technologies  
**Milestone:** End-of-Course Project · Milestone 1 (Formative)  
**Working Title:** MediExtract — turns a dictated consult note into schema-valid fields, with a verbatim source quote behind every field and a blank where it cannot find one.

---

## 1. Working Title
**MediExtract** — turns a dictated consult note into schema-valid fields, with a verbatim source quote behind every field and a blank where it cannot find one.

---

## 2. The Problem, and Why It Matters
Family-medicine physicians spend 5.9 hours of an 11.4-hour workday inside the EHR; 157 minutes of that (44.2%) is clerical work (documentation, order entry, coding), alongside 86 minutes of after-hours "pyjama time" per weekday. Dictating the consult is fast; retyping that dictation into discrete EHR fields is not, and retyping is the part a machine can automate. 

Ambient scribes (Nuance DAX, Abridge, Suki, EkaScribe) generate note prose well, and Amazon Comprehend Medical extracts clinical entities. However, none of them make it cheap to see which words in the dictation justify each discrete field—which is what a physician needs to sign off in seconds instead of re-reading the whole note.

**Explicit Out of Scope:**
* Audio capture and ASR (pipeline starts from text).
* Mapping extracted entities to RxNorm/SNOMED codes.
* Writing directly to a live EHR.

---

## 3. Who It’s For, and the Domain
* **Primary User:** The attending physician at the moment she closes an unfinished note.
* **Persona:** Dr. Aisha, a family physician at a Singapore polyclinic seeing 24 patients a day. At 21:40, she opens her ninth unfinished chart, reads back her dictation, and retypes six fields into the EHR form. She knows medicine cold; what she does not know is whether a machine’s suggestion can be trusted at a glance. She will not accept a field she cannot verify in under 3 seconds, and a plausible wrong dose costs far more than a blank box.
* **Core Design Constraint:** Every field is displayed beside the verbatim phrase from her dictation that produced it. Any field the system cannot ground in her own words is left blank rather than guessed. The workflow shifts her from retyping to reviewing.
* **Domain:** Healthcare / Clinical documentation. (Secondary downstream beneficiaries: clinical documentation specialists and coding teams).

---

## 4. Why AI — And Which Kind?
* **Decision:** Prompted foundation model with schema-constrained structured output (Gemini 2.5 Flash via OpenRouter).
  * *Why Not RAG?* There is nothing to retrieve; the entire context is inside the input note.
  * *Why Not Agents?* It is a single pass with no external tools. Multi-agent loops add cost, latency, and failure modes without adding value.
* **Committed Non-AI Baseline:** Regex + drug-name gazetteer extractor (dose patterns, frequency shorthand, RxNav ingredient lists). Evaluated on the exact same gold set. Shorthand like "po qd" is trivial for regex; what breaks rule-based systems are complex linguistic structures like negation ("denies chest pain"), attribution ("mother has diabetes"), and temporality ("was on metformin until March"). These cases are reported separately to justify model deployment.
* **Model vs. Rules Boundary:** The LLM performs semantic translation only. Schema validation, evidence-span containment, and numeric/unit dosage checks remain deterministic in Python because arithmetic is verifiable and wrong doses are high-risk.
* **Unclimbed Rung (Terminology Normalisation):** Purpose-built biomedical resolvers achieve 82.7% top-3 accuracy vs. Amazon Comprehend Medical (55.8%) and GPT-4 (8.9% on John Snow Labs benchmark). Terminology coding is explicitly left to dedicated downstream services.

---

## 5. Proposed Approach — Build vs. Buy
* **Interface & Serving (OWN):** Custom thin review form displaying fields alongside verbatim evidence spans and blanks for abstentions.
* **Orchestration (OWN):** Direct Python pipeline function without heavy frameworks (LangChain/LlamaIndex).
* **Model Layer (RENT):** Foundation Model API (`google/gemini-2.5-flash` via OpenRouter) with JSON-schema-constrained decoding.
* **Data & Retrieval (RENT):** Public datasets; no retrieval infrastructure.
* **Evaluation & Observability (OWN):** Frozen gold set, per-field scoring, gated vs. ungated runs, and judge calibration.
* **Cost & Latency Analysis:**
  * ~2,000 input + ~250 output tokens per note (1 API call).
  * At Gemini 2.5 Flash list prices ($0.30/1M input, $2.50/1M output), base cost is ~$0.0012 per note (<$8.00 per physician-year at 24 notes/day × 250 days).
  * Enforcing evidence spans roughly doubles output tokens (~$0.0003 additional/note, ~$1.90/physician-year)—the explicit cash price of safety.
* **Low-Code / Prototyping:** Prompts and schemas were prototyped in Google AI Studio before migrating to Python for evaluation harnesses, span-containment validation, and abstention testing.
* **Stretch Goal:** Compare the rented API against a locally hosted small language model (e.g., Qwen) on token cost, latency, and data residency.

---

## 6. Data & Leakage Management
* **Primary Evaluation Set:** `ekacare/clinical_note_generation_dataset` (Hugging Face, MIT license) — 156 transcribed doctor-patient conversations (test split) annotated against EkaCare clinical schemas.
  * *Subset Evaluated:* 67 Latin-script (English) rows evaluated for core recall metrics.
  * *Out-of-Distribution Slice:* 88 Devanagari rows (Hindi/Marathi) and 1 Telugu row.
  * *Reference Context:* Published generation scores: EkaScribe (79.6%), Claude Sonnet 4 (72.6%), GPT-4o (63.2%), MedGemma3-4B (49.9%).
* **Stress Set:** `MTSamples` (Kaggle) — 4,999 dictated reports. Used to test generalisation across genres, header leakage, and Allergy recall (since Eka Care contains only 2 allergy instances in 67 Latin rows). Repo ships a loader script and a 20-row hand-labelled sample.
* **Leakage Reporting:** MTSamples notes carry ALL-CAPS section headers (e.g., `MEDICATIONS:`, `ALLERGIES:`). Scores are reported twice—headers intact vs. headers stripped—across both the regex baseline and the model.
* **Privacy & Data Residency:** Both sets are public and de-identified. Production deployment on real PHI would require a Data Processing Agreement (DPA) with no-training clauses, a Singapore PDPA region-pinned endpoint, or a local SLM.

---

## 7. Success Metrics & Evaluation Harness
* **Headline Metric:** Critical-field recall (medication name, dose amount + unit, frequency, allergy) on Latin-script Eka Care notes.
  * *Target:* $\ge 0.85$ recall. Primary baseline comparator is the Python regex + gazetteer model.
  * *Metric Justification:* Recall is prioritized over precision because silent omission of a prescribed drug is a severe clinical failure; precision is reported alongside to expose trade-offs.
* **Abstention Protocol:** Fields carry `status ∈ {found, not_stated, unsure}` plus a verbatim `evidence` span. The pipeline runs twice:
  1. *Gated Run:* Enforces `evidence in source_text` with explicit `if/else` routing (blanking ungrounded output). See delta D1: the `assert` of the original draft was replaced before any live run.
  2. *Ungated Run:* Disables the gate to measure what percentage of abstained fields were actual hallucinations or mismatches.
* **Deterministic Hallucination Gate:** Pure Python containment check on the span, routed with explicit `if/elif/else` and never with `assert` (reported strict, with whitespace/case matches counted separately as `near_miss` rather than normalised into a pass). The evaluation harness never "snaps" output spans to force matches.
* **Gold Standard & Calibration:** Ground truth gold set (`gold_v1`) frozen and git-tagged prior to evaluation runs. Human hand-labeling calibrates against the Eka Care LLM rubric judge on 30 cases.

---

## 8. Risks, Limitations & Responsible Use
* **Intended Use:** Administrative drafting aid pre-filling EHR fields for explicit physician review and sign-off.
* **Explicit Non-Use:** Not a diagnostic tool, not clinical decision support, no autonomous EHR write-back, not for billing/coding, not for live PHI without local/DPA guardrails.
* **Risk Matrix & Mitigations:**
  | Risk | Failure Mode | Mitigation |
  | :--- | :--- | :--- |
  | **Silent Failure** | Valid JSON, wrong dose or fabricated drug | Mandatory verbatim evidence span + Python `evidence in source_text` gate. Ungrounded fields route to manual entry. |
  | **Linguistic Errors** | Negation, attribution, temporality ("denies chest pain") | Measured via frozen gold-label evaluation and reported as a dedicated error metric. |
  | **Silent Omission** | Missing prescribed medications | Recall as headline metric; candidate counts cross-checked against regex baseline. |
  | **Unit/Route Errors** | 15 mg → 50 mg confusion | Python numeric/unit re-validation against the extracted span. Abstain on mismatch. |
  | **Prompt Injection** | OWASP LLM01 via malicious note text | Note text passed in delimited XML tags (`<note>`); strict Pydantic output validation. |
  | **PHI Exposure** | Cloud API data leakage | Public de-identified data for project; local model / DPA for production. |
* **Governance Frameworks:** Alignment with Singapore IMDA Model AI Governance Framework, EU AI Act, and **OWASP Top 10 for Large Language Model Applications (2026 Edition)**.

---

## 9. Smallest First Version (Minimal Viable Prototype)
* **CLI Execution:** `python extract.py --note gold/case_001.txt`
* **Workflow:** Reads a single note paragraph → calls OpenRouter model with a 4-field Pydantic schema (`medication`, `dose`, `frequency`, `allergy`) → returns structured JSON and prints deterministic pass/fail validation for `evidence in source_text`.

---

## Instructor Feedback Summary (Ajay Vikram Singh)

> **Overall Assessment:**
> "Hi Rohit.. interesting problem statement. and contextual for me as well... Scope/size is OK... Disciplined approach: committed regex baseline, leakage reported before/after, gated-vs-ungated abstention, judge calibration, gold set frozen first. Sounds good to me!"

### Key Guidance & Adjustments Incorporated:
1. **Data Slice Size:** Explicitly documented the English Latin-script subset size (67 out of 156 total rows) as the primary recall evaluation pool.
2. **Outcome Metric Alignment:** Recognized that published Eka Care scores (79.6% / 72.6% / 63.2%) measure overall note *generation* rubrics rather than per-field recall. Cited these figures strictly as background context, establishing the custom Regex Extractor as the primary comparator.
3. **Course Coverage & Safety Standards:** Confirmed coverage across course modules (including explicit out-of-scope justification for agentic workflows in Class 4). Updated safety references from OWASP 2025 to the **OWASP LLM 2026 Edition** (accounting for renumbered top vulnerability classes).
---

## Implementation Delta (recorded 20 September 2026)

Sections 1–9 above are the Milestone 1 statement submitted on 19 August 2026 and are left as
written. This appendix records every place the build departed from it, what was measured
instead, and what is still outstanding. A proposal quietly rewritten to match its results is
worth less than one that shows the distance travelled.

**D1 · `assert` was the wrong construct, and this was caught before any live call.**
Section 7 originally specified `assert evidence in source_text`. Running under `python -O`
strips every assertion from the bytecode, so the one gate the whole design rests on would have
vanished in exactly the deployment mode a production runner is most likely to use. All
clinical validation is now explicit `if/elif/else` routing into named failure states, a test
scans `src/` and `evals/` for the token, and the full suite is run twice — normally and under
`python -O`. Section 7's wording has been corrected in place; this is the one in-place edit.

**D2 · The cost estimate was 2× the measured cost.** Section 5 projected ~2,000 input and
~250 output tokens at ~$0.0012 per note. Measured over 67 live calls: **705 input, 147 output,
$0.000594 per note**, $0.039828 for the batch, zero retries. The projection was pessimistic
because it assumed the JSON schema was billed as prompt tokens; it appears not to be. Full
working in `docs/cost_to_serve.md`.

**D3 · The allergy field cannot be evaluated on this corpus.** Section 6 stated Eka Care
contains "only 2 allergy instances in 67 Latin rows". Measured: **zero**. There is exactly one
allergy mention in all 67 notes (`case_039`, "he has no specific allergies") and it is a
denial, which the field rule correctly labels `not_stated`. The model returned `not_stated`
67/67 — a perfect true-negative record and no positive evidence whatsoever. The MTSamples
stress set promised in section 6 is therefore not optional garnish but the only route to an
allergy recall number, and it is tracked as outstanding below.

**D4 · Recall came in at 0.600 against the 0.85 target, and the reason is not the model.**
Live run `20260920T065552Z-gemini-2.5-flash`, 67 cases, no harness errors: recall
**0.600** (95% CI 0.5214–0.6738), precision 0.6503, silent-failure rate 0.3497, abstention
rate 0.0205. Per field: medication 0.5714, dose 0.5435, frequency 0.6792.

Attributing all 65 failing field decisions (`evals/diagnose.py`) gives:

| Count | Cause |
| ---: | :--- |
| 35 | the model named a different drug from the same note, and the dose and frequency followed that drug |
| 9 | same dosing interval, but the gold phrase also carries "after food" or "for 3 days", which the frequency rule excludes |
| 8 | same drug, with the strength appended to the name (`Dolo 650` against `Dolo`) |
| 6 | the gold dose is `one`/`half`/`no dosage`, which the dose rule says is not a dose |
| 4 | gold and model disagree on whether the field was stated at all |
| 2 | a gate blanked a dose gold had, correctly in both cases |
| **2** | **the model missed a strength that was stated** |

Two of 65. The remainder is the measurement instrument, in three specific places:

- **The field rules contradict each other.** The medication rule offers `'Dolo 650'` as a
  brand name that counts; the dose rule says the 650 in `'Dolo 650'` is the dose. Both cannot
  hold. The labeller resolved it one way and the model the other, which is the 8-field row above.
- **16 of 155 gold labels contradict a rule the gold set itself states.** Seven are rejected by
  `extract.verify_value`, the same gate the pipeline applies to the model. They are listed with
  the rule each breaks by `evals/diagnose.py`, and are queued for gold-v2. `gold_v1.json`
  remains sealed and unmodified.
- **One medication slot cannot represent a note with a median of three drugs (maximum
  twelve).** "The single most clinically significant medication" is not a rule two careful
  readers reproduce: the labeller and the model chose the same drug 40 times out of 56, 71%.
  "The first drug mentioned" is not a better rule — it matches the labels only 40% of the time,
  measured rather than assumed. The labeller's own pre-run note on `case_019` names the drug
  the model chose as an equally valid answer; the metric scored it zero.

Conditioning on the 51 notes where both named the same drug, **dose is right 24/26 and
frequency 30/30**, with a silent-failure rate of 0.011; across the 16 notes where they differ,
recall is 0.209. (Medication is excluded from that first figure, since agreement on the drug
is what defines the subset.) The schema, not the extraction, is the binding constraint, so the
route to the target is a medication *list* with a pre-registered containment metric and
measured precision — not a better model.

**D5 · Status of the five commitments named in the instructor's feedback.**

| Commitment | Status |
| :--- | :--- |
| Gold set frozen first | **Done.** 67 cases, 268 labels, SHA-256 `1f594e46…`, chmod 0444, prompt fingerprint frozen into the seal, `gold-v1` tag on the commit that carries it. The harness refuses any other file, any SHA drift, any unlabelled field, and any prompt change unless the run is marked `--experiment`. |
| Gated vs ungated abstention | **Done.** Both arms come from one cached call, so the abstention measurement costs no extra spend. Abstention rate 0.0205; all three abstentions were defensible on inspection. |
| Committed regex baseline | **Done.** `evals/baseline.py`, scored through the same gates by `evals/score_arm.py`. Pattern-only recall **0.381** and gazetteer-assisted **0.381**, against the model's 0.600; patterns were tuned on a seeded 20-case subsample and score **0.353** on the held-out 47, which is the number to quote. The majority-class baseline ("always `not_stated`") agrees with gold on 113 of 268 field decisions (42.2%) at recall 0. Full comparison in `docs/technique_selection.md`. |
| Leakage reported before and after | **Done** (D15). 20 MTSamples notes with a positive allergy under an ALL-CAPS header, sealed twice — headers intact and headers stripped, same cases, same labels — and scored paired. Allergy recall **0.900 → 0.650** when the header goes. |
| Judge calibration | **Outstanding.** Being built as a second annotator from a different model family, scored against gold-v1 *before* any adjudication, never permitted to edit a label on its own, with every adjudication resolved in the system's favour counted separately. |

**D6 · Transport confirmed.** `google/gemini-2.5-flash` is live on OpenRouter through the
OpenAI Python SDK; the 404 recorded during prototyping came from Google's direct API, not from
the model being retired. Strict `response_format: json_schema` is honoured, SDK-level timeout
15 s and one retry, `provider.require_parameters` on, and 67/67 calls finished inside the 3 s
verification budget (p50 1,216 ms, p95 1,608 ms, max 2,442 ms).

**D7 · The pipeline inherits the ASR's errors, and that is visible in the data.** Section 2
puts audio capture out of scope. `case_045` dictates "Dolo 450", a strength Dolo is not sold
in; the pipeline grounded it faithfully because the phrase really is in the transcript. A
verbatim-evidence design propagates an upstream transcription error rather than correcting it —
which is the argument for the physician sign-off step, and for a formulary plausibility check
as the next deterministic guardrail rather than a larger model.

**D8 · The gazetteer the baseline was promised does not work on this corpus.** Section 4 named
"RxNav ingredient lists" as part of the non-AI comparator. It was fetched rather than typed,
with its source and retrieval date recorded (`data/gazetteer/fetch_rxnorm.py`,
14,689 names, 2026-09-20), because a drug list written from memory after reading the gold labels
would be contaminated. The result: it can name **6 of 56** gold medications, **10.7%**, and it
moves pooled recall by **zero** while costing precision (0.527 → 0.488). RxNorm is a US
vocabulary of ingredients; these are Indian dictations full of local brands. The promise is kept
in the sense that the approach was built and measured, and the measurement is that it fails —
for a reason about vocabulary coverage rather than implementation. It also settles where the
foundation model actually earns its cost: **dose 0.543 against 0.196 (2.8×)**, while on
frequency regex reaches 0.604 against 0.679, so the shorthand half of the task barely needs a
model at all.

**D9 · The interface in section 5 now exists.** "A custom thin review form displaying fields
alongside verbatim evidence spans and blanks for abstentions" is `demo/build_review.py`, which
generates one static, script-free, CSP-locked HTML page from a finished run. Building it turned
`guardrails.md`'s control S-07 (output handling, OWASP LLM10:2026) from specified into
implemented, and the corpus made the case for itself: these notes carry literal `<PII>`
de-identification placeholders, which naive HTML interpolation would have swallowed silently.

**D10 · The prompt contradiction is fixed, and it was worth 12.5 points of medication recall.**
Run `20260920T133717Z-gemini-2.5-flash`, `--experiment p0-prompt-fixes`, prompt fingerprint
`a92d2abcb2af4294`, 67 live calls, $0.041221:

| | `73c882d1` | `a92d2abc` | change |
| :--- | ---: | ---: | ---: |
| pooled recall | 0.600 | **0.645** | +4.5 pp |
| pooled precision | 0.650 | **0.694** | +4.4 pp |
| silent-failure rate | 0.350 | **0.308** | −4.2 pp |
| medication recall | 0.571 | **0.696** | **+12.5 pp** |
| dose recall | 0.543 | 0.565 | +2.2 pp |
| frequency recall | 0.679 | 0.660 | −1.9 pp |
| abstention rate | 0.021 | 0.027 | +0.6 pp |
| abstention precision | 0.667 | 0.750 | +8.3 pp |

Three changes, each aimed at a failure the attribution had named: the medication rule now says
to give the product name without its strength (the two rules used to contradict each other); a
dose value must contain a digit, so a mangled `F.milligram` becomes an honest `not_stated`; and
`not_stated` means the empty string, never the word.

**The mechanism is established; the recall gain is not, and the table above overstates it.**
Those are two different claims and only the first is supported:

- The failure category "same drug, strength appended to the name" went from **8 occurrences to
  zero**. That is a count of a named defect disappearing, not a rate, and it is as solid as the
  attribution that produced it.
- The recall figures are paired over the same 155 labelled fields, so the right test is exact
  McNemar over the fields that *changed*: **12 flipped wrong → right, 5 flipped right → wrong,
  p = 0.14**. The 4.5-point pooled gain is **not separated** at α 0.05. Medication alone is 9
  against 2, **p = 0.065** — suggestive and still not separated. `evals/metrics.py` reproduces
  both.

This is the discipline the A2 battery work forced: three identical runs there scored 37, 41 and
49 of 60 with the model held fixed, a spread wider than most gaps between different models, so a
ranking read off single runs is one sample of a noisy process. A large p-value does not say the
two prompts are equally good; it says 155 fields cannot tell them apart, which is a fact about
this evaluation. The remedy is a larger labelled set, which is one more reason gold-v2 matters.

Frequency lost one field — a single flip, p = 1.00 — reported rather than smoothed.

**Two of the three "errors to eliminate" turned out to be the guardrails working.** `case_004`
(gold dose `half`) and `case_016` (gold dose `1 tablet`) are refused by the dose gate because
`FIELD_RULES` states in terms that a quantity per administration is not a dose. The gate is
right and the labels are wrong, so they belong in gold-v2; making the gate accept them would
have bought recall by breaking the rule the project set itself. `case_046` (`F.milligram`) was a
real prompt bug and is addressed above.

**D11 · Two blockers removed, both found by red-teaming the plan rather than by hitting them.**

- *The sealed-gold registry.* A live run could only ever score `gold_v1.json` from the Eka Care
  corpus, because the sealed path and the expected dataset were module constants. gold-v2 and a
  second corpus were therefore reachable only by editing the guard that protects the ground
  truth. Each version now declares its path, its SHA-256 file, its corpus and the label schema
  its file must itself declare, selected with `--gold-version`; every existing refusal is kept
  and four new ones are added.
- *The v4 wire-schema spike.* Before rewriting the pipeline around a medication list, one live
  call ($0.000635) established that strict structured output still works when the schema is an
  array of objects with `maxItems: 12`, alongside `require_parameters: true`. It was **accepted
  on the first attempt**, so the schema change is unblocked. The same probe showed the prompt
  returning one entry for a three-drug note, which is the next thing to change, and that
  `provider.data_collection: "deny"` routes normally — the `allow` used for the first batch run
  was never necessary, and every call since has used `deny`.

**D12 · Financial guardrails, because "monitor the API key" is not a control.** `evals/metrics.py`
audits every call — prompt, completion, cached and reasoning tokens, the list-price estimate
against OpenRouter's own charge, latency, attempts, and the destination including the model
actually served — and adds a second bound scoped to one run (`--run-cap-usd`, default $1.00,
$0.002 for the probe) checked against worst case before each call. The $8 project ceiling still
belongs to `extract.SpendLedger`; the run cap exists because a loop inside a single run would
stay under the project ceiling while consuming it.

Building it found a real hole: the probe calls the endpoint directly rather than through
`call_model`, so its $0.000635 never reached the ledger that is supposed to bound total spend.
There is now exactly one writer per call, pinned in both directions by tests, and the missing
amount has been recorded. Lifetime spend is **$0.082276 over 136 calls** against the $8 ceiling,
and over the 67 calls of the current run the list-price model matched the provider's charge to
**$0.000001** in total.

**D13 · A latency number in this document was resting on the wrong clock.** `within_budget` was
computed from local elapsed time only. In a batch run the payload is built from a cached result,
so the local clock never sees the call — and a 11,993 ms call in the current run was reported as
inside the 3,000 ms budget. End to end now means API time plus local time. Corrected figures:
the first run's API latency peaked at 2,442 ms and **all 67** cases finished inside the budget;
the current run has p50 1,213 ms, p95 1,600 ms and one outlier at 11,993 ms, so **66 of 67**.
The 3-second claim in §3 holds for the median and the 95th percentile, and there is a tail.

**D14 · Measurement and enforcement are now separate modules, and one of them exists because a
number was over-claimed.** `evals/spend_guard.py` enforces: it refuses a call that would cross
the per-run cap and audits every call that is made. `evals/metrics.py` measures: pure functions
over artefacts already on disk, no network, nothing written. The split is deliberate — a module
that can refuse a call should not also be the one that scores it, or a bug in the scoring can
silence the refusal.

`metrics.py` adds three things the earlier tooling lacked. **Populations** that must sum to the
case count, so a case whose payload was an error envelope and a case our own encoding gate
refused to send are never read as model abstentions. **Layer ownership** for every failing field,
because a count with no owner is an observation while a count with an owner is a work item: on
the current run, of 57 failing fields, **38 belong to the schema** (17 to the single medication
slot and 21 to dose and frequency cascading from that choice), **15 to the labels** (9 frequency
labels carrying food timing the field rule excludes, 6 dose labels that are not strengths), 2 are
disputed between labels and model, 1 to the gate — correctly, the label is the defect — and
**1 to the model**. And **separability**, which is what caught D10's over-claim.

It is built so it cannot disagree with `scores.json`: per-field precision and recall use
`scoring.py`'s definitions, the interval is `scoring.wilson`, causes come from
`diagnose.attribute`, and a test asserts the match against every finished run on disk. Writing
its per-field recall independently was the first thing I tried, and it reported medication recall
as 100% against the scorer's 69.6% — because a field that is proposed but wrong is not only a
false positive, it is also a gold fact the physician did not get. Two implementations of one
metric are two answers to one question.

**D15 · The allergy field is measured at last, and a quarter of its performance was
the section header.** Section 6 promised a leakage report and an allergy number, and neither
was possible on Eka Care: all 67 Latin-script notes contain one allergy mention and it is a
denial, so gold-v1 has **zero** allergy positives. Three quarters of the schema was measured
and one quarter was not.

`data/allergy_set/fetch_mtsamples.py` pulls MTSamples through the same datasets-server API as
the Eka Care set and keeps notes with a **positive** allergy under an ALL-CAPS header. The yield
is the first finding: **51 usable notes out of 3,900 scanned**, because almost every note says
"None" or "NKDA". `evals/label_allergy_v1.py` labels 20 of them and seals two variants — the
notes as written, and the same notes with every ALL-CAPS `HEADER:` removed, carrying the *same
case ids and the same labels*, so `evals/metrics.py` compares them paired.

| scope | headers intact | headers stripped | delta | flips right/wrong | p |
| :--- | ---: | ---: | ---: | ---: | ---: |
| **allergy** | **18/20 (90.0%)** | **13/20 (65.0%)** | **−25.0 pp** | **0 / 5** | 0.063 |
| medication | 5/11 (45.5%) | 4/11 (36.4%) | −9.1 pp | 1 / 2 | 1.000 |
| dose | 2/7 (28.6%) | 2/7 (28.6%) | 0.0 pp | 1 / 1 | 1.000 |
| frequency | 3/7 (42.9%) | 3/7 (42.9%) | 0.0 pp | 1 / 1 | 1.000 |
| pooled | 28/45 (62.2%) | 22/45 (48.9%) | −13.3 pp | 3 / 9 | 0.146 |

**Allergy recall is 0.900 with the header and 0.650 without it**, and every one of the five
fields that changed changed in the same direction — no field got *better* when the header was
removed. So the effect is directionally unambiguous: a quarter of the allergy performance was
reading `ALLERGIES:` rather than reading the sentence. That is precisely the before-and-after
the instructor asked for, and it is the argument for not quoting a header-rich corpus as
evidence about dictation.

**And the test cannot reach α 0.05, by construction.** Five discordant pairs all pointing one
way gives an exact two-sided binomial p of 2/2⁵ = **0.0625**; there is no result at n = 20 that
clears 0.05 here. Six one-directional flips would (2/2⁶ = 0.031). That is a power statement
about the design, not a hedge about the finding, and the remedy is more labelled cases — 31
more sit in the pool already.

Three limitations stated rather than discovered later. The notes are **American surgical and
progress notes**, not Singapore polyclinic dictation, so transfer to the deployment target is
limited. The labels are **mine as the assistant, not the human labeller's** — `sealed_by:
claude-opus-5` — which satisfies the "not the model under test" requirement (a different model
family) but is not a clinician's hand. And medication recall here (0.455) is **not comparable
with gold-v1's**: this set's rule is "the first medication in the MEDICATIONS section", declared
because ranking clinical significance across surgical notes is not a judgement I can make.
40 live calls, $0.030111.

**D16 · gold-v2 is built and waits on two signatures.** `evals/label_gold_v2.py` reads the
sealed gold-v1 read-only, verifies its hash before touching it, and applies the 16 corrections
the audit found, each recorded beside the rule that requires it. Fourteen are **rule-derived** —
`half` and `1 tablet` become `not_stated` because `FIELD_RULES` names them as examples of what
is not a dose; seven frequency values lose the meal timing the same rules exclude; `case_001`'s
medication span is repaired to the phrase its value actually names. Two are **review**
corrections: `case_010` has a drug name in the frequency slot and a frequency in the dose slot,
which is legible but is still an inference about intent, so sealing refuses until a human signs
them:

```
./.venv/bin/python evals/label_gold_v2.py validate --seal --labeller rohit \
    --confirm case_010.medication --confirm case_010.frequency
```

Slice tags are attached by recorded pattern rather than by hand, so a slice is reproducible:
`#negation` on 15 cases, `#multidrug` on 30, `#attribution`, `#temporality` and
`#contemplation` on one each. Once sealed, `--gold-version gold-v2` scores against it and
`evals/metrics.py` will pair gold-v1 against gold-v2 to separate the delta from label changes
from the delta from system changes.

