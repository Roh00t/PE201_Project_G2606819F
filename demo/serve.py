#!/usr/bin/env python3
"""The localhost console: a dictation box that runs the real pipeline.

    ./.venv/bin/python demo/serve.py                  # http://127.0.0.1:8000
    ./.venv/bin/python demo/serve.py --mock           # no API call, no spend
    ./.venv/bin/python demo/serve.py --port 8080 --max-calls 5

`demo/ehr.html` is *generated* from committed payloads, so it can only ever show
the cases that were already run. This serves that page unchanged at `/chart` and
adds one thing it cannot have: an empty field at `/` where a note is typed or
dictated, and a button that runs `src/extract.py` over whatever arrives.

**The backend is the CLI, not a copy of it.** `src/extract.py --note` takes a
path, so the posted text is written to a temporary file and the real pipeline is
executed as a subprocess. Nothing about extraction, grounding or gating is
reimplemented here, and the exit code the page reports is the one the pipeline
returned. The same holds for rendering: the fields come from
`build_ehr.field_rows` and the highlighting from `build_review`, so this page
cannot show a badge the static chart could not show.

**Deliberately scriptless, like the page it serves.** The round trip is a plain
HTML form POST and a freshly rendered page - no JavaScript, no fetch, no event
handler. `demo/ehr.html` is tested to carry none of those
(`tests/test_ehr.py`), and a console that needed them to demonstrate the same
pipeline would undercut the claim. The only CSP relaxation here is
`form-action 'self'`, because a form that may not submit is not a form.

**Routing is an allowlist, never the filesystem.** `.env` sits in the repo root
and holds the OpenRouter key; a static handler rooted here would serve it on
request, and `.gitignore` is not an HTTP control. Four routes exist and
everything else is a 404 that echoes nothing back.

Other deliberate limits: the socket binds loopback only, so a laptop on a shared
network is not offering subprocess execution to it; the subprocess is an argv
list with a timeout, never a shell; and the request body is capped at
`extract.MAX_NOTE_CHARS` before a process is spawned. Past that cap the server
decides nothing - an empty note, a hostile note or a 19,000-character note all
go to the pipeline's own input gate, which is the thing under demonstration.

The dictation is attacker-chosen at runtime here, which it never is on the
generated page, so every interpolated string goes through `build_review.esc`.

stdout: nothing. stderr: one line per request, plus the startup banner.
Exit codes: 0 clean shutdown; 2 the port is unusable; 6 internal error.
"""

import argparse
import json
import subprocess
import sys
import tempfile
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "evals"))
sys.path.insert(0, str(ROOT / "demo"))

# Reused rather than reimplemented, for the reason build_ehr.py gives: a second
# escaper, highlighter or badge vocabulary is a second thing to get wrong, and
# tests/test_ehr.py and tests/test_review.py already pin these.
from build_ehr import STYLE, field_rows  # noqa: E402
from build_review import esc, highlight, spans_for  # noqa: E402
import extract  # noqa: E402

EXIT_OK = 0
EXIT_PORT = 2
EXIT_INTERNAL = 6

HOST = "127.0.0.1"          # loopback, deliberately not configurable
DEFAULT_PORT = 8000
DEFAULT_MAX_CALLS = 25
DEFAULT_TIMEOUT_S = 90.0

CHART = ROOT / "demo" / "ehr.html"
EXTRACT_CLI = ROOT / "src" / "extract.py"

CSP = ("default-src 'none'; style-src 'unsafe-inline'; "
       "form-action 'self'; base-uri 'none'")

# What each pipeline exit code means to the person watching. The pairing of code
# to wording is the point of CLAUDE.md §3.3: a safe abstention (1) and a network
# failure (3) must not read the same on screen, because conflating them is how an
# evaluation's abstention rate gets inflated by its own outages.
OUTCOME = {
    extract.EXIT_OK: (
        "ok", "Extracted and verified",
        "Every value below was found in the dictation and matched to a quote."),
    extract.EXIT_BLANKED: (
        "held", "Extracted, with at least one field blanked",
        "The pipeline could not ground one or more values in the dictation, so it "
        "wiped them to null rather than returning a guess. That is the safe path, "
        "not a failure."),
    extract.EXIT_INPUT: (
        "none", "Dictation rejected before any model call",
        "The input gate refused this text, so no tokens were spent on it."),
    extract.EXIT_UPSTREAM: (
        "flagged", "The model endpoint could not be reached",
        "This is an infrastructure failure, not an abstention. Nothing was "
        "extracted and nothing should be read as a clinical result."),
    extract.EXIT_SCHEMA: (
        "flagged", "The reply did not satisfy the schema",
        "The model returned something the strict schema refused. No payload was "
        "produced."),
    extract.EXIT_BUDGET: (
        "flagged", "The spend ceiling stopped this call",
        "The run was refused on cost grounds before it could complete."),
    extract.EXIT_INTERNAL: (
        "flagged", "The pipeline hit an internal error",
        "Treat this as a defect in the system, not as a statement about the "
        "dictation."),
}
UNKNOWN_OUTCOME = ("flagged", "The pipeline exited with an unrecognised code",
                   "No payload on this page can be trusted.")


class Refusal(Exception):
    """A request the server declines before any pipeline run.

    Carries the HTTP status so routing stays a single expression, and never
    echoes the request back - a 404 that quotes the path it refused is a
    reflection surface.
    """

    def __init__(self, status: int, reason: str):
        super().__init__(reason)
        self.status = status
        self.reason = reason


class Console:
    """The server's whole mutable state: the settings and the call counter.

    Separated from the HTTP handler so `route` can be exercised without a
    socket, which is the same seam `build_ehr.build` uses.
    """

    def __init__(self, mock: bool = False, max_calls: int = DEFAULT_MAX_CALLS,
                 timeout_s: float = DEFAULT_TIMEOUT_S, python: str | None = None):
        self.mock = mock
        self.max_calls = max_calls
        self.timeout_s = timeout_s
        self.python = python or sys.executable
        self.calls_used = 0

    # -- the pipeline ----------------------------------------------------

    def run_pipeline(self, text: str) -> tuple[int, dict, str]:
        """Run `src/extract.py` over `text` and return (exit code, payload, stderr).

        The text reaches the pipeline as a file because that is the only input
        the CLI accepts. A fresh temporary directory per request means two
        concurrent requests cannot see each other's note.
        """
        if self.calls_used >= self.max_calls:
            raise Refusal(429, f"this server was started with a cap of "
                               f"{self.max_calls} pipeline runs and has used them all")
        self.calls_used += 1

        argv = [self.python, str(EXTRACT_CLI), "--note", None, "--json-only"]
        if self.mock:
            argv.append("--mock")

        with tempfile.TemporaryDirectory(prefix="mediextract-live-") as tmp:
            note_path = Path(tmp) / "dictation.txt"
            note_path.write_text(text, encoding="utf-8")
            argv[3] = str(note_path)
            try:
                proc = subprocess.run(argv, capture_output=True, text=True,
                                      cwd=str(ROOT), timeout=self.timeout_s)
            except subprocess.TimeoutExpired:
                return (extract.EXIT_UPSTREAM,
                        extract.error_envelope("ERR_UPSTREAM_TIMEOUT"),
                        f"the pipeline did not finish within {self.timeout_s:.0f}s")

        try:
            payload = json.loads(proc.stdout)
        except (json.JSONDecodeError, ValueError):
            # stdout is contracted to be one JSON document (CLAUDE.md §3.1). If it
            # is not, say so rather than rendering a chart from nothing.
            return (extract.EXIT_INTERNAL,
                    extract.error_envelope("ERR_STDOUT_NOT_JSON"),
                    "the pipeline's stdout was not a single JSON document")
        return proc.returncode, payload, proc.stderr

    def billable(self) -> bool:
        """Whether a run from here would actually cost money."""
        if self.mock:
            return False
        try:
            extract.load_api_key()
        except Exception:        # noqa: BLE001 - any failure means no usable key
            return False
        return True

    # -- routing ---------------------------------------------------------

    def route(self, method: str, target: str, body: bytes = b"",
              content_type: str = "") -> tuple[int, str, bytes]:
        """(status, content type, body) for one request. An allowlist, in full."""
        path = urllib.parse.urlsplit(target).path
        try:
            if method == "GET" and path == "/":
                return 200, "text/html; charset=utf-8", self.console_page().encode("utf-8")
            if method == "GET" and path == "/chart":
                return 200, "text/html; charset=utf-8", self.chart_bytes()
            if method == "GET" and path == "/health":
                return (200, "application/json",
                        json.dumps({"ok": True, "mock": self.mock,
                                    "calls_used": self.calls_used,
                                    "max_calls": self.max_calls}).encode("utf-8"))
            if method == "POST" and path == "/extract":
                return self.extract_response(body, content_type)
            if path in ("/", "/chart", "/health", "/extract"):
                raise Refusal(405, f"{path} does not accept {method}")
            raise Refusal(404, "no such route")
        except Refusal as refusal:
            return (refusal.status, "text/html; charset=utf-8",
                    self.console_page(refused=refusal).encode("utf-8"))

    def chart_bytes(self) -> bytes:
        """The generated chart, verbatim. Not rebuilt, not rewritten."""
        if not CHART.is_file():
            raise Refusal(404, "demo/ehr.html has not been generated yet - "
                               "run demo/build_ehr.py first")
        return CHART.read_bytes()

    def extract_response(self, body: bytes, content_type: str) -> tuple[int, str, bytes]:
        if content_type.split(";")[0].strip().lower() != "application/x-www-form-urlencoded":
            raise Refusal(415, "this endpoint takes the console's own form")
        if len(body) > extract.MAX_NOTE_CHARS * 4:
            # Mirrors extract.MAX_NOTE_BYTES. Refused here so an oversized body
            # never reaches a subprocess; every other judgement about the text
            # belongs to the pipeline's input gate.
            raise Refusal(413, f"a dictation is limited to "
                               f"{extract.MAX_NOTE_CHARS:,} characters")
        try:
            decoded = body.decode("utf-8")
        except UnicodeDecodeError:
            raise Refusal(400, "the form body was not valid UTF-8") from None
        fields = urllib.parse.parse_qs(decoded, keep_blank_values=True)
        text = (fields.get("dictation") or [""])[0]

        exit_code, payload, stderr = self.run_pipeline(text)
        page = self.console_page(dictation=text, exit_code=exit_code,
                                 payload=payload, stderr=stderr)
        return 200, "text/html; charset=utf-8", page.encode("utf-8")

    # -- rendering -------------------------------------------------------

    def result_section(self, dictation: str, exit_code: int, payload: dict,
                       stderr: str) -> str:
        state, headline, explain = OUTCOME.get(exit_code, UNKNOWN_OUTCOME)
        banner = (f'<div class="outcome {state}">'
                  f'<strong>{esc(headline)}</strong>'
                  f'<span class="chip mono">exit {exit_code}</span>'
                  f'<p>{esc(explain)}</p></div>')

        if payload.get("status") != "ok":
            # The human message lives on stderr by contract, and the envelope's
            # own `note` key is a temporary path, so neither is rendered: the
            # machine-readable code is what identifies the refusal.
            detail = esc(payload.get("code") or "no code returned")
            tail = esc((stderr or "").strip().splitlines()[-1]) if stderr.strip() else ""
            return (banner + '<div class="card"><div class="chead">Pipeline refusal</div>'
                    f'<div class="cbody"><p><code>{detail}</code></p>'
                    + (f'<p class="caveat">{tail}</p>' if tail else "")
                    + "</div></div>")

        spans, counts = spans_for(dictation, payload["extraction"])
        repeated = [f"{f} appears {n} times" for f, n in sorted(counts.items()) if n > 1]
        caveat = ('<p class="caveat">The gate checks that the quote is in the note, not '
                  f'which occurrence: {esc("; ".join(repeated))}.</p>') if repeated else ""

        flags = (payload.get("input_scan", {}) or {}).get("flags") or []
        codes = [g["code"] for g in payload.get("gate", [])]
        flagged = sum(1 for c in codes if c.startswith("REVIEW"))
        alert = (f'<p class="alert">Input scan flagged this dictation: '
                 f'<strong>{esc(", ".join(flags))}</strong>. {flagged} field(s) were '
                 "downgraded to review rather than accepted.</p>") if flags else ""

        latency = (payload.get("latency_ms", {}) or {}).get("total")
        budget = payload.get("latency_budget_ms")
        if isinstance(latency, (int, float)):
            over = payload.get("within_budget") is False
            tail = (f" &mdash; over the {budget:,.0f} ms budget"
                    if over and isinstance(budget, (int, float)) else "")
            timing = f'<span class="{"chip over" if over else "chip"}">model call {latency:,.0f} ms{tail}</span>'
        else:
            timing = ""

        return f"""{banner}{alert}
  <div class="cols">
    <div class="card">
      <div class="chead">Dictation, as the gates read it {timing}</div>
      <div class="cbody"><p class="note">{highlight(dictation, spans)}</p>{caveat}</div>
    </div>
    <div class="card">
      <div class="chead">Structured clinical data
        <span class="tagline">MediExtract</span></div>
      <div class="cbody fields">
{field_rows(payload)}
      </div>
    </div>
  </div>
  <details class="raw">
    <summary>The payload this page was drawn from</summary>
    <pre>{esc(json.dumps(payload, indent=2, ensure_ascii=False))}</pre>
  </details>"""

    def console_page(self, dictation: str = "", exit_code: int | None = None,
                     payload: dict | None = None, stderr: str = "",
                     refused: Refusal | None = None) -> str:
        if refused is not None:
            result = ('<div class="outcome flagged">'
                      f'<strong>Request declined</strong>'
                      f'<span class="chip mono">HTTP {refused.status}</span>'
                      f'<p>{esc(refused.reason)}.</p></div>')
        elif exit_code is None or payload is None:
            result = ""
        else:
            result = self.result_section(dictation, exit_code, payload, stderr)

        if self.mock:
            mode = ('<p class="synthetic"><strong>Mock mode.</strong> No request leaves this '
                    'machine and nothing is spent. The values below are a canned reply fed '
                    'through the real gates, so the gating is genuine and the extraction is '
                    'not.</p>')
        elif not self.billable():
            mode = ('<p class="synthetic"><strong>No API key found.</strong> '
                    '<code>OPENROUTER_API_KEY</code> is set neither in the environment nor in '
                    '<code>.env</code>, so a run will stop at the pipeline\'s own key check '
                    'and report exit 6. Add a key, or restart with <code>--mock</code> to '
                    'exercise the gates offline.</p>')
        else:
            mode = (f'<p class="synthetic"><strong>Live.</strong> Each run is a real, billed '
                    f'call to <code>{esc(extract.MODEL)}</code>, recorded in the project '
                    f'ledger. This server will run the pipeline at most '
                    f'{self.max_calls} times; {self.calls_used} used so far.</p>')

        return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="{CSP}">
<title>Live dictation &mdash; MediExtract</title>
<style>{STYLE}
.wrap {{ max-width:1180px; margin:0 auto; padding:20px 24px 60px; }}
.dictate {{ background:var(--panel); border:1px solid var(--line); border-radius:10px;
            padding:16px 18px; margin-bottom:16px; }}
.dictate h1 {{ margin:0 0 4px; font-size:19px; letter-spacing:-.01em; }}
.dictate p.hint {{ margin:0 0 12px; color:var(--muted); font-size:13px; }}
textarea {{ width:100%; min-height:132px; resize:vertical; padding:11px 12px;
            border:1px solid var(--line); border-radius:8px; background:var(--bg);
            font:15px/1.55 inherit; color:var(--ink); }}
textarea:focus {{ outline:2px solid var(--brand); outline-offset:1px; background:#fff; }}
.actions {{ display:flex; align-items:center; gap:12px; margin-top:12px; }}
button {{ background:var(--brand); color:#fff; border:0; border-radius:8px;
          padding:10px 18px; font-size:14px; font-weight:600; cursor:pointer; }}
button:hover {{ background:#0c5955; }}
.actions a {{ color:var(--muted); font-size:13px; }}
.outcome {{ border:1px solid var(--line); border-left-width:4px; border-radius:8px;
            padding:11px 15px; margin:0 0 14px; background:var(--panel); }}
.outcome p {{ margin:4px 0 0; font-size:13px; color:var(--muted); }}
.outcome.ok {{ border-left-color:var(--ok); background:var(--ok-bg); }}
.outcome.held {{ border-left-color:var(--held); background:var(--held-bg); }}
.outcome.flagged {{ border-left-color:var(--danger); background:var(--danger-bg); }}
.outcome.none {{ border-left-color:var(--none); background:var(--none-bg); }}
.raw {{ margin-top:16px; font-size:12.5px; color:var(--muted); }}
.raw pre {{ background:var(--panel); border:1px solid var(--line); border-radius:8px;
            padding:12px 14px; overflow-x:auto; max-height:360px; }}
</style>
</head>
<body>
<div class="topbar">
  <span class="t">Polyclinic EHR &mdash; live dictation</span>
  <span class="u">Dr. Aisha &middot; Station 4</span>
</div>
{mode}
<div class="wrap">
  <form class="dictate" method="post" action="/extract">
    <h1>Consultation note</h1>
    <p class="hint">Type or dictate the consultation, then run it. The pipeline reads this
       text exactly as it arrives &mdash; it is never cleaned, corrected or reformatted
       first.</p>
    <textarea name="dictation" autofocus
      placeholder="Dictate or type the consultation note here.">{esc(dictation)}</textarea>
    <div class="actions">
      <button type="submit">Run MediExtract</button>
      <a href="/">Clear</a>
      <a href="/chart">Recorded cases &rarr;</a>
    </div>
  </form>
{result}
</div>
<footer>
  <p>This page runs <code>src/extract.py</code> as a subprocess over the text in the box and
     renders the JSON it writes to stdout. The fields, badges and quote highlights come from
     <code>demo/build_ehr.py</code> and <code>demo/build_review.py</code> &mdash; the same code
     that draws the recorded cases &mdash; so nothing can appear here that could not appear
     there, and the status wording is <code>extract.DISPLAY</code>'s alone.</p>
  <p>No script, no event handler, no remote origin: the round trip is a form POST and a fresh
     page. The server binds <code>127.0.0.1</code> only, routes from a four-entry allowlist
     rather than the filesystem, and passes your text to the pipeline untouched.</p>
</footer>
</body>
</html>
"""


def make_handler(console: Console):
    """A handler class bound to one Console.

    `BaseHTTPRequestHandler` is instantiated per request by the server, so the
    shared state has to be closed over rather than passed in.
    """

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        # The default banner reports the Python version to anyone who asks.
        server_version = "MediExtract-demo"
        sys_version = ""

        def log_message(self, fmt, *args):
            # CLAUDE.md §3.2: telemetry goes to stderr, never stdout.
            sys.stderr.write(f"[serve] {self.address_string()} {fmt % args}\n")

        def respond(self, status: int, content_type: str, body: bytes):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Security-Policy", CSP)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def read_body(self) -> bytes:
            """The body, bounded. A declared length over the cap is refused
            without reading it, so an oversized POST costs no memory."""
            raw = self.headers.get("Content-Length")
            if raw is None:
                return b""
            try:
                length = int(raw)
            except ValueError:
                return b""
            if length < 0:
                return b""
            if length > extract.MAX_NOTE_BYTES:
                return b"x" * (extract.MAX_NOTE_BYTES + 1)
            return self.rfile.read(length)

        def handle_one(self):
            try:
                body = self.read_body() if self.command == "POST" else b""
                method = "GET" if self.command == "HEAD" else self.command
                status, content_type, page = console.route(
                    method, self.path, body,
                    self.headers.get("Content-Type", ""))
            except Exception:                                   # noqa: BLE001
                traceback.print_exc(file=sys.stderr)
                status, content_type = 500, "text/plain; charset=utf-8"
                page = b"internal error; see the server's stderr\n"
            self.respond(status, content_type, page)

        do_GET = handle_one
        do_HEAD = handle_one
        do_POST = handle_one

    return Handler


def serve(console: Console, port: int = DEFAULT_PORT) -> ThreadingHTTPServer:
    """A bound, unstarted server. Loopback only, by construction."""
    return ThreadingHTTPServer((HOST, port), make_handler(console))


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--mock", action="store_true",
                        help="canned reply through the real gates; no call, no spend")
    parser.add_argument("--max-calls", type=int, default=DEFAULT_MAX_CALLS,
                        help="pipeline runs this process will allow (default: %(default)s)")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S,
                        help="seconds before a pipeline run is abandoned")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    console = Console(mock=args.mock, max_calls=args.max_calls, timeout_s=args.timeout)
    try:
        httpd = serve(console, args.port)
    except OSError as exc:
        print(f"cannot bind {HOST}:{args.port} ({exc.strerror or exc}). "
              f"Another server may already be running; try --port.", file=sys.stderr)
        return EXIT_PORT

    mode = "mock" if console.mock else ("live" if console.billable() else "no API key")
    print(f"MediExtract console on http://{HOST}:{args.port}  [{mode}]\n"
          f"  /        the dictation box\n"
          f"  /chart   the recorded cases (demo/ehr.html, served unchanged)\n"
          f"Ctrl-C to stop.", file=sys.stderr)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.", file=sys.stderr)
    finally:
        httpd.server_close()
    return EXIT_OK


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:                                           # noqa: BLE001
        traceback.print_exc(file=sys.stderr)
        sys.exit(EXIT_INTERNAL)
