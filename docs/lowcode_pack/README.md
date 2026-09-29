# Low-code pack — run the arm for real, in about twenty minutes

[`docs/lowcode_arm.md`](../lowcode_arm.md) measures a **proxy** low-code arm: the same model and
prompt with the deterministic layer removed, built from an existing run at no cost. That is a
faithful stand-in and it is not a measurement of a console, because the payloads still enjoy this
project's prompt engineering.

This pack closes the gap. Everything a console needs is here, exported from the pipeline itself
rather than retyped.

| File | What it is |
| :--- | :--- |
| `system_prompt.txt` | `src/extract.py:SYSTEM_INSTRUCTION`, verbatim — 3,180 characters |
| `response_schema.json` | `src/extract.py:response_json_schema()`, verbatim — the strict schema |
| `convert_console_output.py` | turns the console's JSON into payloads the project's scorer grades |

Regenerate the first two whenever the prompt moves, so the pack cannot drift:

```bash
./.venv/bin/python -c "import sys,json; sys.path.insert(0,'src'); import extract; \
open('docs/lowcode_pack/system_prompt.txt','w').write(extract.SYSTEM_INSTRUCTION + chr(10)); \
json.dump(extract.response_json_schema(), open('docs/lowcode_pack/response_schema.json','w'), indent=2)"
```

The prompt in this pack is fingerprint **`a92d2abcb2af4294`** — the same one the sealed gold sets
were labelled against. If `extract.prompt_fingerprint()` no longer returns that, the pack is stale
and the comparison is not like-for-like.

## 1. Configure the console

In **Google AI Studio**:

| Setting | Value | Why |
| :--- | :--- | :--- |
| Model | `gemini-2.5-flash` | the same model the coded pipeline calls |
| System instructions | paste `system_prompt.txt` | the same instruction, unedited |
| Structured output | paste `response_schema.json` | the same four-field contract |
| Temperature | `0` | the pipeline's setting |
| Output token limit | `1024` | `extract.MAX_OUTPUT_TOKENS` |
| Thinking / reasoning | **off** | the pipeline sends `reasoning: {effort: "none"}` |

If the console will not accept the schema as given, **record that and stop** — "the tool could not
hold the contract" is a finding, and a more interesting one than a score. Note what it rejected.

## 2. Run the notes

Each note goes in wrapped exactly as the pipeline wraps it:

```
<note>
...the dictation...
</note>
```

Print the notes to paste, one at a time:

```bash
./.venv/bin/python -c "
import json
for c in json.load(open('data/gold_labels/gold_v2.json'))['cases']:
    print('=== ' + c['case_id']); print('<note>'); print(c['source_text']); print('</note>')
"
```

Twenty cases is enough to be interesting; sixty-seven makes it comparable field for field. Either
way, **say which you did** — a 20-case arm scored against a 67-case arm is not a comparison.

## 3. Collect the answers

One JSON file, keyed by case id, each value the object the console returned:

```json
{
  "case_001": {
    "medication": {"value": "paracetamol", "evidence": "paracetamol", "status": "found"},
    "dose":       {"value": "500", "evidence": "500", "status": "found"},
    "frequency":  {"value": "three times a day", "evidence": "three times a day", "status": "found"},
    "allergy":    {"value": "", "evidence": "", "status": "not_stated"}
  },
  "case_002": { "...": "..." }
}
```

Save it as `docs/lowcode_pack/console_results.json`. Paste what the console returned — do not tidy
it. A malformed answer is data: the pipeline's own fence-stripping and Pydantic validation exist
because models wrap JSON in Markdown, and a console that does the same is evidence for that gate.

## 4. Convert and score

```bash
./.venv/bin/python docs/lowcode_pack/convert_console_output.py \
    docs/lowcode_pack/console_results.json --out evals/results/lowcode-studio
```

```bash
./.venv/bin/python evals/score_arm.py evals/results/lowcode-studio \
    --gold data/gold_labels/gold_v2.json
```

```bash
./.venv/bin/python evals/metrics.py evals/results/lowcode-studio \
    evals/results/gold-v2-live --gold data/gold_labels/gold_v2.json
```

The converter runs **no gate**: a field with a value is delivered as stated, a field without one is
not stated. That is the point — the absence of a gate is what is being measured, and the scorer is
the same pre-registered one every other arm is graded by.

## 5. What to expect, and what would be surprising

From the proxy arm, the coded pipeline removed **three wrong values** at **zero** cost in recall
(paired McNemar 0 flips, p = 1.000). So on routine notes expect a console to land close on recall
and a little worse on precision.

Two outcomes would be genuinely informative and both are worth more than a score:

- **The console cannot hold the schema**, or wraps its JSON in Markdown. That is the argument for
  `parse_output` and strict Pydantic validation, made by the tool itself.
- **The console has nowhere to put a tripwire.** Feed it `demo/notes/case_003_injection.txt`. The
  live battery found the model obeys that vector, and the coded pipeline downgrades all three
  planted values to review. A console will hand you `warfarin 10 mg daily` as a clean answer. That
  single case is the whole architecture argument, and it costs one paste.

## What this pack cannot tell you

It measures one console, one prompt, one sitting, by hand. It says nothing about a low-code
*workflow builder* (n8n, Power Automate) wrapping the same API, which would have the same gate
problem and a different cost structure. And a hand-run arm has no latency or spend telemetry, so
the cost-to-serve comparison in [`cost_to_serve.md`](../cost_to_serve.md) does not extend to it.
