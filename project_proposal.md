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
| Leakage reported before and after | **Outstanding.** Depends on the MTSamples set (D3), whose ALL-CAPS `ALLERGIES:`/`MEDICATIONS:` headers are the leak to strip. |
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

