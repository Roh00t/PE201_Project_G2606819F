# MediExtract: AI System Engineer Directives (NTU PE6201)

**Role:** Lead AI System Engineer and DevSecOps Architect

**Project:** MediExtract (Low-Latency Clinical Note Extraction Pipeline)

**Objective:** Establish immutable architectural boundaries, security protocols, and operational workflows for enterprise-grade clinical data processing.

This document serves as the master operational directive. Strict adherence to these constraints is mandatory to ensure patient data safety, system determinism, and compliance with institutional standards.

## 1. Core Architecture & Technology Stack

### 1.1 Zero-Framework Policy

* **Single-File Pipeline:** The entire core extraction pipeline must reside exclusively in a single Python script (`src/extract.py`).
* **Prohibited Dependencies:** You are strictly forbidden from utilizing heavy orchestration frameworks such as LangChain, LlamaIndex, or any abstraction layers that obscure prompt payloads or API request parameters. The data flow from raw text to API call to final JSON must be completely transparent and line-by-line auditable.

### 1.2 API Routing & Transport Mechanics

* **Infrastructure:** All inference must be routed exclusively through OpenRouter using the standard, unmodified OpenAI Python SDK.
* **Configuration Parameters:**
* **Base URL:** `base_url="[https://openrouter.ai/api/v1](https://openrouter.ai/api/v1)"`
* **Model Target:** `google/gemini-2.5-flash`


* **Resource Exhaustion Defenses:** Timeouts, retry limits, and maximum token output caps must be strictly configured and enforced at the SDK client level. This guarantees that network anomalies or model hangs will definitively terminate rather than create hanging zombie processes in a batch run.

### 1.3 State Management & Git History

* **Immutable Ground Truth:** The `gold-v1` repository tag is strictly immutable. No Git history rewrites, force-pushes, or retroactive modifications are permitted.
* **Data Integrity:** You must never alter `data/gold_labels/gold_v1.json` once it has been cryptographically sealed.
* **Correction Protocol:** Any subsequent adjustments or manual label corrections must be pushed to a newly versioned file (`gold-v2`).

---

## 2. Security, Guardrails & OWASP 2026 Compliance

### 2.1 Strict Output Validation & Parsing

* **Pydantic Enforcement:** All raw LLM responses must be coerced into a strict JSON format and validated against defined Pydantic schemas before they are allowed to pass to the downstream grounding gates.
* **Pre-Processing Sanitization:** You must actively detect and strip Markdown wrappers (e.g., removing ````json`and ```` block delimiters) from the LLM output *before* passing the string to Pydantic. This prevents fatal parsing crashes when the LLM wraps valid JSON in formatting artifacts.

### 2.2 Deterministic Safety Gates (No `assert`)

* **Vulnerability Mitigation:** You are strictly forbidden from using Python `assert` statements for any runtime clinical data validation.
* **The `python -O` Weakness:** Executing the pipeline in optimized mode (`python -O`) globally strips all assertions from the bytecode. Relying on `assert` would silently bypass all security checks in production.
* **Mandatory Routing:** You must use explicit `if/elif/else` conditional logic that explicitly routes unvalidated data to predefined failure states.

### 2.3 Fail-Safe Data Wiping

* **Deterministic Wiping:** On any gate failure—whether due to an ungrounded extraction, a schema violation, or a missing field—you must execute a hard-wipe of the core data payload (e.g., forcefully setting `drug_name=None`).
* **Database Payload Purity:** Do not insert UI-friendly or debugging strings (e.g., "BLANK", "Abstained due to error") into the actual data payload. The downstream database must only receive `Null/None` or strictly validated types.

### 2.4 Clinical Verification & Anti-Hallucination

* **Value-Mismatch Defense:** The grounding gate must perform a dual-verification check. It is not enough to confirm that the `verbatim_source_phrase` exists in the original source text. The logic must also structurally verify that the extracted *value* (e.g., the specific dosage or frequency) logically aligns with the verbatim quote to prevent hallucinated pairings.
* **Encoding Resilience (Data Provenance):** You must handle hidden unicode characters (e.g., `\u202a`) or homoglyph anomalies as forced, safe abstentions. You must **never** silently mutate, scrub, or "clean" the physician's raw dictation to force a regex pass. The original clinical note is read-only.

### 2.5 Compliance Cross-Referencing

* Before implementing or modifying any validation logic, you must independently cross-reference the proposed changes against `project_proposal.md`, `guardrails.md`, and `slides_notes.md`.
* Acknowledge and ensure that MediExtract operates under the strict boundaries of the **OWASP 2026 LLM Top 10** specifications (specifically noting that this is not an Agentic system, and older 2025 frameworks do not apply).

---

## 3. Unix Pipeline Compliance (I/O Constraints)

### 3.1 Standard Output (`stdout`) strictness

* **Data Only:** Standard output must remain 100% pure, machine-parsable JSON.
* **Zero Leakage:** A single mixed log string, SDK warning, or rogue `print()` statement will fatally break the downstream database ingestion pipe. All `print()` calls directed to `stdout` must be reserved exclusively for the final validated payload.

### 3.2 Standard Error (`stderr`) Routing

* **Telemetry Isolation:** All pipeline telemetry must be routed exclusively to `stderr`. This includes:
* System logging and execution traces
* Evaluation tables and metric outputs
* OpenRouter API errors, timeouts, or rate limit warnings



### 3.3 Explicit Exit Code Mapping

* Do not overload or reuse exit codes. System errors must be mathematically isolated from safe pipeline behaviors to prevent skewed batch evaluation metrics.
* **Code Separation:** A system crash, network failure, or OpenRouter API timeout/429 must return a distinct and dedicated error code (e.g., `exit 6`).
* **Evaluation Integrity:** Network/API failures must *never* return the same exit code used for a successful but safely abstained extraction (e.g., `exit 1`). Failing to separate these will artificially inflate the system's "abstention rate" during evaluation scoring.

---

## 4. Current Operational Priorities

* **Batch Evaluation Execution:** The immediate operational priority is maintaining and executing the batch evaluation loop (`evals/run_ekacare.py`) to process the 67 Latin-script Eka Care clinical cases.
* **Deployment Gate:** Any new code or logic adjustments merged into the repository must successfully pass the full evaluation suite while executing under optimized `python -O` conditions to prove that no security gates rely on fragile assertions.