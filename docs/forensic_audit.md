# Forensic audit: data lineage and evaluation rigour

**Scope.** Do MediExtract's actual data-engineering and evaluation practices match the five claims
made to a peer about dataset curation, ground-truth creation, manual labelling, adversarial
testing, and evaluation gates?

**Method.** Every grade below rests on a file, a provenance record, a commit, or a command re-run
during the audit. Where a claim is graded down, the specific artefact that contradicts it is
named. Grading: **Pass** = the claim is fully backed; **Partial** = substantive evidence exists but
the claim overstates or misdescribes it; **Fail** = no evidence.

**Audited at** commit `faf0041` plus the uncommitted judge-calibration work, 24 September 2026.

---

## 1. Executive verdict

**"Walk the talk" score: 75% at audit; 87.5% after the remediation in §5** — Pass 100 / Partial 50, equally weighted over the four dimensions
(100 + 50 + 50 + 100) / 4.

**Bottom line.** The engineering is real, and in two of the four dimensions it materially exceeds
what was claimed — the provenance records and the self-verifying evaluation harness are stronger
than the description given to the peer. Both markdowns are **narrative failures rather than
missing work**: the labelling process described (LLM first, human second) is not the process the
repository shows for either dataset, and the "near-distribution" half of the claimed adversarial
testing does not exist anywhere in the codebase. Nothing found suggests a number was overstated;
the discrepancies are about *how the work was characterised*, not whether it was done.

---

## 2. Evidence scorecard

| Audit dimension | Claimed practice | Codebase evidence found | Grade |
| :--- | :--- | :--- | :--- |
| **A. Dataset customisation** | Native Eka Care replaced with a 4-slot schema | `src/extract.py` — `ExtractedField`, `ClinicalExtraction`, `response_json_schema()` (`strict: true`, required keys exactly the four slots, each with `value`/`evidence`/`status`); `GoldField`/`GoldCase`/`GoldSet`; `evals/label_gold_v1.py` hand-labelling CLI running the **production gate** during labelling; `gold_v1.json` provenance recording dataset, revision, split, the 156→67 Latin-script filter with all 89 excluded row indices, and a researcher-**exposure** record | **Pass** (exceeds) |
| **B. Gold anchor & sealing** | Multi-pass labelling (LLM then human), 18 fixes, sealed `gold-v2` | `evals/label_gold_v2.py` — 18 corrections (16 `rule`, 2 `review` signed by `rohit`), `derived_from` carrying gold-v1's SHA-256, write-once seal, `--labeller` and per-correction `--confirm` required; all four sealed files hash-matching at `0444`; registry refuses a file not declaring its own `schema_version`; 30 tests in `tests/test_gold_versions.py`. **But the labelling process is not the one claimed** — see §3.1 | **Partial** |
| **C. Adversarial testing** *(re-graded 25 Sep 2026)* | Near- and far-distribution testing, header leakage | `evals/label_allergy_v1.py:strip_headers` + two sealed paired variants; live paired runs `leak-allergy-v1` / `-stripped`; allergy recall 0.900 → 0.650, McNemar exact p = 0.0625; `guardrails.md` §6.6 and the precision-only structural finding; 16 homoglyph/encoding attack strings and injection tripwires in `tests/test_guardrails.py`; a 14-category red-team matrix in §6.2. near-distribution arm added after this audit: `evals/paraphrase.py`, sealed `paraphrase-v1`, paired McNemar p = 0.6072 — see §5 | **Pass** (was Partial) |
| **D. Evaluation verification** | Paired statistical tests, doc-test harness | `evals/metrics.py` — `mcnemar` (exact binomial on discordant pairs), `compare`, `compare_golds` (pairs only fields `found` in both; withdrawn/added reported separately); `evals/score_arm.py` writes `scores-vs-<goldstem>.json` so a cross-gold measurement cannot overwrite a run's own; the three-way gold-v2 split; `tests/test_guardrails_doc.py` (15 checks: AST identity with docstrings stripped, `ast.Assert` walk, symbol resolution, anchor resolution, figures-match-artefacts); 350 tests green under `python -O` | **Pass** (exceeds) |

---

## 3. Detailed discrepancies

### 3.1 The claimed labelling process did not happen — in either direction (Dimension B)

The claim was *"a multi-pass process (initial LLM labeling followed by human review/corrections)"*.

**gold-v1 was hand-labelled from scratch, with no machine pass.**

- `evals/label_gold_v1.py` is headed *"Hand-labelling infrastructure for the gold-v1 evaluation
  set"* and its documented workflow is `profile → template → label --labeller rohit → validate
  --seal`.
- It contains **zero** model calls: `grep -c "call_model\|openai\|chat.completions"` returns 0.
- The commit that introduced the file says so outright: `1dfacc9 gold-v1: 67 hand-labelled Eka
  Care cases`.

**The allergy set is the mirror image: machine-labelled with no human review.**
`allergy_v1.json` carries `provenance.sealed_by: "claude-opus-5"`, and `guardrails.md` §6.6's
honesty block already concedes this is "a different model family from the one under test, which
is the prescribed remedy but not a clinician's hand."

So "LLM first, human second" describes **neither** dataset. One is human-only, the other is
machine-only. The claim undersells gold-v1 (human-first labelling with the production gate live
during labelling is the stronger methodology) and oversells the allergy set (no human reviewed it).

**Two lesser gaps in the same dimension.** `gold-v1` carries a git tag; **`gold-v2` does not** —
its immutability currently rests on the SHA-256 file and the `0444` mode, not on a tag. And
`gold_v2.json`, `allergy_v1.json` and `allergy_v1_stripped.json` were all committed under
`9cd0eae`, whose message describes an unrelated rejected architecture profile. The lineage is
recoverable from the provenance blocks, not from the commit history.

### 3.2 "Near-distribution variation" testing does not exist (Dimension C)

The claim was *"near-distribution variations and far-distribution adversarial perturbations"*.

The far-distribution half is real and well executed: header stripping is a genuine paired
perturbation study, the two variants are proven to differ only in note text
(`test_the_two_variants_are_the_same_cases_with_the_same_labels`), the comparison is paired and
McNemar-tested, and it produced the project's sharpest security finding.

The near-distribution half has no implementation. There is no paraphrase arm, no typo or
ASR-noise injection, no abbreviation-expansion arm, and no word-order variation. A search for
`paraphrase|typo|perturb` across `evals/`, `tests/` and `src/` returns nothing. For a system whose
deployment input is *dictated* clinical speech, near-distribution robustness is the more
operationally relevant axis of the two, and it is the one not covered.

### 3.3 The offline/live boundary in the red-team set is not visible from the claim (Dimension C)

The 16 homoglyph strings and the injection tripwires are exercised as **unit tests against the
deterministic gates** — `scan_input` and `apply_gates` — never through a live model call. They
demonstrate that the *gate* behaves, which is a real and worthwhile result, but it is a different
claim from "the system resists these attacks". `guardrails.md` §6.2 is honest about this
internally, marking model-dependent rows "measured" and budgeting $0.50 for the live run, but that
live red-team run is **not on disk**. Anyone reading only the claim would assume it had been done.

### 3.4 "Quality verification gates" is true, and one-sided (Dimension C/E)

Claim 5 — precision controls and evaluation harnesses rather than blind trust — is backed. The
qualification is one the project discovered itself and states at `guardrails.md:2331`:

> **Every gate in this system is a precision control. None is a recall control.**

Every gate catches an *invented* value; none catches a *missed* one. A reader of claim 5 would
reasonably infer two-sided quality control, and the system has one side.

A second data point arrived during this audit. S-14, the LLM-as-a-judge layer, was built and
calibrated against sealed gold-v2. Its pre-registered adoption gate — written in `guardrails.md`
§6.1 *before* the code existed — required 0.9 agreement **on both classes**. Measured: **0.9467**
where gold says correct, **0.2093** where gold says wrong. Worse, a judge answering "correct" to
everything would score **0.8396** against the real judge's **0.8284**, so it does not beat a rubber
stamp. The gate refused it and it was not adopted (`guardrails.md` §6.8). That is claim 5 working
exactly as described — a quality control that rejected a component the project wanted — and it is
the strongest single piece of evidence in this audit that the gates are real rather than
decorative.

### 3.5 What the audit did *not* find

No fabricated numbers. Every headline figure was re-derived from artefacts during this audit and
matched: the three-way gold-v2 split (0.6452 → 0.7230 → 0.7297), the leakage result (0.900 →
0.650), and all four sealed hashes. Falsifying one figure in `guardrails.md`'s measured block was
tested during the audit and turned `tests/test_guardrails_doc.py` red with the exact artefact
diff. Statistical claims are paired where pairing applies, p-values are reported with deltas, and
the document distinguishes a defect count from a rate.

---

## 4. Forensic conclusion

**The data lineage and evaluation architecture meet institutional-grade standards; the description
of them does not.**

On lineage, the repository is stronger than the claim. Recording which three source rows were read
before the sample was drawn, how much of each, and which prompt sentence it may have influenced, is
a pre-registration of researcher degrees of freedom that most production pipelines never attempt.
Write-once sealing with per-correction human signatures, a registry that refuses an undeclared
schema version, and 0444 files whose hashes were re-verified during this audit constitute a real
chain of custody.

On evaluation, the harness is the strongest artefact in the project. Paired tests where pairing
applies, a comparator printed beside every figure, cross-gold artefacts that cannot overwrite each
other, and a specification that fails its own test suite when a quoted number drifts from the
artefact it names — that is a system built by someone who expects to be wrong and wants to find out
early.

Two corrections are required before the claims can be repeated:

1. **Say what the labelling actually was.** gold-v1 was hand-labelled with the production gate
   live; the allergy set was machine-labelled by a different model family and has had no human
   review. Neither is "LLM first, human second", and the true description of gold-v1 is the more
   impressive one.
2. **Drop "near-distribution" or build it.** One paraphrase or dictation-noise arm over the
   existing 67 cases would convert this from an overstatement into a Pass, and it is the axis that
   matters most for dictated input.

Two gaps are worth closing on their own merits: the live red-team run that §6.2 budgets for but has
not executed, and a `gold-v2` git tag to match `gold-v1`.


---

## 5. Remediation log

### 5.1 Dimension C closed — the near-distribution arm was built (25 September 2026)

§3.2 found that the "near-distribution variations" half of the adversarial-testing claim had no
implementation. It now does.

`evals/paraphrase.py` re-dictates all 67 consults with six named transforms — dosage-form cue
dropped, meal timing rephrased, plan verbs rewritten, hesitation fillers, punctuation drift,
capitalisation drift — each acting **strictly outside the spans carrying a gold value**. The value
labels are therefore byte-identical to gold-v2 and the comparison is paired field for field, which
`tests/test_paraphrase.py:PairingInvariant` checks rather than assumes. Sealed as
`paraphrase-v1` (`fa3fa8bf…5642b`) at `0444`, deterministic at seed 42, with no model anywhere in
the label path. 27 tests; 66 of 67 cases changed.

**Result: pooled recall 73.0% → 75.0%, 9 flips right against 6 wrong, McNemar exact p = 0.6072.**
A null, reported as a null.

It also upgrades the value of the §6.6 header finding. Surface re-dictation costs nothing
measurable while header removal costs 25 points of allergy recall, so the dependency localises to
the ALL-CAPS header specifically rather than to document formatting in general. Neither
measurement supports that claim alone.

**Re-grade: Dimension C Partial → Pass.** Revised score (100 + 50 + 100 + 100) / 4 = **87.5%**.

### 5.2 Still open

| Gap | Dimension | Status |
| :--- | :--- | :--- |
| The labelling narrative — gold-v1 was hand-labelled, the allergy set machine-labelled; "LLM then human" describes neither | B | **Open** — a wording correction, not an engineering task |
| `gold-v2` git tag | B | **Closed** — annotated tag at `9cd0eae`, pushed to `origin`, and `git show gold-v2:data/gold_labels/gold_v2.json` hashes to `d5e90f5a…a959` |
| `gold-v1` tag is local-only | B | **Closed** 25 Sep 2026 — pushed to `origin`; all three tags now verify, with `git show <tag>:…` matching each committed `.sha256` |
| The live red-team run §6.2 budgets ($0.50) has not been executed; the 16 homoglyph strings and injection tripwires are exercised against the gates offline only | C | **Open** |
