# MediExtract — executive summary and governance audit

PE6201 End-of-Course Project · `google/gemini-2.5-flash` via OpenRouter · prompt fingerprint
`a92d2abcb2af4294` · 377 tests green under both standard and `python -O` execution.

---

## 1. What was built, and what it is for

Dr. Aisha, a general practitioner in a Singapore polyclinic, dictates a consult in ninety seconds
and then retypes medication, dose and frequency into separate EHR fields to close the chart.
Ambient scribing tools already produce fluent prose; none of them show her **where a value came
from**, and she will not sign an AI-generated dose she cannot verify in **under three seconds**.

MediExtract turns one dictated note into four schema-valid fields — `medication`, `dose`,
`frequency`, `allergy` — each carrying the verbatim span it was extracted from, and **blanks any
field it cannot ground**. The deliverable is not a summary. It is a value with a highlight under
it, or nothing at all.

| Measure | Result | Comparator |
| :--- | ---: | :--- |
| Pooled recall, sealed `gold-v2`, n = 67 | **0.7297** | regex + 14,689-name RxNorm gazetteer: **0.381** |
| | | majority class (all `not_stated`): **0.000**, yet agrees with gold 113/268 |
| Precision | 0.7500 | — |
| Silent-failure rate | 0.2517 | the number that matters clinically: wrong *and* presented as verified |
| Median latency | 1,293 ms | 66/67 inside the 3,000 ms persona budget |
| Cost per note | **$0.000617** | cost per *correct field* $0.000383 — the honest unit |

---

## 2. Architectural trade-offs, each with the evidence that decided it

Documented in full at [`technique_selection.md`](technique_selection.md) §2.

| Decision | Chosen | Rejected | Why, measured |
| :--- | :--- | :--- | :--- |
| **Extractor** | Foundation model, prompted, strict structured output | Rules/lookup as the primary extractor | 0.730 against 0.381 for the committed regex+gazetteer baseline. Rules were **kept for the gates**, where deterministic `if` statements are checkable and a wrong dose is expensive. |
| **Retrieval** | None | RAG | There is nothing to retrieve: the answer is always inside the note being processed. Retrieval could only add a failure mode — a wrong chunk — for zero possible recall gain, plus non-deterministic latency against a 3-second budget. |
| **Orchestration** | One call, no tools, no memory | Multi-agent / agentic framework | Cost and latency, but principally **threat model**: OWASP's component-versus-actor boundary means the LLM Top 10 applies and the Agentic Top 10 does not. Adding one tool would change what this system *is*, not just its bill. `guardrails.md` §2 carries the crosswalk and the threshold at which each agentic control activates. |
| **Framework** | None — one file | LangChain / LlamaIndex | Every request parameter is line-by-line auditable, which is what made the cost model checkable against the provider to **0.005% drift**. |
| **Hosting** | Rented API (OpenRouter) | Self-hosted small open-weight model (e.g. Qwen) | **Reasoned, not measured.** Latency, maintenance and data-residency were the stated grounds and this remains a *stretch goal* in `project_proposal.md` §5 — no local benchmark was run, and this report does not claim one. `cost_to_serve.md` §5 puts the crossover at roughly **1,700 physicians** before self-hosting pays back. |

**The decision the economics actually force.** At SGD 100/h clinician time, a 5-percentage-point
accuracy gain is worth **561×** a token-price saving. Token price is not a decision variable in
this workflow; accuracy is, by three orders of magnitude. Moving from one medication slot to a
medication *list* is projected to save ~$3,600 per physician-year against a **$3.57** annual
inference bill — one schema change worth a thousand times the entire model spend.

---

## 3. OWASP LLM Top 10 (2026) mitigation mapping

The 2026 numbering is used throughout; `guardrails.md` §1.2 carries the full 2025↔2026 crosswalk.
Status vocabulary is strict: **IMPLEMENTED** names a path, a symbol and a test.

| 2026 risk | Exposure here | Mitigation | Status |
| :--- | :--- | :--- | :--- |
| **LLM01** Prompt Injection | High — the note is untrusted dictation | Encoding anomalies force abstention **with no model call**; injection tripwire → REVIEW; `<note>` fencing; strict schema; no tools | IMPLEMENTED — 16 attack vectors abstain, 11 legitimate notations do not, 0/156 false positives |
| **LLM02** Sensitive Information Disclosure | High for PHI; low on this public de-identified corpus | Secrets out of git, placeholder-key guard, upstream error text never echoed, totals-only ledger, `data_collection: deny` (proven to route) | IMPLEMENTED; redaction (S-01) SPECIFIED |
| **LLM03** Excessive Agency | **Structurally eliminated** | No tools, no tool choice, no memory, no write-back — pinned by `test_the_model_is_given_no_tools` | IMPLEMENTED |
| **LLM04** Supply Chain | Moderate | Pinned `requirements.txt`, single SDK, provenance recorded per call (served model, provider, response id) | IMPLEMENTED; hash-pinned lockfile (S-05) SPECIFIED |
| **LLM05** Data and Model Poisoning | Moderate — labels are the attack surface | Five hash-sealed corpora at `0444`, write-once seals, per-correction human signatures, registry refusing an undeclared `schema_version` | IMPLEMENTED |
| **LLM06** Unbounded Consumption | Moderate | Two bounds: `$8` project ceiling and a per-run cap checked against **worst case before every call**; `max_tokens` 1024; 1 retry; 15 s timeout; breach exits 5 | IMPLEMENTED |
| **LLM07** Misinformation | **The core risk** | Verbatim span containment, value-token grounding, dose number/unit pairing via `Decimal`, frequency equivalence — failure hard-wipes to `None` | IMPLEMENTED. LLM-judge layer (S-14) built, calibrated and **refused** — §4 |
| **LLM08** Hidden Context Exposure | Low | System prompt carries no secrets; echoed prompt text in a value is caught by the grounding gate | IMPLEMENTED |
| **LLM09** Vector and Embedding Weaknesses | **Not applicable** | No retrieval, no embeddings, no vector store — and a denylist ensures none is added silently | N/A, by architecture |
| **LLM10** Improper Output Handling | High — output feeds a database | stdout is 100% machine-parsable JSON, all telemetry to stderr; wiped fields are `null`, never a display string; every CSV cell passes `csv_safe` | IMPLEMENTED |

**The structural gap, found by measurement rather than by review.** Stripping ALL-CAPS section
headers drops allergy recall **0.900 → 0.650**. Enumerating all thirty gates afterwards showed why
none of them fired: *every gate in this system is a precision control; none is a recall control.*
They catch an invented value and never a missed one. A near-distribution arm then localised the
dependency — re-dictating the same consults around byte-identical labels moved recall 0.730 →
0.750, **McNemar p = 0.6072**, a null. The crutch is the header specifically, not formatting in
general.

---

## 4. Governance: two gates that refused something

Controls are only real if one has ever said no.

**The LLM judge (S-14) was refused by a criterion written before it existed.** `guardrails.md` §6.1
required 0.9 agreement with the human labels **on both classes** before adoption. Built with a
different model family (`openai/gpt-5-mini`, so it could not agree with itself), calibrated over
all 268 field decisions: **0.9467** on the class gold calls correct, **0.2093** on the class gold
calls wrong. A judge answering "correct" to everything scores **0.8396**; the real judge scored
**0.8284** — it does not beat a rubber stamp. Not adopted; hand labelling still stands behind
every prompt change.

**The paired-statistics rule caught a claim I had already made.** An 8.5-point recall move looked
like an improvement. Decomposed: **7.8 points are the corrected labels (McNemar p = 0.008)** and
0.7 points are the system (p = 1.000, not separated). The unpaired Fisher test on those same eight
flips returns **p = 0.373** and finds nothing — same data, opposite conclusion, because Fisher
discards the fact that both runs saw identical notes.

**Self-verifying CI.** `tests/test_guardrails_doc.py` parses `guardrails.md` with Python's `ast`
module: every snippet must parse, no snippet may contain an `ast.Assert` node, every cited
`file:symbol` must resolve against a static AST scan of the real source, and **every quoted metric
must equal the artefact it names**. Falsifying one figure was tested during the audit and turns the
suite red naming the disagreeing file. The whole suite runs twice, the second time under
`python -O`, which strips assertions — the reason no runtime gate may be one.

---

## 5. Financial ledger and unit economics

Recorded per call by `evals/spend_guard.py`: tokens including cached, list-price estimate against
the provider's own charge, latency, attempts, endpoint, provider, model requested versus served.

| Line | Amount | Basis |
| :--- | ---: | :--- |
| Input price | $0.30 / Mtok | OpenRouter list, `gemini-2.5-flash`, read 2026-09-18 |
| Output price | $2.50 / Mtok | same |
| Tokens per note | 863 in / 143 out | measured median, 67-case run |
| **Cost per note** | **$0.000617** | measured, not modelled |
| **Cost per *correct* field** | **$0.000383** | the honest unit — a system that fails cheaply looks cheap on the first figure only |
| **Per physician-year** | **$3.58** | $0.000617 × 24 notes/day × 242 working days. `cost_to_serve.md` records $3.57 from the previous run at $0.000615/note; the gap is 0.3% run-to-run token variation, not a revised figure. |
| Price-model drift | **+0.005%** | list-price prediction against the provider's charge across all calls |
| Project spend to date | $0.2645 of the **$8** ceiling | 418 calls, all models |

> **Two figures that are routinely confused, and are not the same.** `$8` is the *project's*
> lifetime spend ceiling enforced in `extract.SpendLedger`. `$3.57` is the *per-physician-year*
> inference cost. Evidence spans roughly double output tokens — about **$1.90 per physician-year**
> — which is the explicit cash price of the safety property this system exists for.

**Bounded twice, before the money moves.** The per-run cap is checked against worst case *before*
each call, because a loop inside one run would stay under the project ceiling while consuming it.
Enforcement and measurement are separate modules on purpose: `spend_guard.py` can refuse a call,
`metrics.py` only reads finished artefacts, so a bug in scoring cannot silence a refusal.

---

## 6. Limitations, stated plainly

- **n = 67.** Wilson intervals on every rate, a paired test on every delta, and a large p-value
  reported as *the evaluation is too small to tell* — never as equivalence.
- **Labels are the author's**, not a clinician's. The allergy corpus was machine-labelled by a
  different model family; no clinician has reviewed either.
- **The leakage benchmark is n = 20** and sits at the exact power floor (2/2⁵ = 0.0625). It is
  reported as a defect *count* (4 of 5 flips are the model going silent), not a separated rate.
- **The single medication slot** explains most of the residual error: where labeller and model name
  the same drug, dose is right 24/26 and frequency 30/30. That is a schema limitation, not a model
  one, and it is the highest-value next change.
- **No local-model benchmark** was run; the hosting decision is reasoned, not measured.
- **The live red-team battery** budgeted in `guardrails.md` §6.2 has not been executed; the 16
  homoglyph vectors and injection tripwires are exercised against the deterministic gates offline.
