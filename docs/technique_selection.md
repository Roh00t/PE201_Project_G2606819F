# Which AI technology, and why — measured

ILO 1 asks for a choice between discriminative ML, foundation models,
retrieval-augmented systems, agentic systems, multimodal architectures and physical AI, and
for the choice to be justified for the problem class. This is that justification, with the
alternatives measured rather than dismissed.

**The decision.** A prompted foundation model with schema-constrained structured output, wrapped
in deterministic Python verification. One call, no tools, no retrieval, no loop — the cheapest
rung of the ladder that does the job, with the safety-critical checks kept out of the model.

---

## 1. The measurement that decides it

All arms score the same 67 sealed gold cases through the same gates and the same
pre-registered matching rules (`evals/scoring.py`). Reproduce with `evals/baseline.py` and
`evals/score_arm.py`.

| Arm | n | recall | precision | silent-failure | medication | dose | frequency |
| :--- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| **foundation model** (`google/gemini-2.5-flash`) | 67 | **0.600** | 0.650 | 0.350 | 0.571 | 0.543 | 0.679 |
| regex, pattern-only | 67 | 0.381 | 0.527 | 0.473 | 0.321 | 0.196 | 0.604 |
| regex, pattern-only, held-out 47 | 47 | 0.353 | 0.483 | 0.517 | 0.233 | 0.200 | 0.610 |
| regex + RxNorm gazetteer (14,689 names) | 67 | 0.381 | 0.488 | 0.512 | 0.339 | 0.196 | 0.585 |
| majority class ("everything is not_stated") | 67 | **0.000** | — | — | — | — | — |

Four things fall out of this table, and three of them were not what the proposal predicted.

**The model earns its cost, by 22 recall points.** 0.600 against 0.381 is a 1.6× improvement on
the best rule-based arm, and the honest comparison is the held-out one: the regex patterns were
tuned on a seeded 20-case subsample and score 0.353 on the other 47, so the pooled 0.381 is
mildly optimistic and disclosed as such.

**But not uniformly, and frequency is where it barely earns anything.** Regex reaches 0.604 on
frequency against the model's 0.679 — a 7.5-point gap. Dictated frequency is shorthand from a
closed set (`TDS`, `1-0-1`, "twice a day"), which is exactly what a pattern matcher is for. The
proposal predicted this ("shorthand like 'po qd' is trivial for regex") and it is now measured.
Where the model actually earns its keep is **dose, 0.543 against 0.196 — 2.8×** — and
medication, 0.571 against 0.321.

**The gazetteer promised in section 4 does not work, for an instructive reason.** 14,689
ingredient names fetched from RxNav (`data/gazetteer/fetch_rxnorm.py`, retrieved 2026-09-20)
move pooled recall by **zero** — 0.381 either way. They can name only **6 of 56** gold
medications, 10.7%, because RxNorm is a US vocabulary of *ingredients* and this corpus is
Indian dictation full of local brands: Dolo, Moxclav, Cepodem, Acecloran MR, Penta DSR. The
gazetteer even *costs* precision, 0.527 → 0.488, because when it fires it fires on the wrong
thing. This is a data-readiness lesson, not an implementation bug: a lookup table is only as
good as its coverage of the market the data comes from, and the promised external source did
not cover this one. It is also why the gazetteer was fetched rather than typed — a drug list
written from memory by someone who has already read the gold labels would have hidden this.

**The baseline cannot abstain, and that is the sharpest difference of all.** Its abstention rate
is 0.0% by construction: a pattern either matches or it does not, and there is no "unsure" for
it to report. Its silent-failure rate is 0.473 against the model's 0.350. A rule-based extractor
is also structurally incapable of failing the grounding gate — its evidence is a slice of the
note by construction — so the entire abstention apparatus measures nothing on it. The guardrail
layer only has content against a generative extractor, which is a statement about the scope of
the guardrails rather than a flaw in either arm.

## 2. Each technology class, and the verdict

| Class | Verdict | The evidence for it |
| :--- | :--- | :--- |
| **Rules and lookup tables** | **Rejected as the primary extractor; kept for the gates** | 0.381 against 0.600, and 0.0% abstention. But rules keep the safety-critical arithmetic: the dose number-and-unit check, span containment and the frequency equivalence table are all deterministic Python, because a wrong dose is expensive and `if` statements are checkable. |
| **Discriminative ML** (clinical NER, span tagging) | **Not built, and not claimed** | It needs labelled spans at a scale this project does not have — 268 labels in total, which is an evaluation set, not a training set. The published comparison for the adjacent task is cited in `project_proposal.md` §4 (purpose-built biomedical resolvers 82.7% top-3 against GPT-4 at 8.9% on terminology coding); those are other people's numbers for a different task, and they are the reason terminology coding is left to a dedicated downstream service rather than attempted here. |
| **Foundation model, prompted, structured output** | **Chosen** | 0.600 recall, 1,216 ms median, $0.000594 per note, strict JSON schema honoured, zero retries across 67 calls. It is the cheapest rung that handles unseen brand names and free-form dictation. |
| **Retrieval-augmented (RAG)** | **Rejected** | There is nothing to retrieve. The answer is always inside the note being processed, so retrieval could only add a failure mode — a wrong chunk — for zero possible recall. Every field must be grounded in *this* note, not in a corpus. |
| **Agentic** | **Rejected, and the rejection is load-bearing** | One call, no tools, no memory, no downstream actions. This is not only a cost decision: OWASP's component-versus-actor boundary means the LLM Top 10 applies to this system and the Agentic Top 10 does not, which is what `guardrails.md` is built against. Adding a tool would change the threat model, not just the bill. |
| **Multimodal** | **Out of scope, and it is the upstream** | The real input is dictated audio; this pipeline starts from a transcript, as `project_proposal.md` §2 states. §3 below is why that boundary matters more than it sounds. |
| **Physical / embodied AI** | **Not applicable** | No actuation, no perception loop, no embodiment. |

## 3. The limitation the multimodal boundary hides

`case_045` dictates **"Dolo 450"**. Dolo is sold as 650 and 500; 450 is not a strength it comes
in, so the transcript almost certainly records a speech-recognition error. Both the model and
the gold label captured it faithfully, because the phrase really is in the text.

That is the honest cost of a verbatim-evidence design: **it propagates an upstream transcription
error rather than correcting it.** It cannot do otherwise without breaking its own contract —
the physician's dictation is read-only, and a system that silently "corrects" 450 to 650 is
guessing at a dose, which is precisely the failure the whole design exists to prevent.

Two consequences worth stating out loud:

1. The system's accuracy ceiling is the transcriber's accuracy, and this project never measured
   that because it starts from text. A deployment would have to.
2. The right next guardrail is **not a bigger model**. It is a deterministic plausibility check
   against a formulary — does this product come in this strength? — which would have flagged
   "Dolo 450" for review without inventing a value. That is a lookup table, the very technology
   §2 rejected as a primary extractor, doing what lookup tables are good at.

## 4. The unclimbed rungs, and why each stays unclimbed

- **Terminology coding (RxNorm/SNOMED mapping).** Out of scope in §2 of the proposal, and §1
  above is the evidence for leaving it out: RxNorm cannot even name 89% of this corpus's drugs,
  so mapping to it is a project of its own.
- **Fine-tuning.** 268 hand labels is an evaluation set. And the diagnosis says the failures are
  not capability failures — 2 of 65 are the model failing to read something — so there is
  nothing for a fine-tune to fix.
- **A larger model.** Being measured (three models, same 67 cases, same rules). The prior from
  the diagnosis is that it buys little: where the model and the labeller name the same drug,
  dose is right 24 of 26 and frequency 30 of 30, so the binding constraint is a single-slot
  schema, not model capability.
- **Self-hosting a small model.** `docs/cost_to_serve.md` §6: break-even is roughly 1,700
  physicians, and inference is 0.05% of the per-note cost, so cost is the wrong reason. Data
  residency would be the right one.
- **Chain-of-thought or self-consistency sampling.** Both multiply token spend for the same
  task, and `reasoning: {effort: "none"}` is set deliberately: with thinking disabled the token
  order *is* the reasoning order, which is what lets the schema force a verbatim quote out of
  the model before it commits to a value. Sampling would also break the determinism the gates
  and the cache depend on.

## 5. Where the line between model and rules sits, restated

The model does one thing: semantic translation from dictation to candidate fields, with a quote.
Everything that can be checked is checked in Python — span containment, value grounding, the
dose number and unit, frequency equivalence, input scanning, the spend ceiling. That split is
not an aesthetic preference. It is the measured difference between a component with a 0.350
silent-failure rate and a component whose three abstentions were all defensible on inspection,
and it is why arithmetic and safety decisions stay on the deterministic side of the line.
