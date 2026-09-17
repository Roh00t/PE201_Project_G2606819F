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
  1. *Gated Run:* Enforces `assert evidence in source_text` (blanking ungrounded output).
  2. *Ungated Run:* Disables the gate to measure what percentage of abstained fields were actual hallucinations or mismatches.
* **Deterministic Hallucination Gate:** Pure Python check executing `assert evidence in source_text` (reported strict and whitespace/case-normalized). The evaluation harness never "snaps" output spans to force matches.
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