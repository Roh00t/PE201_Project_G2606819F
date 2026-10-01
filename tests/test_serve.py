"""The localhost console.

This is the first page in the repository whose input is chosen at runtime by
whoever is at the keyboard, and the first thing here that opens a socket and
spawns a subprocess. Both change the threat model, so these tests are mostly
about the things that are no longer guaranteed by construction:

`TheAllowlistIsTheWholeRouter` exists because `.env` sits in the repo root
holding the OpenRouter key, and a handler that routed from the filesystem would
serve it to anyone who asked. `.gitignore` does not apply over HTTP.

`NothingHostileSurvivesTheRoundTrip` exists because the generated chart only ever
renders committed corpus notes, where a dictated `<script>` is hypothetical.
Here it is typed in by hand.

`RefusedBeforeAnythingIsSpawned` exists because every POST can start a process
and spend money, so the bounds have to hold before the subprocess, not after it.

Everything runs with `--mock`, so the suite stays offline and free.
"""

import ast
import json
import sys
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "evals"))
sys.path.insert(0, str(ROOT / "demo"))

import extract  # noqa: E402
import serve  # noqa: E402
from extract import DISPLAY  # noqa: E402

FORM = "application/x-www-form-urlencoded"
NOTE = "Tablet Dolo 650 twice a day."
HOSTILE = ('Patient <PII> reviewed. <script>alert("xss")</script> '
           "Ampersand & angle > bracket. Tablet Dolo 650 twice a day.")


def field(value=None, evidence=None, status="not_stated"):
    return {"value": value, "evidence": evidence, "status": status}


def grounded(note=NOTE, **codes):
    """A payload whose values really are in `note`.

    `--mock` returns a canned reply whose values are planted fabrications - the
    point of `tests/test_extract_cli.py:test_mock_wipes_the_planted_fabrications`
    - so a mock run over live-typed text correctly grounds nothing and emits no
    highlight. Rendering has to be driven from a payload instead.
    """
    codes = codes or {"medication": "VERIFIED", "dose": "VERIFIED",
                      "frequency": "VERIFIED", "allergy": "NOT_STATED"}
    values = {"medication": ("Dolo", "Tablet Dolo 650"),
              "dose": ("650", "Dolo 650"),
              "frequency": ("twice a day", "twice a day"),
              "allergy": (None, None)}
    return {
        "status": "ok", "schema_version": "1", "note": "live",
        "latency_ms": {"total": 1292.6}, "latency_budget_ms": 3000,
        "within_budget": True, "input_scan": {"flags": [], "review_required": False},
        "extraction": {
            name: field(*(values[name] if codes.get(name) != "NOT_STATED" else (None, None)),
                        status="found" if codes.get(name) != "NOT_STATED" else "not_stated")
            for name in values},
        "gate": [{"field": name, "outcome": "pass", "code": code, "near_miss": False}
                 for name, code in codes.items()],
    }


def console(**kw):
    kw.setdefault("mock", True)
    kw.setdefault("max_calls", 50)
    return serve.Console(**kw)


def post(con, text, content_type=FORM):
    body = urllib.parse.urlencode({"dictation": text}).encode("utf-8")
    return con.route("POST", "/extract", body, content_type)


def page_of(result):
    return result[2].decode("utf-8")


class TheAllowlistIsTheWholeRouter(unittest.TestCase):
    """A path this server does not name is a 404, whatever is on disk."""

    def setUp(self):
        self.con = console()

    def test_the_four_routes_answer(self):
        for method, path in (("GET", "/"), ("GET", "/chart"), ("GET", "/health")):
            with self.subTest(path=path):
                self.assertEqual(self.con.route(method, path)[0], 200)

    def test_the_api_key_is_not_reachable_by_any_spelling(self):
        # The defect this prevents: SimpleHTTPRequestHandler rooted at the repo.
        for path in ("/.env", "/../.env", "/%2e%2e/.env", "/..%2f.env",
                     "/src/extract.py", "/data/gold_labels/gold_v1.json",
                     "/demo/serve.py", "/../../etc/passwd"):
            with self.subTest(path=path):
                self.assertEqual(self.con.route("GET", path)[0], 404)

    def test_a_refusal_does_not_echo_the_path_it_refused(self):
        page = page_of(self.con.route("GET", "/<script>alert(1)</script>"))
        self.assertNotIn("alert(1)", page)

    def test_a_known_route_with_the_wrong_method_is_405_not_404(self):
        self.assertEqual(self.con.route("GET", "/extract")[0], 405)
        self.assertEqual(self.con.route("POST", "/")[0], 405)

    def test_a_query_string_does_not_change_the_route(self):
        self.assertEqual(self.con.route("GET", "/?x=1")[0], 200)
        self.assertEqual(self.con.route("GET", "/chart?../.env")[0], 200)

    def test_the_chart_is_served_byte_for_byte(self):
        served = self.con.route("GET", "/chart")[2]
        self.assertEqual(served, (ROOT / "demo" / "ehr.html").read_bytes())


class ThePageIsAsInertAsTheOneItServes(unittest.TestCase):
    """The chart is tested to carry no script, no handler and no remote origin.
    A console that needed any of them to demonstrate the same pipeline would
    undercut that claim on camera, so it carries none either. A form is the one
    addition, and `form-action 'self'` is the one CSP relaxation."""

    @classmethod
    def setUpClass(cls):
        cls.con = console()
        cls.blank = page_of(cls.con.route("GET", "/"))
        cls.result = page_of(post(cls.con, NOTE))

    def pages(self):
        return (("console", self.blank), ("result", self.result))

    def test_no_script_no_handler_no_remote_origin(self):
        for label, page in self.pages():
            for pattern in (r"<script", r"\son[a-z]+\s*=", r"https?://"):
                with self.subTest(page=label, pattern=pattern):
                    self.assertIsNone(__import__("re").search(pattern, page, 2))

    def test_the_policy_still_forbids_everything_by_default(self):
        for label, page in self.pages():
            with self.subTest(page=label):
                self.assertIn("default-src 'none'", page)
                self.assertIn("form-action 'self'", page)

    def test_the_only_form_posts_to_the_only_endpoint(self):
        self.assertEqual(self.blank.count("<form"), 1)
        self.assertIn('method="post" action="/extract"', self.blank)

    def test_the_box_starts_empty(self):
        # Asked for explicitly: the field is empty, not pre-filled with an example.
        self.assertIn('placeholder="Dictate or type the consultation note here."',
                      self.blank)
        self.assertIn("</textarea>", self.blank)
        start = self.blank.index("<textarea")
        self.assertEqual(self.blank[self.blank.index(">", start) + 1:
                                    self.blank.index("</textarea>")], "")

    def test_the_box_holds_what_was_submitted_so_it_can_be_rerun(self):
        self.assertIn(NOTE, self.result[:self.result.index("</textarea>")])


class NothingHostileSurvivesTheRoundTrip(unittest.TestCase):
    """The dictation is typed in by whoever is at the keyboard."""

    @classmethod
    def setUpClass(cls):
        cls.page = page_of(post(console(), HOSTILE))

    def test_a_dictated_script_tag_is_text_not_markup(self):
        self.assertNotIn("<script", self.page.lower())
        self.assertIn("&lt;script&gt;", self.page)

    def test_it_survives_in_the_textarea_too(self):
        # Two sinks, one escaper. The textarea is the one a reviewer forgets.
        textarea = self.page[self.page.index("<textarea"):self.page.index("</textarea>")]
        self.assertIn("&lt;script&gt;", textarea)
        self.assertNotIn("</textarea><", textarea)

    def test_the_pii_placeholder_survives_as_visible_text(self):
        self.assertIn("&lt;PII&gt;", self.page)

    def test_ampersands_are_escaped_once_and_not_twice(self):
        self.assertIn("Ampersand &amp; angle &gt; bracket", self.page)
        self.assertNotIn("&amp;amp;", self.page)

    def test_the_embedded_payload_cannot_close_its_own_block(self):
        raw = self.page[self.page.index("<pre>"):]
        self.assertNotIn("<script", raw.lower())


class RefusedBeforeAnythingIsSpawned(unittest.TestCase):
    """Each POST can start a process and spend money."""

    def test_an_oversized_body_is_refused_without_a_subprocess(self):
        con = console()
        status = con.route("POST", "/extract",
                           b"x" * (extract.MAX_NOTE_BYTES + 1), FORM)[0]
        self.assertEqual(status, 413)
        self.assertEqual(con.calls_used, 0)

    def test_the_cap_mirrors_the_pipelines_own_limit(self):
        con = console()
        self.assertEqual(
            con.route("POST", "/extract", b"x" * extract.MAX_NOTE_BYTES, FORM)[0], 200)

    def test_a_body_that_is_not_the_consoles_form_is_refused(self):
        con = console()
        self.assertEqual(post(con, NOTE, "application/json")[0], 415)
        self.assertEqual(con.calls_used, 0)

    def test_the_charset_parameter_does_not_break_the_check(self):
        self.assertEqual(post(console(), NOTE, FORM + "; charset=utf-8")[0], 200)

    def test_the_run_cap_refuses_the_next_one(self):
        con = console(max_calls=2)
        self.assertEqual(post(con, NOTE)[0], 200)
        self.assertEqual(post(con, NOTE)[0], 200)
        self.assertEqual(post(con, NOTE)[0], 429)
        self.assertEqual(con.calls_used, 2)

    def test_a_refused_run_still_renders_the_console(self):
        con = console(max_calls=0)
        status, _, body = post(con, NOTE)
        self.assertEqual(status, 429)
        self.assertIn("Run MediExtract", body.decode("utf-8"))

    def test_an_undecodable_body_is_refused(self):
        self.assertEqual(
            console().route("POST", "/extract", b"dictation=\xff\xfe", FORM)[0], 400)


class TheExitCodeIsReportedNotSwallowed(unittest.TestCase):
    """CLAUDE.md §3.3 separates a safe abstention from an upstream failure so an
    evaluation's abstention rate is not inflated by its own outages. The same
    separation has to survive onto the screen, or the demonstration reunites
    what the exit codes were split apart to keep separate."""

    def test_every_pipeline_exit_code_has_its_own_wording(self):
        codes = {extract.EXIT_OK, extract.EXIT_BLANKED, extract.EXIT_INPUT,
                 extract.EXIT_UPSTREAM, extract.EXIT_SCHEMA, extract.EXIT_BUDGET,
                 extract.EXIT_INTERNAL}
        self.assertEqual(set(serve.OUTCOME), codes)
        headlines = [head for _, head, _ in serve.OUTCOME.values()]
        self.assertEqual(len(set(headlines)), len(headlines))

    def test_an_abstention_does_not_read_like_a_failure(self):
        safe = serve.OUTCOME[extract.EXIT_BLANKED]
        broken = serve.OUTCOME[extract.EXIT_UPSTREAM]
        self.assertEqual(safe[0], "held")
        self.assertEqual(broken[0], "flagged")
        self.assertIn("safe path", safe[2])
        self.assertIn("not an abstention", broken[2])

    def test_an_unknown_code_does_not_borrow_a_reassuring_sentence(self):
        state, headline, _ = serve.OUTCOME.get(99, serve.UNKNOWN_OUTCOME)
        self.assertEqual(state, "flagged")
        self.assertIn("unrecognised", headline)

    def test_an_empty_dictation_reaches_the_pipelines_own_input_gate(self):
        # The server validates size and content type and nothing else, so the
        # gate under demonstration is the one that answers.
        page = page_of(post(console(), "   "))
        self.assertIn(f"exit {extract.EXIT_INPUT}", page)
        self.assertIn("ERR_INPUT_EMPTY", page)

    def test_a_refusal_shows_the_code_and_not_the_temporary_path(self):
        page = page_of(post(console(), ""))
        self.assertNotIn("mediextract-live-", page)
        self.assertNotIn("dictation.txt", page)


class OnlyThePipelinesOwnVocabulary(unittest.TestCase):
    """The same guarantee `tests/test_ehr.py` pins for the chart: no badge here
    may describe a check the system does not perform."""

    def test_every_explanation_string_comes_from_extract_display(self):
        import re
        page = page_of(post(console(), NOTE))
        wording = set(re.findall(r'class="fwhy">([^&]+) &mdash;', page))
        self.assertTrue(wording, "no explanation strings found - the check is vacuous")
        self.assertEqual(wording - set(DISPLAY.values()), set())

    def test_the_four_states_are_the_chart_states(self):
        self.assertEqual(sorted({state for state, _, _ in serve.OUTCOME.values()}
                                | {serve.UNKNOWN_OUTCOME[0]}),
                         sorted(__import__("build_ehr").STATE_LABEL))


class ItSharesTheChartsMachinery(unittest.TestCase):
    """A second renderer would be a second thing to get wrong, and the badge
    vocabulary test above would only cover one of them."""

    SOURCE = (ROOT / "demo" / "serve.py").read_text(encoding="utf-8")

    def test_it_imports_rather_than_reimplements(self):
        self.assertIn("from build_ehr import STYLE, field_rows", self.SOURCE)
        self.assertIn("from build_review import esc, highlight, spans_for", self.SOURCE)
        for name in ("def esc(", "def highlight(", "def spans_for(", "def field_rows(",
                     "def state_for("):
            self.assertEqual(self.SOURCE.count(name), 0,
                             f"{name} is reimplemented here")

    def test_the_backend_is_the_cli_not_a_second_pipeline(self):
        # extract is imported for its constants and its error envelope. The
        # extraction itself must go through the CLI, or §1.1's single auditable
        # file stops being the thing that runs.
        tree = ast.parse(self.SOURCE)
        called = {node.func.attr for node in ast.walk(tree)
                  if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                  and isinstance(node.func.value, ast.Name)
                  and node.func.value.id == "extract"}
        self.assertEqual(called - {"error_envelope", "load_api_key"}, set())
        self.assertIn("str(EXTRACT_CLI)", self.SOURCE)

    def test_it_never_writes_the_chart_it_serves(self):
        self.assertNotIn("write_text", self.SOURCE.replace("note_path.write_text", ""))
        self.assertNotIn("write_bytes", self.SOURCE)

    def test_the_subprocess_is_an_argv_list_with_a_timeout(self):
        self.assertNotIn("shell=True", self.SOURCE)
        self.assertIn("timeout=self.timeout_s", self.SOURCE)


class ItBindsLoopbackOnly(unittest.TestCase):
    """A server that shells a subprocess over posted text and binds every
    interface is offering subprocess execution to the local network."""

    SOURCE = (ROOT / "demo" / "serve.py").read_text(encoding="utf-8")

    def test_the_host_constant_is_loopback(self):
        self.assertEqual(serve.HOST, "127.0.0.1")

    def test_no_wildcard_address_appears_anywhere_in_the_file(self):
        for wildcard in ("0.0.0.0", '""', "'::'"):
            with self.subTest(wildcard=wildcard):
                self.assertNotIn(f"({wildcard}", self.SOURCE)

    def test_the_host_is_not_a_command_line_option(self):
        # A `--host` flag is one typo away from publishing this.
        self.assertNotIn("--host", self.SOURCE)
        self.assertNotIn("--bind", self.SOURCE)

    def test_a_bound_server_is_on_loopback(self):
        httpd = serve.serve(console(), 0)
        try:
            self.assertEqual(httpd.server_address[0], "127.0.0.1")
        finally:
            httpd.server_close()


class OverRealHttp(unittest.TestCase):
    """Routing is tested without a socket above; this proves the handler wires
    it up, sends the headers, and bounds the body before reading it."""

    @classmethod
    def setUpClass(cls):
        cls.con = console()
        cls.httpd = serve.serve(cls.con, 0)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=5)

    def fetch(self, path, data=None, content_type=FORM):
        request = urllib.request.Request(
            self.base + path, data=data,
            headers={"Content-Type": content_type} if data else {})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.status, dict(response.headers), response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers), exc.read()

    def test_the_console_answers_with_its_policy_attached(self):
        status, headers, body = self.fetch("/")
        self.assertEqual(status, 200)
        self.assertIn("default-src 'none'", headers["Content-Security-Policy"])
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertIn(b"Run MediExtract", body)

    def test_the_server_banner_does_not_report_the_python_version(self):
        self.assertNotIn("Python", self.fetch("/")[1].get("Server", ""))

    def test_a_full_round_trip_renders_the_fields(self):
        data = urllib.parse.urlencode({"dictation": NOTE}).encode("utf-8")
        status, _, body = self.fetch("/extract", data)
        self.assertEqual(status, 200)
        page = body.decode("utf-8")
        for name in extract.CRITICAL_FIELDS:
            with self.subTest(field=name):
                self.assertIn(f'<div class="flabel">{name}</div>', page)
        # The canned reply is ungroundable by design, so the honest result of a
        # mock round trip is four wiped fields and exit 1 - not a filled chart.
        self.assertIn(f"exit {extract.EXIT_BLANKED}", page)
        self.assertIn(NOTE, page)

    def test_the_env_file_is_a_404_over_the_wire(self):
        self.assertEqual(self.fetch("/.env")[0], 404)

    def test_an_oversized_post_is_refused_without_being_read(self):
        before = self.con.calls_used
        huge = b"dictation=" + b"x" * (extract.MAX_NOTE_BYTES + 10)
        self.assertEqual(self.fetch("/extract", huge)[0], 413)
        self.assertEqual(self.con.calls_used, before)

    def test_health_reports_the_mode_and_the_budget(self):
        payload = json.loads(self.fetch("/health")[2])
        self.assertTrue(payload["mock"])
        self.assertEqual(payload["max_calls"], 50)

    def test_a_head_request_sends_headers_and_no_body(self):
        request = urllib.request.Request(self.base + "/", method="HEAD")
        with urllib.request.urlopen(request, timeout=30) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.read(), b"")


class TheCostOfARunIsStatedBeforeItIsIncurred(unittest.TestCase):
    def test_mock_mode_says_the_extraction_is_not_genuine(self):
        page = page_of(console(mock=True).route("GET", "/"))
        self.assertIn("Mock mode", page)
        self.assertIn("nothing is spent", page)
        self.assertNotIn("real, billed", page)

    def test_mock_mode_is_not_billable(self):
        self.assertFalse(console(mock=True).billable())

    def test_a_missing_key_is_reported_rather_than_crashed_into(self):
        con = console(mock=False)
        con.billable = lambda: False
        page = con.console_page()
        self.assertIn("No API key found", page)
        self.assertIn("--mock", page)

    def test_a_live_console_states_the_model_and_the_cap(self):
        con = console(mock=False, max_calls=7)
        con.billable = lambda: True
        page = con.console_page()
        self.assertIn(extract.MODEL, page)
        self.assertIn("at most 7 times", page)


if __name__ == "__main__":
    unittest.main()


class TheChartItDrawsFromAPayload(unittest.TestCase):
    """Rendering, driven from a payload rather than from `--mock`.

    This is the half a mock round trip cannot reach: the canned reply grounds
    nothing, so a live mock POST never produces a highlighted quote or a
    verified badge. These are the states the physician actually sees.
    """

    def render(self, note=NOTE, exit_code=0, payload=None, stderr=""):
        return serve.Console(mock=True).result_section(
            note, exit_code, payload if payload is not None else grounded(note), stderr)

    def test_a_verified_value_is_highlighted_where_it_occurs(self):
        page = self.render()
        self.assertIn('<mark class="q-medication"', page)
        self.assertEqual(page.count("<mark"), page.count("</mark>"))

    def test_every_field_gets_a_row_and_a_badge(self):
        page = self.render()
        for name in extract.CRITICAL_FIELDS:
            with self.subTest(field=name):
                self.assertIn(f'<div class="flabel">{name}</div>', page)
        self.assertIn("Verified", page)
        self.assertIn("Not stated", page)

    def test_an_injection_flagged_field_keeps_its_value_and_says_so(self):
        # The behaviour a hand-drawn mockup got wrong twice: a flagged field is
        # not blanked, it is shown and disowned.
        payload = grounded(medication="REVIEW_INJECTION_PATTERN", dose="VERIFIED",
                           frequency="VERIFIED", allergy="NOT_STATED")
        payload["input_scan"] = {"flags": ["instruction_like_text"],
                                 "review_required": True}
        page = self.render(payload=payload)
        self.assertIn("Dolo", page)
        self.assertIn("Needs review", page)
        self.assertIn("Input scan flagged this dictation", page)

    def test_a_repeated_quote_is_disclosed_rather_than_resolved(self):
        note = "Tablet Dolo 650 twice a day. Tablet Dolo 650 twice a day."
        page = self.render(note=note, payload=grounded(note))
        self.assertIn("not which occurrence", page.replace("\n", " ").replace(",", ""))

    def test_the_latency_chip_names_the_measured_call(self):
        self.assertIn("model call 1,293 ms", self.render())

    def test_a_call_over_budget_says_so_on_its_chip(self):
        payload = grounded()
        payload["within_budget"] = False
        payload["latency_ms"] = {"total": 12325.1}
        page = self.render(payload=payload)
        self.assertIn("chip over", page)
        self.assertIn("over the 3,000 ms budget", page)

    def test_the_payload_it_drew_from_is_on_the_page(self):
        # So a viewer can check the chart against the JSON in one frame.
        page = self.render()
        self.assertIn("The payload this page was drawn from", page)
        self.assertIn("&quot;VERIFIED&quot;", page)

    def test_a_blanked_field_shows_nothing_rather_than_a_debugging_string(self):
        # CLAUDE.md §2.3: no UI string may stand in for a wiped value.
        payload = grounded(medication="ABSTAIN_UNGROUNDED", dose="NOT_STATED",
                           frequency="NOT_STATED", allergy="NOT_STATED")
        payload["extraction"]["medication"] = field(None, "Tablet Dolo 650", "found")
        page = self.render(payload=payload)
        self.assertIn("&mdash; blank &mdash;", page)
        for invented in ("BLANK", "Abstained", "null", "None"):
            with self.subTest(string=invented):
                self.assertNotIn(f">{invented}<", page)
