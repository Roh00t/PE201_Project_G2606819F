# MediExtract — recorded demonstration, shooting script

Target five minutes. Audience is mixed: the marker is technical, the framing has to land for
someone who is not. Criterion 4 (25%) rewards a working demonstration **and** limitations
stated out loud, so the last minute is not padding — it is scored.

Every command below has been run and produces the output described. Numbers come from run
`20260920T065552Z-gemini-2.5-flash` and from `evals/diagnose.py`.

---

## Before you record

- [ ] **Record the live call first, as its own clip**, before anything else. It is the only
      beat that depends on the network, a rate limit and a provider staying up. Everything
      else is offline and can be reshot freely.
- [ ] `export OPENROUTER_API_KEY=...` in a shell you opened **before** you start recording,
      and never `cat .env`, `env`, or `echo $OPENROUTER_API_KEY` on camera.
- [ ] Terminal: ~100 columns, font large enough to read at 720p, light theme, prompt shortened
      to something neutral (no hostname, no full path).
- [ ] Rehearse once end to end with `--mock` so the timing is known.
- [ ] Fallback if the live call fails on the day: use the `--mock` beat instead and **say so
      on camera** — "this is a canned response so the demonstration is reproducible". The
      output shape is identical. Never present a mock as a live call.
- [ ] Have `demo/review.html` already generated and open in a second window.

```bash
./.venv/bin/python demo/build_review.py
```

---

## 0:00 – 0:35 · The problem, and who has it

**On screen:** you, or a single title card.

Family-medicine physicians spend 5.9 hours of an 11.4-hour day in the EHR, 157 minutes of it
on clerical work, plus 86 minutes of after-hours charting. Dictating the consult is fast.
Retyping that dictation into discrete fields is the slow part, and it is the part a machine
can do.

Dr Aisha is a family physician at a Singapore polyclinic, 24 patients a day. It is 21:40 and
she is on her ninth unfinished chart. She knows medicine cold. What she does not know is
whether a machine's suggestion can be trusted at a glance — and she will not accept a field
she cannot verify in under three seconds.

> Say the constraint out loud, because the whole design follows from it: **a plausible wrong
> dose costs far more than an empty box.**

## 0:35 – 1:05 · What it does, and the one design bet

MediExtract turns one dictated note into four fields — medication, dose, frequency, allergy —
and puts the physician's own words beside each one. If it cannot find the words that justify a
value, it leaves the field blank instead of guessing.

The bet: **the evidence is the product, not the value.** The model is asked for a verbatim
quote first and the value second, and a deterministic Python check confirms the quote really
is in the dictation. That check is not a prompt instruction the model can talk itself out of —
it is `if`/`else` in a file, and the field is wiped when it fails.

## 1:05 – 2:00 · Live: one note in, one JSON out

**On screen:** terminal, split so `stdout` and the report are both visible.

```bash
./.venv/bin/python src/extract.py --note gold/case_001.txt --provider-data-collection allow
```

Point at three things and no more:

1. **`stdout` is pure JSON** — nothing else, so it can be piped straight into a database. The
   human-readable table went to `stderr`. That separation is why a rogue log line cannot
   corrupt an ingestion pipeline.
2. **The four fields, each with its quote**, and `VERIFIED` beside each one.
3. **The footer: milliseconds and dollars.** Say the measured numbers: median **1,213 ms**,
   95th percentile 1,600 ms, at **$0.000615 per note**. Be exact about the tail: in the current
   run 66 of 67 notes finished inside Dr Aisha's three-second budget and one took 12 seconds.
   The first run's worst case was 2,442 ms and all 67 were inside it. A tail like that is a
   queueing problem to solve, not a number to hide.

> If you want the pipe visible: `... | python -m json.tool | head -20`.

## 2:00 – 2:45 · What Dr Aisha actually sees

**On screen:** `demo/review.html` in the browser. Pick `case_001`, then `case_016`.

The dictation is on top with each quote highlighted in its field's colour; the table below is
exactly what the database would receive. Click a case with a held field — the value shows as
**— blank —**, with the reason in plain words and what she should do about it.

Two sentences worth saying here:

- The page has no JavaScript and declares `default-src 'none'`, so it cannot reach the network.
  A screen that renders clinical text is an output-handling surface, and this corpus contains
  literal `<PII>` placeholders — escaped, so they appear as text rather than vanishing.
- A blank is a result, not a gap.

## 2:45 – 3:20 · The guardrail, on camera

**On screen:** terminal.

```bash
./.venv/bin/python src/extract.py --note demo/notes/homoglyph.txt
```

This note contains one Cyrillic character that looks exactly like a Latin `e` inside
"metformin". Watch what happens:

- all four fields blank, `BLANK (Abstained: Hidden or look-alike characters in note)`
- `"called": false` — **the model is never invoked**
- `billed_usd: 0.0` — the attack costs nothing
- exit code 1, which means "safely abstained", never the code a crash returns

Say the rule: the physician's dictation is read-only. The system does **not** quietly strip the
character to make its own regex pass, because silently editing a clinical record to make a
check succeed is worse than refusing.

## 3:20 – 4:20 · The numbers, honestly

**On screen:** the scores table, or a slide with the attribution.

> The one rule for this section: the headline number is 0.600. Say it first, and never lead
> with the conditioned figure.

Recall against the sealed 67-case gold set is **0.600** (95% CI 0.521–0.674) against a 0.85
target. Then explain why, because the explanation is the finding:

I attributed all 65 failing field decisions. **Two** are the model failing to read something.
The rest is the measurement instrument:

- the field rules **contradict each other** — one says `Dolo 650` is a medication name, the
  other says the 650 is the dose; that is 8 fields
- **16 of 155 gold labels break a rule the gold set itself states** — my own labelling
- one medication slot cannot represent a note with a **median of three drugs**; "the single
  most significant medication" is not a rule two readers reproduce — the model and I agreed
  40 times out of 56

Then, with the conditioning said out loud: *among the 51 notes where the model and I named the
same drug, dose is right 24 of 26 and frequency 30 of 30, and the silent-failure rate is 1%.
Across the 16 notes where we named different drugs it is 0.209.* That is a subset defined by
agreement, so medication is excluded from it — but it locates the bottleneck in the schema,
not the extraction.

Business line: **$3.57 per physician-year** at 24 notes a day, 250 days. The $8 budget for this
project covered 68 live calls with $7.96 left.

## 4:20 – 5:00 · Limitations, out loud

Say these plainly. They are scored, and every one is measured rather than hedged.

1. **The allergy field is unproven.** Zero positives in all 67 notes — one mention, and it is a
   denial. The model was right 67 times out of 67 and that tells us nothing about recall.
2. **It inherits the transcriber's errors.** One note dictates "Dolo 450", a strength that drug
   is not sold in. The pipeline grounded it faithfully, because the phrase really is in the
   transcript. Verbatim grounding propagates an upstream error rather than correcting it —
   which is the argument for physician sign-off, and for a formulary check as the next
   guardrail rather than a bigger model.
3. **I wrote the system and the labels.** That is the weakest part of the evaluation, and the
   fix in flight is a second annotator from a different model family that may queue
   disagreements but never edit a label.
4. **English, Latin script only.** 88 of the 156 rows are Devanagari and are excluded by an
   explicit filter, not quietly dropped.
5. **Not clinical decision support.** An administrative drafting aid for explicit review, no
   autonomous write-back, and not for live patient data without a processing agreement.

Close on the one sentence that tells you it worked: *every field a physician sees is either
backed by her own words or blank.*

---

## Do not say

- Do not call 0.979 the result. It is conditioned on agreement and must appear beside 0.600.
- Do not say the system is "PDPA compliant" or "HIPAA compliant". It supports compliance;
  compliance is an organisational property.
- Do not claim the allergy field works.
- Do not describe this as an agent. It has no tools and takes no actions; that boundary is
  deliberate and documented.
- Do not read a number off memory. Everything quotable is in `scores.json`, `manifest.json`
  or `evals/diagnose.py`.
