# MediExtract — video demonstration: deck, script, and render blocks

**Runtime 6:30.** Nine scenes. Every number is quoted from a committed artefact and every command
shown is in [`commands.md`](../commands.md).

Each scene carries a JSON block with `scene_id`, `timestamp`, `narration_text` and
`latex_overlay_code`, for a Colab renderer (Manim / LaTeX / PyCairo). The LaTeX is standalone
`tikzpicture` / `tabular` fragments — no document preamble, so they can be dropped into a
`standalone` template or converted to Manim `Tex` objects directly.

**Recording notes.** Terminal at 16pt or larger. Run the mock commands live — they need no key and
no network, so nothing can fail on camera. Have `demo/review.html` already open in a second tab.

---

## Scene 1 — The three-second rule · 0:00–0:50

**On screen:** Dr. Aisha persona card, then the 3-second constraint filling the frame.

```json
{
  "scene_id": "01_persona",
  "timestamp": "00:00-00:50",
  "narration_text": "Dr. Aisha is a general practitioner in a Singapore polyclinic. She dictates a consult in ninety seconds, and then spends minutes retyping medication, dose and frequency into separate boxes to close the chart. Scribing tools already write fluent prose for her. That is not her problem. Her problem is that she cannot see where a value came from, and she will not sign off an AI-generated dose she cannot verify in under three seconds. So this project does not optimise for a fluent summary. It optimises for a value you can check at a glance, and a blank wherever the system cannot prove what it is telling you.",
  "latex_overlay_code": "\\begin{tikzpicture}\n  \\node[draw, rounded corners, thick, minimum width=11cm, minimum height=2.4cm, align=left, font=\\large] (p) at (0,0) {\\textbf{Dr.\\ Aisha} --- General Practitioner\\\\ Singapore Polyclinic\\\\[2pt] \\small 157 min/day clerical documentation};\n  \\node[below=0.7cm of p, font=\\Huge\\bfseries, text=red!70!black] {3 seconds to verify};\n  \\node[below=2.0cm of p, font=\\large] {or she does not use it};\n\\end{tikzpicture}"
}
```

## Scene 2 — What the system is · 0:50–1:25

**On screen:** the Mermaid architecture diagram from the README, animated left to right.

```json
{
  "scene_id": "02_architecture",
  "timestamp": "00:50-01:25",
  "narration_text": "One dictated note in. One call to Gemini 2.5 Flash through OpenRouter, at temperature zero, with a strict JSON schema and no tools. Then deterministic gates in plain Python: is the quoted evidence actually present in the note, word for word; is the value actually supported by its own quote; does the dose number carry its unit. If any check fails, the field is hard-wiped to null. Not to the string BLANK, not to a friendly apology — to null, because the database downstream must never receive a display string. There is no framework here. The entire pipeline is one file you can read top to bottom.",
  "latex_overlay_code": "\\begin{tikzpicture}[node distance=1.5cm, every node/.style={font=\\small}]\n  \\node[draw, rounded corners] (n) {Dictated note};\n  \\node[draw, rounded corners, right=of n] (s) {Input scan};\n  \\node[draw, rounded corners, right=of s, fill=blue!10] (m) {1 LLM call};\n  \\node[draw, rounded corners, right=of m, fill=green!12] (g) {Gates};\n  \\node[draw, rounded corners, above right=0.5cm and 1.2cm of g] (ok) {value + evidence};\n  \\node[draw, rounded corners, below right=0.5cm and 1.2cm of g, fill=red!12] (bad) {\\textbf{null}};\n  \\draw[->, thick] (n)--(s); \\draw[->, thick] (s)--(m); \\draw[->, thick] (m)--(g);\n  \\draw[->, thick] (g)--(ok) node[midway, above, font=\\tiny] {grounded};\n  \\draw[->, thick] (g)--(bad) node[midway, below, font=\\tiny] {cannot prove it};\n\\end{tikzpicture}"
}
```

## Scene 3 — Live demo, the grounded case · 1:25–2:20

**Run on camera:**

```bash
./.venv/bin/python src/extract.py --note gold/case_001.txt --mock
```

Then switch to `demo/review.html` and point at one highlighted span.

```json
{
  "scene_id": "03_demo_grounded",
  "timestamp": "01:25-02:20",
  "narration_text": "Here is a real note from the corpus. I run the pipeline. Standard output is one JSON document and nothing else — every log line goes to standard error, so this pipes straight into a database without a single stray character breaking it. Look at the structure: each field has a value, and beside it the exact span of the note that value came from. Now the review screen. Medication, dose, frequency — and beneath each one, highlighted in the original dictation, the words it was taken from. That is the three-second check. Dr. Aisha is not reading the note again. She is confirming that a highlight sits under a value.",
  "latex_overlay_code": "\\begin{tabular}{@{}l l l@{}}\n  \\toprule\n  \\textbf{Field} & \\textbf{Value} & \\textbf{Evidence in note} \\\\\n  \\midrule\n  medication & paracetamol & \\colorbox{yellow!45}{paracetamol} \\\\\n  dose & 500 & \\colorbox{yellow!45}{500} \\\\\n  frequency & three times a day & \\colorbox{yellow!45}{three times a day} \\\\\n  allergy & \\textit{null} & --- \\\\\n  \\bottomrule\n\\end{tabular}"
}
```

## Scene 4 — Live demo, the abstention · 2:20–3:05

**Run on camera:**

```bash
./.venv/bin/python src/extract.py --note gold/case_002_traps.txt --mock; echo "exit $?"
```

```json
{
  "scene_id": "04_demo_abstain",
  "timestamp": "02:20-03:05",
  "narration_text": "This second note is the adversarial fixture. It contains the father's metformin, not the patient's. A drug stopped in March. A drug only being considered. An ambiguous penicillin history. Watch the fields come back null. And watch the exit code: one, not zero. One means the system ran correctly and chose to abstain. An API timeout returns three. Those codes are deliberately kept apart, because if a network failure returned the same code as a safe abstention, every batch evaluation would silently inflate its own abstention rate and the system would look safer than it is. A blank field here is not a failure of the product. It is the product.",
  "latex_overlay_code": "\\begin{tikzpicture}\n  \\node[font=\\large\\bfseries] (t) at (0,1.2) {Exit codes are separated on purpose};\n  \\node[below=0.2cm of t] {\\begin{tabular}{@{}r l@{}}\n    \\toprule\n    \\texttt{0} & all four fields verified \\\\\n    \\texttt{\\textbf{1}} & \\textbf{ran correctly, safely abstained} \\\\\n    \\texttt{2} & input rejected \\\\\n    \\texttt{\\textbf{3}} & \\textbf{upstream / API failure} \\\\\n    \\texttt{5} & spend bound would be crossed \\\\\n    \\bottomrule\n  \\end{tabular}};\n  \\node[below=3.4cm of t, font=\\small\\itshape, text=red!70!black] {collapse 1 and 3 and the abstention rate becomes fiction};\n\\end{tikzpicture}"
}
```

## Scene 5 — Does it actually work · 3:05–3:45

```json
{
  "scene_id": "05_results",
  "timestamp": "03:05-03:45",
  "narration_text": "Against sealed ground truth — sixty-seven consults, two hundred and sixty-eight field decisions — pooled recall is seventy-three percent. That number means nothing on its own, so it is never reported on its own. A regex baseline with a fourteen-thousand-name RxNorm gazetteer, which I built and committed, reaches thirty-eight. And answering not-stated to every single field already agrees with the labels one hundred and thirteen times out of two hundred and sixty-eight, at zero recall. Every headline in this project is printed beside those two comparators, because a recall figure with nothing to compare it against says nothing at all.",
  "latex_overlay_code": "\\begin{tabular}{@{}l r@{}}\n  \\toprule\n  \\textbf{Arm} & \\textbf{Pooled recall} \\\\\n  \\midrule\n  \\textbf{gemini-2.5-flash, gated} & \\textbf{0.730} \\\\\n  regex + RxNorm gazetteer (14,689) & 0.381 \\\\\n  majority class (all \\texttt{not\\_stated}) & 0.000 \\\\\n  \\midrule\n  \\multicolumn{2}{@{}l@{}}{\\small cost \\$0.000617/note $\\rightarrow$ \\textbf{\\$3.58 per physician-year}} \\\\\n  \\multicolumn{2}{@{}l@{}}{\\small median latency 1{,}293\\,ms \\quad 66/67 inside the 3\\,s budget} \\\\\n  \\bottomrule\n\\end{tabular}"
}
```

## Scene 6 — The finding I did not expect · 3:45–4:40

```json
{
  "scene_id": "06_header_leakage",
  "timestamp": "03:45-04:40",
  "narration_text": "Here is the result I did not go looking for. I built a second corpus of twenty notes that genuinely record an allergy, under an all-caps section header. Recall: ninety percent. Then I removed just the headers — same notes, same labels, nothing else touched — and recall fell to sixty-five. The model was not reading the clinical text. It was reading the formatting. And then the important part: I went back through all thirty of my safety gates and none of them fires on this. Every gate I built catches an invented value. Not one catches a missed one. Every gate in this system is a precision control, and there is no recall control anywhere in it. I only know that because I measured something I expected to be boring.",
  "latex_overlay_code": "\\begin{tikzpicture}\n  \\begin{scope}[yscale=2.6]\n    \\draw[->] (0,0)--(0,1.05) node[above, font=\\small] {allergy recall};\n    \\fill[green!55!black] (0.7,0) rectangle (1.7,0.90);\n    \\fill[red!65!black]   (2.4,0) rectangle (3.4,0.65);\n    \\node[font=\\small\\bfseries, text=white] at (1.2,0.80) {0.900};\n    \\node[font=\\small\\bfseries, text=white] at (2.9,0.55) {0.650};\n  \\end{scope}\n  \\node[font=\\small] at (1.2,-0.2) {headers intact};\n  \\node[font=\\small] at (2.9,-0.2) {headers stripped};\n  \\node[align=center, font=\\small, below=0.8cm] at (2.05,-0.2) {$-25$ pp \\quad McNemar exact $p = 0.0625$\\\\[3pt] \\textbf{every gate is a precision control;}\\\\ \\textbf{none is a recall control}};\n\\end{tikzpicture}"
}
```

## Scene 7 — Statistics done properly · 4:40–5:25

```json
{
  "scene_id": "07_paired_stats",
  "timestamp": "04:40-05:25",
  "narration_text": "When I corrected eighteen labels and re-scored, the headline jumped eight and a half points. It would have been very easy to call that an improvement. It is not. Seven point eight of those points are the corrected answer key, and only zero point seven is the system — and that zero point seven does not separate from noise at all. The paired McNemar test on the label change gives p equals zero point zero zero eight. The unpaired Fisher test on the very same eight flips gives zero point three seven three. Same data, opposite conclusions, because Fisher throws away the fact that both runs saw identical notes. So this project reports the paired test with every delta, and shows Fisher beside it labelled as the number that does not apply.",
  "latex_overlay_code": "\\begin{tikzpicture}\n  \\node[font=\\large\\bfseries] at (0,2.2) {One 8.5-point move, decomposed};\n  \\node at (0,0.9) {\\begin{tabular}{@{}l r r@{}}\n    \\toprule\n    & \\textbf{recall} & \\textbf{McNemar} \\\\\n    \\midrule\n    A \\ gold-v1 & 0.6452 & --- \\\\\n    B \\ same outputs, corrected labels & 0.7230 & $\\mathbf{p = 0.008}$ \\\\\n    C \\ live run, corrected labels & 0.7297 & $p = 1.000$ \\\\\n    \\midrule\n    \\multicolumn{3}{@{}l@{}}{\\textbf{7.8 pts = the labels \\quad 0.7 pts = noise}} \\\\\n    \\bottomrule\n  \\end{tabular}};\n  \\node[font=\\small, text=red!70!black] at (0,-1.5) {unpaired Fisher on the same 8 flips: $p = 0.373$ --- it finds nothing};\n\\end{tikzpicture}"
}
```

## Scene 8 — The judge I built and threw away · 5:25–6:00

```json
{
  "scene_id": "08_judge_rejected",
  "timestamp": "05:25-06:00",
  "narration_text": "My security specification proposed an LLM-as-a-judge to grade extractions automatically, and — crucially — it wrote the adoption criterion before I wrote the code: ninety percent agreement with the human labels on both classes. I built it, using a different model family so it could not simply agree with itself. On the extractions gold calls correct, it scored ninety-five. On the ones gold calls wrong, it scored twenty-one. And a judge that just says correct to everything scores eighty-four, while mine scored eighty-three. It does not beat a rubber stamp. So it was not adopted. Writing the gate before the measurement is what made that an easy decision instead of an argument with myself.",
  "latex_overlay_code": "\\begin{tikzpicture}\n  \\node[font=\\large\\bfseries] at (0,2.1) {S-14: gate written \\textit{before} the code};\n  \\node at (0,0.7) {\\begin{tabular}{@{}l r c@{}}\n    \\toprule\n    \\textbf{Agreement with gold} & & \\textbf{gate 0.90} \\\\\n    \\midrule\n    class: gold says \\textsc{correct} & 0.9467 & \\textcolor{green!55!black}{\\textbf{pass}} \\\\\n    class: gold says \\textsc{wrong} & \\textbf{0.2093} & \\textcolor{red!75!black}{\\textbf{fail}} \\\\\n    \\midrule\n    judge, overall & 0.8284 & \\\\\n    \\textit{rubber stamp} (accept everything) & \\textit{0.8396} & \\\\\n    \\bottomrule\n  \\end{tabular}};\n  \\node[font=\\Large\\bfseries, text=red!75!black] at (0,-1.6) {NOT ADOPTED};\n\\end{tikzpicture}"
}
```

## Scene 9 — The spec checks itself · 6:00–6:30

**Run on camera:**

```bash
./.venv/bin/python -O -m unittest discover -s tests
./.venv/bin/python -m unittest tests.test_guardrails_doc
```

```json
{
  "scene_id": "09_ci_verification",
  "timestamp": "06:00-06:30",
  "narration_text": "Last thing. Three hundred and seventy-seven tests, and I run them twice — the second time with python dash capital O, which strips every assert statement out of the bytecode. That is exactly why no safety gate in this codebase is allowed to be an assert, and an AST walk enforces it. And this one: my security document parses its own code snippets, resolves every symbol it cites against the real source, and compares every number it quotes to the artefact that produced it. Change a figure in the document without re-running the experiment and the test suite goes red and names the file that disagrees. The documentation cannot drift from the code, because the code refuses to let it.",
  "latex_overlay_code": "\\begin{tikzpicture}\n  \\node[draw, thick, rounded corners, fill=black!92, text=green!75!black, font=\\ttfamily\\small, align=left, inner sep=10pt] {\n    \\$ python -O -m unittest discover -s tests\\\\\n    Ran 377 tests ... \\textbf{OK}\\\\[6pt]\n    \\$ python -m unittest tests.test\\_guardrails\\_doc\\\\\n    Ran 15 tests ... \\textbf{OK}\\\\[6pt]\n    \\textcolor{white}{-- every snippet parses}\\\\\n    \\textcolor{white}{-- every file:symbol resolves}\\\\\n    \\textcolor{white}{-- every quoted metric == its artefact}\n  };\n\\end{tikzpicture}"
}
```

---

## Slide-by-slide summary

| # | Slide | Time | The one thing it must land |
| ---: | :--- | :--- | :--- |
| 1 | Dr. Aisha · the 3-second rule | 0:50 | the constraint is verification speed, not fluency |
| 2 | Architecture | 0:35 | one call, one file, deterministic gates, hard wipe to null |
| 3 | Live demo — grounded | 0:55 | evidence sits beside every value |
| 4 | Live demo — abstention | 0:45 | a blank is the product; exit 1 ≠ exit 3 |
| 5 | Results vs comparators | 0:40 | 0.730 against 0.381 and 0.000 |
| 6 | Header leakage | 0:55 | 0.900 → 0.650; every gate is precision-only |
| 7 | Paired statistics | 0:45 | 7.8 pts labels / 0.7 pts system; p = 0.008 vs 0.373 |
| 8 | The judge, refused | 0:35 | gate written first; 0.2093; loses to a rubber stamp |
| 9 | Self-verifying CI | 0:30 | 377 tests under `-O`; the doc cannot drift |

## If you must cut to 5 minutes

Cut Scene 7 to a single sentence ("the label correction and the system change are reported
separately, p = 0.008 against p = 1.000") and drop Scene 8 to fifteen seconds. Do not cut Scene 4
or Scene 6: the abstention *is* the product, and the leakage finding is the only place the project
discovers something it did not set out to look for.
