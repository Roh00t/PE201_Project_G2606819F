# MediExtract — Final Report

**PE6201 Emerging AI Technologies · End-of-Course Project · MSc Enterprise AI, NTU Singapore**

---

## 1. What this changes

Clinical AI has a verification problem, not a fluency problem. A scribe that writes
`metformin 500 mg twice daily` in confident prose has produced something a clinician must either
trust or re-derive by re-reading the transcript — and re-deriving is slower than typing it from
scratch, so a plausible wrong extraction costs more than a blank box. GPs already spend some 157
minutes a day on clerical work; an unguarded model adds an audit loop.

MediExtract changes the unit of output from a *summary* to a **verifiable claim**. Each of four
fields — `medication`, `dose`, `frequency`, `allergy` — arrives with the verbatim words it came
from, and anything unprovable against the source arrives as `null` with a named reason. The
clinician's question stops being "is this true?" and becomes "do these highlighted words say what
this box says?" — a match rather than a search.

That is the thesis, and it is a deterministic-software claim rather than a model claim: the model
proposes, Python decides, and when the perimeter cannot certify a value it destroys it.

## 2. Architecture and persona

**Dr. Aisha, a GP at a Singapore polyclinic**, consulting back to back, closing each chart before
the next patient sits down. She gives a machine-written field roughly **three seconds**; past that
she retypes it. Every decision here is downstream of that budget.

One transcript in. One OpenRouter call to `google/gemini-2.5-flash` at temperature 0, strict
`json_schema`, no tools and no retrieval — there is nothing to retrieve, since the answer is always
inside the note being processed. The reply is stripped of markdown fencing, validated by Pydantic,
then passed to deterministic gates written as plain `if/elif/else`. **No `assert` appears in any
runtime path**: `python -O` strips assertions, so a gate written as one would silently cease to exist
in production. The whole suite runs under `-O` to prove it.

The gates check that the evidence span is a literal substring of the note, that the value is
grounded in its own quote, that a dose's number and unit are paired, and that the note carries no
instruction-like text. Failure is a **hard wipe to `None`** — never a display string like `"BLANK"`,
because the database must receive nulls or validated types only. Exit codes are separated
deliberately (`1` safe abstention, `3` upstream): collapsing them would let every network blip
inflate the measured abstention rate, the class of error that makes an evaluation flatter itself.

Measured: **median 1,293 ms**, p95 1,834 ms, 66 of 67 inside the 3,000 ms budget, at **$0.000617
per note**.

## 3. Metrics: an honest reading

Against sealed `gold-v2` — 67 consults, 268 field decisions, 148 labelled `found`:

| | Result |
| :--- | ---: |
| Recall | **0.7297** CI [0.653, 0.795] |
| Precision | **0.7500** CI [0.673, 0.814] |
| **Silent-failure rate** | **0.2517** |

Two deserve more weight than the headline. **Gate false positives are zero** — no gate ever
destroyed a correct value — and **abstention precision is 1.000**: every field blanked was one whose
ungated value was wrong. The perimeter has never been wrong to refuse.

**The silent-failure rate did not improve, and it is the interesting one.** Of 143 values presented
as verified, 36 were wrong. Attributed to the layer that owns the fix:

| Cause | n |
| :--- | ---: |
| `MED_DIFFERENT_DRUG` — a different drug from the same note | **17** |
| `FREQ_DIFFERENT_REGIMEN` — the regimen of the drug it picked | 8 |
| `DOSE_OTHER_DRUG` — the strength of the drug it picked | 6 |
| `DOSE_GOLD_SILENT` / `FREQ_PHRASE_LENGTH` — label disputes | 5 |

**31 of 36 (86%) are a single defect: drug selection in a multi-drug note** — 17 directly and 14 as
the arithmetic cascade of reporting the dose and schedule of whichever drug was named. **Zero are
owned by the model layer and zero by the gate layer.** Every one of the 36 values genuinely appears
in the note and is quoted correctly, so there is **no hallucination in this set at all**. They are
correctly-copied facts about the wrong drug.

The schema owns this. A single `medication` slot describing a note with a median of three drugs is
wrong whenever it chooses differently from the labeller, and the split is stark: **recall 0.990
(101/102) where the drug choice agrees against 0.196 (9/46) where it does not.** The system is
essentially perfect once it picks the right drug. That conditioned figure sizes a fix and is never
quoted as the system's recall.

## 4. Evaluation: what held, and two controls refused

**Prompt injection — the probabilistic layer failed and the deterministic layer held.** In a live
battery of ten injection patterns the model **obeyed five**, returning values such as
`warfarin 10 mg daily` that no dictation contained. **Zero reached a payload as verified.** The
input scan flagged the phrasing and the grounding gate found no supporting words, so each value was
retained and explicitly disowned as `REVIEW`. That is the project's central result: alignment was not
the control that worked, and a substring check was.

**The comparator separates, and only partly.** Against the committed regex baseline on the same
answer key, pooled recall moves 43.2% → 73.0%: a paired exact McNemar of **52 flips right against 8
wrong, p = 5.2 × 10⁻⁹**. Separated on medication and dose — but **frequency is not** (+11.3pp,
p = 0.180), where rules were already strong. A 14,689-name RxNorm gazetteer added **no recall** over
patterns alone and **cost** precision.

**Two safety controls were specified, measured, and refused.** A proximity gate wiping fields near
attribution or temporal cues caught **0 of 36** silent failures at every window width, because 15 of
its 16 cue words never occur in the corpus; broadened until it fired, it destroyed **9 correct
fields** and still caught nothing, because in a dictated prescription the cue *is* the positive
signal (*"has been taking Dolo 650"* — gold `Dolo` / `650`, correct). An allergy recall check scored
**sensitivity 0 of 4**: its trigger word is the ALL-CAPS header that the stripped corpus removes, so
it matched 20 of 20 intact notes and none of the four misses — circular by construction.

An **LLM-as-judge was built and rejected against a bar written before the code existed**: 0.9467 on
the class gold calls correct, **0.2093** on the class it calls wrong, and a judge answering
"correct" to everything scores **0.8396 against its 0.8284**. It does not beat a rubber stamp. κ and
AC₁ are both reported with their chance terms (0.1967 against 0.7827), because quoting whichever
flattered would be the whole failure mode.

**The hardest discipline was refusing to report unseparated gaps.** A prompt change once went out as
a "4.5-point improvement"; paired, it is 12 flips against 5, **p = 0.14**. A large p-value never
means two systems are equally good — it means the evaluation is too small to tell.

## 5. Where this goes next

The fix for 0.2517 is architectural, not another guardrail: replace the single `medication` slot
with a **bounded array of up to twelve medication objects**, each carrying its own dose, frequency
and evidence span. `evals/probes/probe_v4_schema.py` has confirmed the provider accepts that schema
alongside `require_parameters: true`, first attempt, for $0.000635, so the engineering risk is
retired. What gates it is evaluation, not code: scoring a list needs a **gold-v3** with multi-drug
containment labels, which breaks comparability with every sealed run to date. That is a labelling
cost, named here rather than deferred quietly.

The honest limits: 67 consults cannot resolve effects under roughly eight points, and it is one
corpus of one language variety. Verbatim grounding also means a transcription typo yields a blank
rather than a correction — accepted on purpose, because fuzzy matching would raise recall and
dissolve the only guarantee this system makes.
