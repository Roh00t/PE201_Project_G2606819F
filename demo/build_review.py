#!/usr/bin/env python3
"""Build the physician's verification screen from a finished run.

    ./.venv/bin/python demo/build_review.py                    # newest run
    ./.venv/bin/python demo/build_review.py evals/results/<run_id> --out demo/review.html

This is the interface project_proposal.md section 5 promises: every field beside
the verbatim phrase from the dictation that produced it, and a visible blank
wherever the pipeline could not ground one. It is what makes a five-minute
demonstration legible to someone who will not read `scores.json`.

Deliberately inert, because the input is untrusted clinical text and this file
is the first HTML this project ships (guardrails.md control S-07, OWASP
LLM10:2026 Improper Output Handling):

  - **no JavaScript at all, and no form.** The case selector is CSS `:checked`
    sibling matching on bare radio inputs, so the page needs no script and no
    inline handler, and the policy below can forbid script outright. A review
    screen has nothing to compute.
  - every interpolated string goes through `html.escape(quote=True)`. The
    corpus really does contain angle brackets - notes carry `<PII>`
    de-identification placeholders, and naive interpolation would swallow them
    silently, which is the benign version of the same bug that executes a
    `<script>` a model was talked into emitting.
  - highlights are built by walking character offsets of the raw note and
    escaping each run between boundaries, never by substituting into escaped
    text, so a span can never straddle an entity and break it.
  - `Content-Security-Policy: default-src 'none'` with no remote origins, so
    the page cannot reach the network even if something slipped through.

It reads; it never writes to the run directory or to any gold file.
stdout: the path written. stderr: a short summary.
Exit codes: 0 written; 2 run directory or gold file unusable; 6 internal error.
"""

import argparse
import html
import json
import statistics
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "evals"))

from diagnose import newest_run  # noqa: E402  (same "newest run" rule as the diagnostics)
from extract import BLANK, CRITICAL_FIELDS, DISPLAY  # noqa: E402

GOLD_DIR = ROOT / "data" / "gold_labels"
SEALED_PATH = GOLD_DIR / "gold_v1.json"
RESULTS_DIR = ROOT / "evals" / "results"
DEFAULT_OUT = ROOT / "demo" / "review.html"

EXIT_OK = 0
EXIT_UNUSABLE = 2
EXIT_INTERNAL = 6

# What the physician is being asked to do about each field, in her words.
ACTION = {
    "VERIFIED": "Check it against the quote and accept.",
    "NOT_STATED": "Nothing was dictated for this field.",
    "REVIEW_MODEL_UNSURE": "The dictation is ambiguous here. Decide yourself.",
    "REVIEW_INJECTION_PATTERN": "The note contains instruction-like text. Read it before accepting.",
}
DEFAULT_ACTION = "Left blank on purpose. Type it in yourself if the dictation says it."


def esc(value) -> str:
    """Every string that reaches the page passes through here."""
    return html.escape("" if value is None else str(value), quote=True)


def spans_for(note: str, extraction: dict) -> tuple[list[tuple[int, int, str]], dict[str, int]]:
    """Character ranges of each field's quote inside the raw note.

    The gate guarantees a passing quote is a literal substring, so `find` is
    the whole search. A quote that occurs more than once is highlighted at its
    first occurrence and the count is reported, because containment is what the
    gate checks - it never claimed to know which occurrence the model meant.
    """
    spans, counts = [], {}
    for field in CRITICAL_FIELDS:
        evidence = (extraction.get(field) or {}).get("evidence")
        if not evidence:
            continue
        start = note.find(evidence)
        if start < 0:
            continue
        spans.append((start, start + len(evidence), field))
        counts[field] = note.count(evidence)
    return spans, counts


def highlight(note: str, spans: list[tuple[int, int, str]]) -> str:
    """The note with each quote wrapped, escaping run by run.

    Overlapping quotes are normal - a dose sits inside the medication phrase -
    so each character carries the set of fields covering it and runs of equal
    sets are emitted together. That keeps the markup well formed no matter how
    the spans interleave.
    """
    if not note:
        return ""
    labels: list[set] = [set() for _ in note]
    for start, end, field in spans:
        for index in range(max(0, start), min(len(note), end)):
            labels[index].add(field)
    out, i = [], 0
    while i < len(note):
        current = frozenset(labels[i])
        j = i
        while j < len(note) and frozenset(labels[j]) == current:
            j += 1
        chunk = esc(note[i:j])
        if current:
            classes = " ".join(sorted(f"q-{f}" for f in current))
            out.append(f'<mark class="{classes}" title="{esc(", ".join(sorted(current)))}">'
                       f"{chunk}</mark>")
        else:
            out.append(chunk)
        i = j
    return "".join(out)


def field_rows(payload: dict, raw: dict | None) -> str:
    codes = {g["field"]: g["code"] for g in payload.get("gate", [])}
    rows = []
    for field in CRITICAL_FIELDS:
        got = payload["extraction"].get(field) or {}
        value, evidence = got.get("value"), got.get("evidence")
        code = codes.get(field, "")
        blank = value in (None, BLANK)
        shown = ('<span class="blank">&mdash; blank &mdash;</span>' if blank
                 else f"<strong>{esc(value)}</strong>")
        quote = (f"<q>{esc(evidence)}</q>" if evidence
                 else '<span class="blank">no quote</span>')
        state = "ok" if code == "VERIFIED" else ("none" if code == "NOT_STATED" else "held")
        note = ""
        if raw is not None and blank and code.startswith("ABSTAIN"):
            rejected = (raw["extraction"].get(field) or {}).get("value")
            if rejected:
                note = ('<div class="rejected"><span class="tag">engineering detail, '
                        "not clinical data</span> the model proposed "
                        f"<code>{esc(rejected)}</code> and the gate refused it</div>")
        rows.append(
            f'<tr class="{state}">'
            f"<th>{esc(field)}</th>"
            f"<td>{shown}{note}</td>"
            f"<td>{quote}</td>"
            f'<td class="status">{esc(DISPLAY.get(code, code))}<br>'
            f'<span class="action">{esc(ACTION.get(code, DEFAULT_ACTION))}</span></td>'
            "</tr>"
        )
    return "\n".join(rows)


def case_panel(case_id: str, note: str, payload: dict, raw: dict | None) -> str:
    spans, counts = spans_for(note, payload["extraction"])
    repeated = [f"{f} (appears {n}x)" for f, n in sorted(counts.items()) if n > 1]
    latency = payload.get("latency_ms", {}) or {}
    usage = payload.get("usage", {}) or {}
    flags = (payload.get("input_scan", {}) or {}).get("flags") or []
    provenance = payload.get("provenance", {}) or {}
    api_ms = latency.get("api") or 0.0
    within = payload.get("within_budget")
    budget = payload.get("latency_budget_ms")
    return f"""
<section class="panel" id="p-{esc(case_id)}">
  <h2>{esc(case_id)}</h2>
  <div class="dictation">
    <h3>The dictation, as recorded</h3>
    <p class="note">{highlight(note, spans)}</p>
    {'<p class="caveat">Quote occurs more than once: ' + esc("; ".join(repeated))
      + ". The gate checks that the quote is in the note, not which occurrence it came from.</p>"
      if repeated else ""}
  </div>
  <table class="fields">
    <thead><tr><th>Field</th><th>Value</th><th>Quote from your dictation</th>
    <th>Status and what to do</th></tr></thead>
    <tbody>
{field_rows(payload, raw)}
    </tbody>
  </table>
  <p class="meta">
    {esc(f"{api_ms:.0f} ms")} to answer
    {esc(f"(budget {budget:.0f} ms)") if isinstance(budget, (int, float)) else ""}
    {'&middot; <span class="ok-tag">inside budget</span>' if within else
     '&middot; <span class="held-tag">over budget</span>' if within is False else ""}
    &middot; {esc(f"${usage.get('billed_usd', 0):.6f}")} billed
    &middot; {esc(usage.get("input_tokens", "?"))} in / {esc(usage.get("output_tokens", "?"))} out
    &middot; served by {esc(provenance.get("served_model") or provenance.get("requested_model") or "-")}
    {'&middot; <span class="held-tag">note flagged: ' + esc(", ".join(flags)) + "</span>" if flags else ""}
  </p>
</section>"""


def summary_block(manifest: dict, scores: dict | None, payloads: list[dict]) -> str:
    served = [p for p in payloads if (p.get("provenance") or {}).get("called")]
    api = [p["latency_ms"]["api"] for p in served] or [0.0]
    billed = sum((p.get("usage") or {}).get("billed_usd", 0.0) for p in served)
    cells = [
        ("notes", f"{len(payloads)}"),
        ("median answer", f"{statistics.median(api):.0f} ms"),
        ("slowest", f"{max(api):.0f} ms"),
        ("cost for the batch", f"${billed:.4f}"),
        ("cost per note", f"${billed / len(served):.6f}" if served else "-"),
    ]
    if scores:
        pooled = scores.get("pooled") or {}
        cells += [
            ("recall", f"{pooled.get('recall')}"),
            ("precision", f"{pooled.get('precision')}"),
            ("abstention rate", f"{pooled.get('abstention_rate')}"),
            ("silent-failure rate", f"{pooled.get('silent_failure_rate')}"),
        ]
    tiles = "\n".join(f'<div class="tile"><span class="k">{esc(k)}</span>'
                      f'<span class="v">{esc(v)}</span></div>' for k, v in cells)
    return f"""
<header>
  <h1>MediExtract &mdash; verification screen</h1>
  <p class="sub">Run {esc(manifest.get('run_id'))} &middot; {esc(manifest.get('model'))}
     &middot; {esc(manifest.get('mode'))} &middot; prompt
     {esc(manifest.get('prompt_fingerprint'))}
     {'&middot; experiment ' + esc(manifest.get('experiment')) if manifest.get('experiment') else ''}</p>
  <div class="tiles">{tiles}</div>
  <p class="caveat">Recall and the other rates come from this run's
     <code>scores.json</code> against the sealed gold set, and are the
     pre-registered numbers, not a re-scoring. Every field below is the gated
     payload &mdash; what the database would receive.</p>
</header>"""


STYLE = """
:root { --ink:#16191d; --muted:#5b6672; --line:#d8dee6; --bg:#fbfcfd; --panel:#fff;
        --ok:#0a6b3d; --ok-bg:#e6f4ec; --held:#8a4b00; --held-bg:#fdf1e2;
        --none:#5b6672; --none-bg:#f1f3f6;
        --q-medication:#dbe9ff; --q-dose:#ffe9c9; --q-frequency:#d9f2e4; --q-allergy:#f6dcef; }
* { box-sizing:border-box; }
body { margin:0; font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
       color:var(--ink); background:var(--bg); }
header { padding:22px 24px 16px; background:var(--panel); border-bottom:1px solid var(--line); }
h1 { margin:0 0 4px; font-size:20px; letter-spacing:-.01em; }
.sub { margin:0 0 14px; color:var(--muted); font-size:13px; }
.tiles { display:flex; flex-wrap:wrap; gap:8px; }
.tile { border:1px solid var(--line); border-radius:8px; padding:8px 12px; background:var(--bg); }
.tile .k { display:block; font-size:11px; text-transform:uppercase; letter-spacing:.06em;
           color:var(--muted); }
.tile .v { font-size:16px; font-variant-numeric:tabular-nums; }
.caveat { margin:12px 0 0; color:var(--muted); font-size:12.5px; max-width:78ch; }
.app { display:flex; align-items:flex-start; }
.app > input { position:absolute; opacity:0; pointer-events:none; }
nav { width:190px; flex:0 0 190px; border-right:1px solid var(--line); background:var(--panel);
      max-height:calc(100vh - 40px); overflow:auto; padding:10px 0; }
nav label { display:block; padding:7px 14px; font-size:13px; cursor:pointer;
            border-left:3px solid transparent; }
nav label:hover { background:var(--bg); }
nav .badge { float:right; font-size:11px; color:var(--muted); }
main { flex:1; min-width:0; padding:20px 24px 60px; }
.panel { display:none; }
h2 { margin:0 0 14px; font-size:16px; font-family:ui-monospace,SFMono-Regular,Menlo,monospace; }
h3 { margin:0 0 6px; font-size:12px; text-transform:uppercase; letter-spacing:.06em;
     color:var(--muted); font-weight:600; }
.dictation { background:var(--panel); border:1px solid var(--line); border-radius:10px;
             padding:14px 16px; margin-bottom:16px; }
.note { margin:0; white-space:pre-wrap; word-wrap:break-word; }
mark { padding:1px 0; border-radius:2px; background:#eee; }
mark.q-medication { background:var(--q-medication); }
mark.q-dose { background:var(--q-dose); }
mark.q-frequency { background:var(--q-frequency); }
mark.q-allergy { background:var(--q-allergy); }
table.fields { width:100%; border-collapse:collapse; background:var(--panel);
               border:1px solid var(--line); border-radius:10px; overflow:hidden; }
table.fields th, table.fields td { text-align:left; vertical-align:top; padding:10px 12px;
                                   border-top:1px solid var(--line); font-size:14px; }
table.fields thead th { border-top:0; font-size:11px; text-transform:uppercase;
                        letter-spacing:.06em; color:var(--muted); background:var(--bg); }
table.fields tbody th { width:110px; font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
                        font-weight:500; color:var(--muted); }
tr.ok .status { color:var(--ok); }
tr.held { background:var(--held-bg); }
tr.held .status { color:var(--held); }
tr.none .status { color:var(--none); }
.blank { color:var(--muted); font-style:italic; }
q { color:var(--ink); }
.status { width:270px; font-size:13px; }
.action { color:var(--muted); font-size:12px; }
.rejected { margin-top:6px; font-size:12px; color:var(--held); }
.tag { display:inline-block; background:var(--held-bg); border:1px solid #e8d3b3;
       border-radius:4px; padding:0 5px; margin-right:6px; font-size:11px; }
.ok-tag { color:var(--ok); } .held-tag { color:var(--held); }
.meta { color:var(--muted); font-size:12.5px; margin:14px 0 0;
        font-variant-numeric:tabular-nums; }
code { font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:12.5px; }
footer { padding:0 24px 40px; color:var(--muted); font-size:12px; max-width:88ch; }
"""


def build(run_dir: Path, gold_path: Path) -> tuple[str, int]:
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    notes = {c["case_id"]: c["source_text"]
             for c in json.loads(gold_path.read_text(encoding="utf-8"))["cases"]}
    gated = [json.loads(line) for line in
             (run_dir / "gated.jsonl").read_text(encoding="utf-8").splitlines() if line]
    ungated_path = run_dir / "ungated.jsonl"
    raws = {}
    if ungated_path.is_file():
        raws = {r["case_id"]: r for r in
                (json.loads(line) for line in
                 ungated_path.read_text(encoding="utf-8").splitlines() if line)}
    scores_path = run_dir / "scores.json"
    scores = json.loads(scores_path.read_text(encoding="utf-8")) if scores_path.is_file() else None

    usable = [p for p in gated if p.get("status") == "ok" and p["case_id"] in notes]
    radios, labels, panels = [], [], []
    for index, payload in enumerate(usable):
        case_id = payload["case_id"]
        codes = [g["code"] for g in payload.get("gate", [])]
        held = sum(c.startswith("ABSTAIN") or c.startswith("REVIEW") for c in codes)
        verified = sum(c == "VERIFIED" for c in codes)
        badge = f"{held} held" if held else (f"{verified} ok" if verified else "nothing stated")
        checked = " checked" if index == 0 else ""
        radios.append(f'<input type="radio" name="case" id="r-{esc(case_id)}"{checked}>')
        labels.append(f'<label for="r-{esc(case_id)}">{esc(case_id)}'
                      f'<span class="badge">{esc(badge)}</span></label>')
        panels.append(case_panel(case_id, notes[case_id], payload, raws.get(case_id)))

    rules = "\n".join(
        f'#r-{p["case_id"]}:checked ~ main #p-{p["case_id"]} {{ display:block; }}\n'
        f'#r-{p["case_id"]}:checked ~ nav label[for="r-{p["case_id"]}"] '
        "{ background:var(--bg); border-left-color:var(--ink); font-weight:600; }"
        for p in usable)

    page = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy"
      content="default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<title>MediExtract review &mdash; {esc(manifest.get('run_id'))}</title>
<style>{STYLE}
{rules}
</style>
</head>
<body>
{summary_block(manifest, scores, usable)}
<div class="app">
{chr(10).join(radios)}
<nav>{chr(10).join(labels)}</nav>
<main>
{chr(10).join(panels)}
</main>
</div>
<footer>
  <p>Generated by <code>demo/build_review.py</code> from
     <code>{esc(str(run_dir.name))}</code> and <code>{esc(gold_path.name)}</code>. Static,
     offline, no script: the case selector is CSS only and the page declares
     <code>default-src 'none'</code>, so it cannot reach the network. Every value shown is
     escaped, which is why the corpus's <code>&lt;PII&gt;</code> placeholders appear as text.</p>
  <p>A blank is a result, not a gap: it means the pipeline could not find the words in your
     dictation that justify the value, so it declines rather than guesses.</p>
</footer>
</body>
</html>
"""
    return page, len(usable)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("run_dir", nargs="?", type=Path, default=None,
                        help="a directory under evals/results (default: the newest)")
    parser.add_argument("--gold", type=Path, default=SEALED_PATH,
                        help="gold file supplying the note text")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="HTML file to write")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--debug", action="store_true")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        run_dir = args.run_dir or newest_run(RESULTS_DIR)
        if run_dir is None:
            print(f"no run found under {RESULTS_DIR}", file=sys.stderr)
            return EXIT_UNUSABLE
        missing = [name for name in ("manifest.json", "gated.jsonl")
                   if not (run_dir / name).is_file()]
        if missing or not args.gold.is_file():
            print(f"{run_dir}: missing {missing or [str(args.gold)]}", file=sys.stderr)
            return EXIT_UNUSABLE
        page, cases = build(run_dir, args.gold)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(page, encoding="utf-8")
    except Exception as exc:
        print(f"ERROR ERR_INTERNAL (exit {EXIT_INTERNAL}): {type(exc).__name__}", file=sys.stderr)
        if args.debug:
            traceback.print_exc()
        return EXIT_INTERNAL
    if not args.quiet:
        print(f"{cases} cases, {len(page):,} bytes", file=sys.stderr)
    sys.stdout.write(str(args.out) + "\n")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
