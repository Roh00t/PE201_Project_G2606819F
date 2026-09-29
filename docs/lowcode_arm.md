# The low-code control arm

`project_proposal.md` §5 records that prompts and schemas were prototyped in Google AI Studio
before migrating to Python. The watch-outs ask for the measured version of that sentence — *"I
tried X first, it broke on Y, so I built Z"* — and until now the project had the sentence without
the number. This is the number.

**Reproduce it with no API calls:**

```bash
./.venv/bin/python evals/lowcode_arm.py build
./.venv/bin/python evals/score_arm.py evals/results/lowcode-proxy \
    --gold data/gold_labels/gold_v2.json
./.venv/bin/python evals/metrics.py evals/results/lowcode-proxy \
    evals/results/gold-v2-live --gold data/gold_labels/gold_v2.json
```

## What the arm is, and what it is not

`run_ekacare.py` writes two files from one cached call. `gated.jsonl` is the delivered payload;
`ungated.jsonl` is the model's raw structured output with `gate_enabled: false`. That second file
is **the same model, the same prompt and the same strict JSON schema with the deterministic layer
removed** — which is the capability boundary a console has. `evals/lowcode_arm.py` materialises it
as a scoreable arm so the **pre-registered** scorer grades it by the same rules as everything else.

> **It is a proxy, not a measurement of Google AI Studio.** The payloads still enjoy this
> project's `FIELD_RULES` prompt engineering, which a console could also be given.
> [`lowcode_pack/`](lowcode_pack/) closes that gap: the exact prompt, the exact schema, and a
> converter that turns a real console run into payloads this same path will score.

**One correction worth recording, because the first version of this measurement was worthless.**
Copying `ungated.jsonl` verbatim scored *identically* to the coded pipeline — recall 0.7297,
precision 0.7500, wiped 5. The reason is that the ungated file retains the `gate` array: the
verdicts are **reported and simply not applied**, and `evals/scoring.py:score` reads `proposed`,
`verified` and `wiped` from those codes rather than from whether a value is null. So the first arm
compared the system with itself and would have published the null result as a finding.
`lowcode_arm.py:console_payload` fixes it by replacing the gate array with what a console reports
— nothing — so every field carrying a value is delivered as stated.

## Table 1 — the same 67 notes, gate on and gate off

| | Low-code console (no gate) | Coded pipeline (gated) |
| :--- | ---: | ---: |
| Values delivered to the physician | **147** | 144 |
| Correct | 108 | 108 |
| Presented as verified | **147** | 143 |
| **Silent failures** (wrong *and* shown as verified) | **39** | **36** |
| Fields held back | **0** | 5 (+1 to review) |
| Recall | 0.7297 | 0.7297 |
| Precision | 0.7347 | **0.7500** |
| Silent-failure rate | 0.2653 | **0.2517** |

**Recall is identical, to the field.** The paired McNemar over the gold-found fields is **0 flips
either way, p = 1.000** — there is nothing for a statistical test to find, and reporting a
"significant improvement" here would be inventing one. The gates cost **zero** correct answers on
this run.

**So the difference is a count, not a rate**, and per `CLAUDE.md` §5.6 it is reported as one.

## Table 2 — the three values a console would have shown, named

| Case · field | Gate code | What a console displays | Gold |
| :--- | :--- | :--- | :--- |
| `case_004.dose` | `ABSTAIN_NUMERIC_MISMATCH` | `half` | not stated |
| `case_016.dose` | `ABSTAIN_INCOHERENT` | `not_stated` | not stated |
| `case_046.dose` | `ABSTAIN_NUMERIC_MISMATCH` | `not_stated` | `40` |

**All three are wrong, and two of them are the literal string `not_stated`.** That is the model
emitting the *word* instead of an empty value — the defect `project_proposal.md` D10 corrected in
the prompt, still occurring at a low rate. A console would write the token `not_stated` into a
clinical dose field and present it as verified. `src/extract.py:parse_output` and the dose gate are
what stop it.

Two further fields (`case_017.dose`, `case_017.allergy`) were held by the gate but were **already
empty** in the raw output, so a console shows nothing there either — which is why 5 held fields
yield only 3 extra displayed values. Stated because the arithmetic would otherwise look wrong.

**And the coded arm's own cost, for symmetry.** `case_040.medication` was routed to
`REVIEW_MODEL_UNSURE`, and the value a console would have shown — `Minoxidil` — is **correct**.
One right answer sent for a second look. That is the trade, and it is cheap at this rate: three
wrong values removed for one correct value questioned.

## Table 3 — what a console cannot do at all

The tables above measure the routine case. This table is the part that decides the architecture,
and every row names the symbol that does the work so it can be checked rather than believed.

<!-- generated: ./.venv/bin/python evals/lowcode_arm.py capabilities -->

| What the coded pipeline does | Where | Why it matters |
| :--- | :--- | :--- |
| Hard-wipe an ungrounded field to null | `src/extract.py:wiped_extraction` | the payload is never a display string |
| Verify a quote is verbatim in the note | `src/extract.py:verify_evidence` | containment, not plausibility |
| Verify the value is supported by its own quote | `src/extract.py:verify_value` | catches a real quote paired with a wrong value |
| Pair a dose number with its unit | `src/extract.py:_check_dose` | 15 mg against 50 mg, by Decimal |
| Flag instruction-like text in the note | `src/extract.py:INJECTION_PATTERNS` | the arm that matters most - see below |
| Force abstention on hidden or look-alike characters | `src/extract.py:scan_input` | refused before any call is billed |
| Separate a safe abstention from an API failure | `src/extract.py:EXIT_BLANKED vs EXIT_UPSTREAM` | exit 1 is not exit 3 |
| Refuse a call that would cross a spend bound | `evals/spend_guard.py:CostGuard.check_before` | checked against worst case |
| Hold a ground truth immutable | `evals/run_ekacare.py:REGISTRY` | SHA-256, 0444, write-once seals |
| Test a delta rather than eyeball it | `evals/metrics.py:mcnemar` | paired, exact, over the fields that changed |
| Keep stdout machine-parsable | `src/extract.py:main` | one JSON document, telemetry to stderr |
| Fail a build when a document drifts from its artefact | `tests/test_guardrails_doc.py` | 15 checks, run in the suite |

## The case that actually decides it

On routine notes the gates buy three wrong values and cost nothing — real, modest, and not on its
own an argument for four weeks of Python.

The argument is the adversarial case, and it is already measured in `guardrails.md` §6.2.1. The
live red-team battery found the model **obeyed 5 of 10 prompt injections**, planting values like
`warfarin 10 mg daily` into fields. With the deterministic tripwire, **0 of those reached a
payload as verified** — every one was downgraded to review with the value retained and flagged.

A console has no tripwire. It has no place to put one. So the `warfarin 10 mg daily` visible on the
third chart in `demo/ehr.html`, flagged red and held back, is what a low-code arm delivers to the
record **as a clean verified value**. That is not a 1.5-point precision delta. That is the whole
difference between the two arms, and it is why the project is code.

The same applies to the 16 homoglyph vectors: a console cannot refuse a note before billing for it,
so every one reaches the model, and §6.2.1 measured that **3 of 16 put a look-alike drug name into
a field marked verified** once the encoding pre-filter is disabled.

## Honest limits

- **One run, 67 notes.** Three named wrong values is a small count, and a different run would give
  a slightly different three. The count is the claim; no rate is inferred from it.
- **The proxy shares the coded arm's prompt.** A console given a weaker prompt would do worse, and
  a console given this prompt does exactly this. Neither is a measurement of the tool.
- **No live console run exists yet.** [`lowcode_pack/`](lowcode_pack/) is the twenty minutes of
  manual work that would turn this proxy into a real arm, and it has not been done.
- **The corpus contains no injections**, so the decisive comparison above is imported from
  §6.2.1's battery rather than measured on gold-v2. The two populations are different and are not
  pooled.
