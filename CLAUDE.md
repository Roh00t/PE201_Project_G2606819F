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

---

## 5. Operational Runbook

Sections 1–4 are the standing directives and are unchanged. This section records how the
repository is actually driven, so a command is never reconstructed from memory.

### 5.1 The verification gate before any commit

```bash
./.venv/bin/python -m unittest discover -s tests          # full suite
./.venv/bin/python -O -m unittest discover -s tests       # §4 deployment gate
./.venv/bin/python -m pyflakes src evals evals/probes tests demo data/gazetteer
```

Both suite runs must be green. `python -O` is not optional: it strips assertions, which is the
whole reason §2.2 forbids them, and `tests/test_guardrails.py:NoAssertInRuntimeCode` scans
`src/`, `evals/` and `demo/` for the statement so the ban cannot rot.

### 5.2 Sealed-gold registry

`evals/run_ekacare.py:REGISTRY` maps a version to its path, its SHA-256 file, the corpus it may
score and the label schema its file must declare. This exists so §1.3 can be honoured without
editing a guard: gold-v2 corrections and a second corpus are selectable rather than reachable
only by loosening `load_gold`.

```bash
./.venv/bin/python evals/run_ekacare.py --gold-version gold-v1     # default
./.venv/bin/python evals/run_ekacare.py --gold-version gold-v2     # refuses until sealed
./.venv/bin/python evals/label_gold_v1.py validate --seal          # seal a labelled set
```

Registered, all four sealed: `gold-v1` (`1f594e46…`), `gold-v2` (`d5e90f5a…`, 18 corrections —
16 rule-derived and 2 signed by `rohit` — plus slice tags), `allergy-v1` (`10725be9…`, 20
MTSamples notes with positive allergies) and `allergy-v1-stripped` (`1d02348e…`, the same labels
with every ALL-CAPS header removed — the leakage report). Adding a version means adding a
registry entry, and **sealing it must stamp the matching `schema_version` into the file** or
loading is refused. `gold_v1.json` is never edited.

A `review` correction in gold-v2 repairs an *intent* rather than applying a rule, so it needs a
name against it:

```bash
./.venv/bin/python evals/label_gold_v2.py validate --seal --labeller <name> \
    --confirm case_010.medication --confirm case_010.frequency
```

### 5.3 Cost control

```bash
./.venv/bin/python evals/run_ekacare.py --experiment my-change --run-cap-usd 0.25
```

Two bounds. The `$8` ceiling is the project's and belongs to `extract.SpendLedger`; `--run-cap-usd`
(default `$1.00`) is this run's and is checked against worst case before every call, because a
loop inside one run would stay under the project ceiling while consuming it. A breach exits 5.

`evals/spend_guard.py` audits every call — tokens including cached, list-price estimate against
the provider's charge, latency, attempts, endpoint, provider, model requested versus served,
response id — to `data/cache/metrics/<run_id>.jsonl`, with an aggregate `metrics.json` beside
the run. It is **never imported by `src/extract.py`**, so §1.1 stands: the pipeline remains one
standalone file.

**Enforcement and measurement are different modules on purpose.** `spend_guard.py` can refuse a
call; `evals/metrics.py` only reads finished artefacts. A module that can refuse a call should
not also be the one that scores it, or a bug in the scoring can silence the refusal.

**Exactly one writer per call.** `extract.call_model` records what it spends. A script that
calls the endpoint directly must pass `CostGuard(records_to_ledger=True)` or its spend never
reaches the ceiling meant to bound it.

### 5.4 Prompt iteration

A prompt change moves `prompt_fingerprint()`, and the sealed gold set records the fingerprint it
was labelled against. That is deliberate friction, not an obstacle to route around.

1. Change `FIELD_RULES` or `SYSTEM_INSTRUCTION` in `src/extract.py`.
2. Confirm the fingerprint moved, and that a plain live run now refuses:
   ```bash
   ./.venv/bin/python -c "import sys; sys.path.insert(0,'src'); import extract; print(extract.prompt_fingerprint())"
   ```
3. Run it as a named experiment, never by relaxing the check:
   ```bash
   ./.venv/bin/python evals/run_ekacare.py --experiment <what-changed> --run-cap-usd 0.25
   ```
4. Attribute the change rather than reading the headline alone:
   ```bash
   ./.venv/bin/python evals/diagnose.py evals/results/<run_id>
   ```
   The first arm must reproduce `scoring.py`'s pre-registered recall and silent-failure rate
   exactly; a test pins that. The other arms are diagnostics and never the headline.
5. Record what moved and what did not in `project_proposal.md`'s Implementation Delta.

**Two rules learned the hard way.** A rule that contradicts another rule costs real recall — the
medication rule offering `Dolo 650` as a name while the dose rule claimed the `650` cost 8
fields. And a prompt cannot fix a gold label: `half` and `1 tablet` are refused by the dose gate
because `FIELD_RULES` says a quantity per administration is not a dose, so those belong in
gold-v2, not in a prompt or a loosened gate.

### 5.5 Comparators, never a bare number

```bash
./.venv/bin/python evals/baseline.py --split report                       # regex, held-out 47
./.venv/bin/python evals/score_arm.py evals/results/<dir> --split report  # scores any arm
./.venv/bin/python evals/probes/probe_v4_schema.py --dry-run              # schema spike, no spend
./.venv/bin/python demo/build_review.py                                  # the review screen
```

`score_arm.py` prints the majority-class baseline beside every arm, because a recall figure with
nothing to compare against says nothing: answering `not_stated` to everything already agrees
with gold on 113 of 268 field decisions.

### 5.6 Never report a gap without testing it

```bash
./.venv/bin/python evals/metrics.py evals/results/<run_a> evals/results/<run_b>
```

Two runs over the same gold set are **paired**, so only the fields that changed carry
information and the test is an exact McNemar over those flips. Report the p-value with the delta,
every time.

The rule exists because it was broken once. The prompt correction of 2026-09-20 was reported as a
4.5-point recall improvement; paired, it is 12 flips one way against 5 the other, **p = 0.14**,
which is not separated at α 0.05. The A2 battery work is where this discipline comes from: three
identical runs there scored 37, 41 and 49 of 60 with the model held fixed, a spread wider than
most gaps between different models.

Two claims, two confidence levels, and they must not be blurred:

- **A named defect disappearing is a count.** "Same drug, strength appended" went from 8
  occurrences to 0. That is solid.
- **A recall delta is a rate.** It needs a test before it is called an improvement.

A large p-value never means two systems are equally good. It means the evaluation is too small to
tell, which is a fact about the evaluation.

### 5.7 Scoring against a second gold version

A corrected answer key raises the score without the extractor changing, so the two must be
reported apart:

```bash
# the label correction alone - re-scores saved outputs, makes NO model call
./.venv/bin/python evals/score_arm.py evals/results/<run> --gold data/gold_labels/gold_v2.json
./.venv/bin/python evals/metrics.py evals/results/<run> \
    --gold data/gold_labels/gold_v1.json --gold-b data/gold_labels/gold_v2.json
```

`score_arm.py` writes `scores-vs-<goldstem>.json` when the gold is not the run's own, so a
cross-gold measurement can never overwrite the run's own scores. Measured for gold-v2: of the
8.5-point move from 0.645 to 0.730, **7.8 points are the labels (p = 0.008) and 0.7 is sampling
noise that does not separate (p = 1.000)**.

`metrics.compare_golds` pairs only fields that are `found` in **both** versions; the 8 withdrawn
and 1 added label are reported separately and never counted as flips.


### 5.8 Judge calibration

```bash
./.venv/bin/python evals/judge_calibration.py evals/results/gold-v2-live --dry-run
./.venv/bin/python evals/judge_calibration.py evals/results/gold-v2-live \
    --gold data/gold_labels/gold_v2.json --arm baseline --cap-usd 0.08
./.venv/bin/python evals/judge_calibration.py evals/results/gold-v2-live \
    --arm swap --limit 20 --cap-usd 0.03        # position bias
./.venv/bin/python evals/judge_calibration.py --report evals/results/judge-gold-v2-live
```

The judge is `openai/gpt-5-mini`, **a different family from the system under test**, because a
model judging its own family reads a shared blind spot as agreement. It never sees the gold label,
and its verdict never reaches a payload — both are tests, not conventions.

`--report` **recomputes** from the saved verdicts rather than re-reading the saved summary, and it
makes no calls. It exists because a statistic that is only ever read back cannot be checked against
its rows: a `--arm verbose --limit 20` run had already rewritten a 268-decision headline as an
80-decision one, silently. The calibration is now always scored over the cases the *baseline* arm
judged, whatever the current invocation passes.

**Report the rubber-stamp row or report nothing.** A judge that answers "correct" to everything
scores 0.8396 on this data, because most extractions are already right. The measured judge scores
0.8284, so it does not beat one. Agreement figures need a comparator for the same reason recall
figures do (§5.5), and here the comparator is what refuses the judge.

**κ and AC₁ are both reported, with their chance terms.** They differ by 0.59 here — 0.1967 against
0.7827 — because both raters accept about 84% of decisions and Cohen's chance term is 0.7863 while
Gwet's is 0.2103. Quoting whichever is more flattering would be the whole failure mode.

The adoption gate was written in `guardrails.md` §6.1 before the code existed and is **not met**
(0.9467 on the class gold calls correct, 0.2093 on the class it calls wrong), so the judge is not
adopted and hand labelling still stands behind every prompt change.

### 5.9 The near-distribution arm

```bash
./.venv/bin/python evals/paraphrase.py report                    # what would change, no writes
./.venv/bin/python evals/paraphrase.py validate --seal --labeller <name>
./.venv/bin/python evals/run_ekacare.py --gold-version paraphrase-v1 \
    --experiment near-distribution-paraphrase --run-cap-usd 0.10
./.venv/bin/python evals/metrics.py evals/results/gold-v2-live \
    evals/results/paraphrase-v1-live --gold data/gold_labels/gold_v2.json
```

Six named transforms re-dictate the prose and **never touch a gold value span**. That invariant is
what makes the arm worth running: the value labels stay byte-identical to gold-v2, so the McNemar
against the gold-v2 live run is exact rather than approximate. `refuse_if_value_lost` blocks a
build that drops a label, and `refuse_if_corpus_barely_moved` blocks one where under 80% of cases
changed — a corpus that barely moved is gold-v2 wearing a different name.

Pair it with §6.6 or do not report it. Alone, "recall unchanged under paraphrase" is a shrug;
beside "recall falls 25 points when an ALL-CAPS header is removed" it localises the dependency to
the header rather than to formatting in general. **The p-value is 0.6072 and that is a null, not
an equivalence** — 15 discordant pairs cannot resolve an effect under about 8 points.
