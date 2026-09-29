#!/usr/bin/env python3
"""The clinician-facing EHR view, built from the CLI's own output.

    ./.venv/bin/python demo/build_ehr.py                       # demo/payloads/*.json
    ./.venv/bin/python demo/build_ehr.py demo/payloads/case_001.json --out /tmp/one.html

`demo/build_review.py` is the evidence-grade screen: case ids, gate codes, token
counts, cost per call. It is the right artefact for a reviewer and the wrong one
for showing what the product *is*. This renders the same payloads as a chart a
physician would recognise - patient banner, dictation on the left, structured
fields on the right - so the three-second verification is legible without
reading a gate code.

**Every value on this page came out of `src/extract.py`'s stdout.** A payload
written by the CLI carries the note's *path* in its `note` key (a batch payload
carries the case id instead), so the dictation is read from the file the payload
itself names. Nothing here is transcribed by hand, and the pairing is checkable
in one step:

    ./.venv/bin/python src/extract.py --note gold/case_001.txt > demo/payloads/case_001.json
    ./.venv/bin/python demo/build_ehr.py

That matters more than it sounds. A hand-written mockup of this screen was
drafted first, and it showed two gate behaviours the system does not have: an
injection "BLOCKED" with the field blanked (real behaviour retains the value and
downgrades it to review), and an "attribution" check that does not exist at all.
Generating the page from the payload makes both mistakes impossible to make -
the only status wording available is `extract.DISPLAY[code]`, and the only codes
available are the ones in the payload.

**Deliberately inert, for the same reasons as `review.html`.** No script, no
form, no event handler; the case selector is CSS. `default-src 'none'`. The input
is untrusted clinical text and these notes carry literal `<PII>` placeholders, so
every interpolated string goes through `build_review.esc`.

**The patient identities are synthetic and labelled as such on the page.** The
corpus notes are de-identified and carry no names; the banner details here exist
so the screen reads as a chart on camera, and the page says so in a standing
notice rather than leaving a viewer to assume.

stdout: the path written, nothing else. stderr: a one-line summary.
Exit codes: 0 written; 2 inputs unusable; 6 internal error.
"""

import argparse
import json
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "evals"))
sys.path.insert(0, str(ROOT / "demo"))

# Reused rather than reimplemented: a second escaper or a second highlighter is a
# second thing to get wrong, and `tests/test_review.py` already pins these.
from build_review import esc, highlight, spans_for  # noqa: E402
from extract import BLANK, CRITICAL_FIELDS, DISPLAY  # noqa: E402

PAYLOAD_DIR = ROOT / "demo" / "payloads"
DEFAULT_OUT = ROOT / "demo" / "ehr.html"

EXIT_OK = 0
EXIT_UNUSABLE = 2
EXIT_INTERNAL = 6

# Synthetic. Stated on the page, not just here. Keyed by the payload's note stem
# so a new fixture gets a banner without editing the renderer.
PATIENTS = {
    "case_001": {"name": "Tan Wei Ming", "detail": "Male, 42 · ID SXXXX567A",
                 "visit": "Review — type 2 diabetes"},
    "case_002_traps": {"name": "Sarah Lim", "detail": "Female, 58 · ID SXXXX889C",
                       "visit": "Follow-up — hypertension"},
    "case_003_injection": {"name": "Daniel Ong", "detail": "Male, 28 · ID SXXXX112B",
                           "visit": "General health screening"},
}
UNKNOWN_PATIENT = {"name": "Synthetic patient", "detail": "de-identified corpus note",
                   "visit": "Consultation"}

# What the physician does about each field. Shares wording with build_review's
# ACTION, restated in chart language rather than review-screen language.
ACTION = {
    "VERIFIED": "Quote matches. Accept.",
    "NOT_STATED": "Nothing dictated for this field.",
    "REVIEW_MODEL_UNSURE": "Dictation is ambiguous. Decide yourself.",
    "REVIEW_INJECTION_PATTERN": "This note contains instruction-like text. Read it before accepting.",
}
DEFAULT_ACTION = "Left blank on purpose. Type it in if the dictation says it."

# Row state drives the chart's colour. Red is reserved for a field carrying a
# value the system is NOT standing behind - an injection-flagged or gate-held
# value a physician might otherwise accept at a glance. An empty field is
# neutral, because an unfilled box is not a clinical danger and painting it red
# is how red stops meaning anything.
STATE_LABEL = {
    "ok": "Verified",
    "flagged": "Needs review",
    "held": "Blanked",
    "none": "Not stated",
}


def state_for(code: str, blank: bool) -> str:
    """Chart state from the gate code. Four buckets, and the split between
    `flagged` and `held` is the one that carries clinical weight: a flagged field
    still shows a value the physician must judge, a held field shows nothing."""
    if code == "VERIFIED":
        return "ok"
    if code == "NOT_STATED":
        return "none"
    if code.startswith("REVIEW") and not blank:
        return "flagged"
    return "held"


def note_text(payload: dict, root: Path = ROOT) -> str | None:
    """The dictation, read from the file the payload names.

    A CLI payload's `note` is a path; a batch payload's is a case id. Only the
    first can be resolved to text here, and a payload whose note cannot be read
    is dropped rather than rendered against a guess.
    """
    named = payload.get("note")
    if not named:
        return None
    candidate = Path(named)
    for path in (candidate, root / candidate):
        if path.is_file():
            return path.read_text(encoding="utf-8")
    return None


def field_rows(payload: dict) -> str:
    codes = {g["field"]: g["code"] for g in payload.get("gate", [])}
    rows = []
    for field in CRITICAL_FIELDS:
        got = payload["extraction"].get(field) or {}
        value, evidence = got.get("value"), got.get("evidence")
        code = codes.get(field, "")
        blank = value in (None, BLANK)
        state = state_for(code, blank)
        shown = ('<span class="blank">&mdash; blank &mdash;</span>' if blank
                 else f"<span class=\"val\">{esc(value)}</span>")
        quote = (f"<q>{esc(evidence)}</q>" if evidence
                 else '<span class="blank">no quote in the dictation</span>')
        rows.append(
            f'<div class="field {state}">'
            f'<div class="flabel">{esc(field)}</div>'
            f'<div class="fbox">{shown}</div>'
            f'<div class="fbadge">{esc(STATE_LABEL[state])}</div>'
            f'<div class="fquote">{quote}</div>'
            f'<div class="fwhy">{esc(DISPLAY.get(code, code))} &mdash; '
            f"{esc(ACTION.get(code, DEFAULT_ACTION))}</div>"
            "</div>"
        )
    return "\n".join(rows)


def case_panel(case_id: str, note: str, payload: dict) -> str:
    spans, counts = spans_for(note, payload["extraction"])
    repeated = [f"{f} appears {n} times" for f, n in sorted(counts.items()) if n > 1]
    who = PATIENTS.get(case_id, UNKNOWN_PATIENT)
    flags = (payload.get("input_scan", {}) or {}).get("flags") or []
    latency = (payload.get("latency_ms", {}) or {}).get("total")
    codes = [g["code"] for g in payload.get("gate", [])]
    flagged = sum(1 for c in codes if c.startswith("REVIEW"))

    banner = ""
    if flags:
        banner = ('<p class="alert">Input scan flagged this dictation: '
                  f'<strong>{esc(", ".join(flags))}</strong>. '
                  f"{flagged} field(s) were downgraded to review rather than accepted.</p>")
    caveat = ""
    if repeated:
        caveat = ('<p class="caveat">The gate checks that the quote is in the note, not '
                  f'which occurrence: {esc("; ".join(repeated))}.</p>')
    within = payload.get("within_budget")
    budget = payload.get("latency_budget_ms")
    if not isinstance(latency, (int, float)):
        timing = ""
    else:
        over = within is False
        klass = "chip over" if over else "chip"
        tail = (f" &mdash; over the {budget:,.0f} ms budget" if over and
                isinstance(budget, (int, float)) else "")
        timing = (f'<span class="{klass}">model call {latency:,.0f} ms{tail}</span>')

    return f"""
<section class="panel" id="p-{esc(case_id)}">
  <div class="pbanner">
    <div>
      <h2>{esc(who['name'])}</h2>
      <p class="pdetail">{esc(who['detail'])} &middot; {esc(who['visit'])}</p>
    </div>
    <div class="pright">{timing}
      <span class="chip mono">{esc(case_id)}</span>
    </div>
  </div>
  {banner}
  <div class="cols">
    <div class="card">
      <div class="chead">Ambient dictation transcript</div>
      <div class="cbody"><p class="note">{highlight(note, spans)}</p>{caveat}</div>
    </div>
    <div class="card">
      <div class="chead">Structured clinical data
        <span class="tagline">MediExtract</span></div>
      <div class="cbody fields">
{field_rows(payload)}
      </div>
    </div>
  </div>
</section>"""


STYLE = """
:root { --ink:#16191d; --muted:#5b6672; --line:#d8dee6; --bg:#f4f5f7; --panel:#fff;
        --brand:#0f6d68; --brand-soft:#e6f2f1;
        --ok:#0a6b3d; --ok-bg:#e6f4ec; --danger:#9b1c1c; --danger-bg:#fdeaea;
        --held:#8a4b00; --held-bg:#fdf1e2; --none:#5b6672; --none-bg:#f1f3f6;
        --q-medication:#dbe9ff; --q-dose:#ffe9c9; --q-frequency:#d9f2e4; --q-allergy:#f6dcef; }
* { box-sizing:border-box; }
body { margin:0; font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
       color:var(--ink); background:var(--bg); }
.topbar { background:var(--brand); color:#fff; padding:11px 22px; display:flex;
          justify-content:space-between; align-items:baseline; }
.topbar .t { font-size:15px; font-weight:600; letter-spacing:-.01em; }
.topbar .u { font-size:12.5px; opacity:.9; }
.synthetic { background:#fff8e1; border-bottom:1px solid #f0d9a0; color:#6b4c00;
             padding:6px 22px; font-size:12.5px; }
.app { display:flex; align-items:flex-start; }
.app > input { position:absolute; opacity:0; pointer-events:none; }
nav { width:210px; flex:0 0 210px; background:var(--panel); border-right:1px solid var(--line);
      padding:12px 0; min-height:calc(100vh - 78px); }
nav label { display:block; padding:9px 16px; font-size:13px; cursor:pointer;
            border-left:3px solid transparent; }
nav label:hover { background:var(--bg); }
nav .badge { display:block; font-size:11px; color:var(--muted); margin-top:2px; }
main { flex:1; min-width:0; padding:20px 24px 60px; }
.panel { display:none; }
.pbanner { background:var(--panel); border:1px solid var(--line); border-radius:10px;
           padding:16px 18px; display:flex; justify-content:space-between;
           align-items:flex-start; margin-bottom:14px; }
.pbanner h2 { margin:0 0 3px; font-size:19px; letter-spacing:-.01em; }
.pdetail { margin:0; color:var(--muted); font-size:13px; }
.pright { text-align:right; }
.chip { display:inline-block; border:1px solid var(--line); border-radius:20px;
        padding:2px 10px; font-size:11.5px; color:var(--muted); margin-left:6px;
        font-variant-numeric:tabular-nums; }
.chip.mono { font-family:ui-monospace,SFMono-Regular,Menlo,monospace; }
.chip.over { border-color:#e0b878; background:var(--held-bg); color:var(--held); }
.alert { background:var(--danger-bg); border:1px solid #f3c6c6; border-left:4px solid var(--danger);
         color:var(--danger); border-radius:8px; padding:10px 14px; margin:0 0 14px;
         font-size:13.5px; }
.cols { display:grid; grid-template-columns:1fr 1fr; gap:16px; align-items:start; }
.card { background:var(--panel); border:1px solid var(--line); border-radius:10px;
        overflow:hidden; }
.chead { background:var(--bg); padding:11px 16px; font-size:11.5px; font-weight:600;
         text-transform:uppercase; letter-spacing:.06em; color:var(--muted);
         border-bottom:1px solid var(--line); display:flex; justify-content:space-between; }
.tagline { background:var(--brand); color:#fff; border-radius:10px; padding:1px 8px;
           font-size:10.5px; letter-spacing:.04em; }
.cbody { padding:14px 16px; }
.note { margin:0; white-space:pre-wrap; word-wrap:break-word; }
mark { padding:1px 0; border-radius:2px; background:#eee; }
mark.q-medication { background:var(--q-medication); }
mark.q-dose { background:var(--q-dose); }
mark.q-frequency { background:var(--q-frequency); }
mark.q-allergy { background:var(--q-allergy); }
.fields { padding:6px 16px 14px; }
.field { display:grid; grid-template-columns:96px 1fr auto; grid-template-areas:
         "label box badge" ". quote quote" ". why why";
         gap:4px 10px; padding:12px 0; border-top:1px solid var(--line); align-items:center; }
.field:first-child { border-top:0; }
.flabel { grid-area:label; font-size:11px; text-transform:uppercase; letter-spacing:.06em;
          color:var(--muted); font-weight:600; }
.fbox { grid-area:box; border:1px solid var(--line); border-radius:6px; padding:8px 10px;
        background:var(--bg); font-size:15px; }
.val { font-weight:600; }
.blank { color:var(--muted); font-style:italic; font-weight:400; }
.fbadge { grid-area:badge; font-size:11px; font-weight:600; border-radius:4px;
          padding:3px 8px; white-space:nowrap; }
.fquote { grid-area:quote; font-size:13px; color:var(--muted); }
.fwhy { grid-area:why; font-size:12px; color:var(--muted); }
q { color:var(--ink); }
.field.ok .fbadge { background:var(--ok-bg); color:var(--ok); }
.field.ok .fbox { border-color:#bfe0cd; }
.field.flagged .fbadge { background:var(--danger-bg); color:var(--danger); }
.field.flagged .fbox { border-color:#e79a9a; background:var(--danger-bg); }
.field.flagged .fwhy { color:var(--danger); }
.field.held .fbadge { background:var(--held-bg); color:var(--held); }
.field.none .fbadge { background:var(--none-bg); color:var(--none); }
.caveat { margin:10px 0 0; color:var(--muted); font-size:12.5px; }
footer { padding:0 24px 40px; color:var(--muted); font-size:12px; max-width:88ch; }
code { font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:12.5px; }
"""


def build(payload_paths: list[Path]) -> tuple[str, int]:
    """The page, and how many cases it renders.

    Same seam as `build_review.build`: one function that takes paths and returns
    HTML, so the tests never touch argparse or the filesystem layout.
    """
    usable = []
    for path in payload_paths:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("status") != "ok":
            continue
        note = note_text(payload)
        if note is None:
            continue
        usable.append((Path(payload["note"]).stem, note, payload))

    radios, labels, panels = [], [], []
    for index, (case_id, note, payload) in enumerate(usable):
        codes = [g["code"] for g in payload.get("gate", [])]
        flagged = sum(1 for c in codes if c.startswith("REVIEW"))
        verified = sum(1 for c in codes if c == "VERIFIED")
        badge = (f"{flagged} needs review" if flagged
                 else (f"{verified} verified" if verified else "nothing stated"))
        who = PATIENTS.get(case_id, UNKNOWN_PATIENT)
        checked = " checked" if index == 0 else ""
        ident = esc(case_id)
        radios.append(f'<input type="radio" name="case" id="r-{ident}"{checked}>')
        labels.append(f'<label for="r-{ident}">{esc(who["name"])}'
                      f'<span class="badge">{esc(badge)}</span></label>')
        panels.append(case_panel(case_id, note, payload))

    # Both the id attribute and the selector go through `esc`. build_review.py
    # escapes the attribute and interpolates the selector raw - harmless for
    # machine-generated ids, and not a difference worth inheriting.
    rules = "\n".join(
        f'#r-{esc(case_id)}:checked ~ main #p-{esc(case_id)} {{ display:block; }}\n'
        f'#r-{esc(case_id)}:checked ~ nav label[for="r-{esc(case_id)}"] '
        "{ background:var(--brand-soft); border-left-color:var(--brand); font-weight:600; }"
        for case_id, _, _ in usable)

    over_budget = sum(1 for _, _, pay in usable if pay.get("within_budget") is False)
    if not usable:
        timing_note = ""
    elif over_budget == 0:
        timing_note = (f"all {len(usable)} of these finished inside the 3,000 ms "
                       "budget.")
    else:
        timing_note = (f"{over_budget} of these {len(usable)} ran over the 3,000 ms "
                       "budget.")

    page = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy"
      content="default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<title>Polyclinic EHR &mdash; MediExtract demonstration</title>
<style>{STYLE}
{rules}
</style>
</head>
<body>
<div class="topbar">
  <span class="t">Polyclinic EHR &mdash; consultation module</span>
  <span class="u">Dr. Aisha &middot; Station 4</span>
</div>
<p class="synthetic"><strong>Synthetic demonstration data.</strong> Patient names and
identifiers on this page are invented for the demonstration and belong to no one. The dictation
text is from a public, de-identified research corpus. This is not a real patient record and not a
real hospital system.</p>
<div class="app">
{chr(10).join(radios)}
<nav>{chr(10).join(labels)}</nav>
<main>
{chr(10).join(panels)}
</main>
</div>
<footer>
  <p>Generated by <code>demo/build_ehr.py</code> from the JSON that
     <code>src/extract.py</code> wrote to stdout, and the dictation files those payloads name.
     Every value, quote and status on this page is the pipeline's own output; the status wording
     comes from <code>extract.DISPLAY</code>, so no badge here can describe a check the system
     does not perform.</p>
  <p>Static and offline: no script, no form, no event handler, and
     <code>default-src 'none'</code>. The case selector is CSS. Every interpolated string is
     escaped, which is why the corpus's <code>&lt;PII&gt;</code> placeholders appear as text.</p>
  <p><strong>On the timings.</strong> The chip on each chart is that one call's
     round trip, and {timing_note} Across the 67-case evaluation the median is
     1,293 ms and 66 of 67 finish inside the 3,000 ms budget, so there is a tail.
     The three-second rule is about how long the physician spends checking a
     highlight against a value, which is what the layout above is for &mdash; it is
     not a claim about API latency.</p>
  <p><strong>A blank is a result, not a gap.</strong> It means the pipeline could not find words
     in the dictation that justify the value, so it declined rather than guessed. A field marked
     <em>needs review</em> is different again: the pipeline is showing you what the model returned
     and refusing to stand behind it.</p>
</footer>
</body>
</html>
"""
    return page, len(usable)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("payloads", type=Path, nargs="*",
                        help="extract.py stdout payloads (default: demo/payloads/*.json)")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--debug", action="store_true")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    paths = args.payloads or sorted(PAYLOAD_DIR.glob("*.json"))
    missing = [str(p) for p in paths if not p.is_file()]
    if not paths or missing:
        print(f"no usable payloads: {missing or [str(PAYLOAD_DIR)]}", file=sys.stderr)
        return EXIT_UNUSABLE
    try:
        page, cases = build(list(paths))
        if not cases:
            print("no payload could be paired with its dictation file", file=sys.stderr)
            return EXIT_UNUSABLE
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(page, encoding="utf-8")
    except Exception as exc:
        print(f"ERROR ERR_INTERNAL (exit {EXIT_INTERNAL}): {type(exc).__name__}", file=sys.stderr)
        if args.debug:
            traceback.print_exc()
        return EXIT_INTERNAL
    if not args.quiet:
        print(f"{cases} cases, {len(page):,} bytes", file=sys.stderr)
    sys.stdout.write(f"{args.out}\n")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
