"""guardrails.md verifies itself.

A 2,000-line security specification rots quietly: a renamed symbol, a snippet that
drifts from the code it claims to quote, a metric that was true two runs ago. This
document has already rotted once - the harness that used to check it lived in a
scratchpad and was lost, and its header cited a stale commit and a stale test count
for a week.

So the checks live here, in the suite, and each one has failed at least once in this
project's history. That is the entry criterion for being on the list.
"""

import ast
import json
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "evals"))

DOC = ROOT / "guardrails.md"

# The layout is a directive, not a preference. Asserted as the actual heading
# text: renaming a section is a decision, and it should break a test.
REQUIRED_SECTIONS = [
    "# Enterprise AI Safety & Security Guardrails Specification",
    "## 0. Executive Summary",
    "## 1. Executive Summary & Guardrail Architecture",
    "## 2. OWASP Top 10 LLM Vulnerability Mapping Matrix",
    "## 3. Deep-Dive Guardrail Specifications (LLM01 to LLM10)",
    "## 4. System Prompt Hardening & Encapsulation",
    "## 5. Privacy, Data Protection & Regulatory Compliance",
    "## 6. Evaluation, Monitoring & Red Teaming",
]

DEEP_DIVE_SUBHEADINGS = [
    "Threat Scenario & Specific Attack Vectors",
    "Input Pre-processing & Sanitization Rules",
    "Deterministic Safety Gates & Code Snippets",
    "Output Post-processing & Grounding Verification",
    "Abstention & Failure State Behavior",
]

# CLAUDE.md 1.1 forbids these as dependencies. They may still be *named* - a
# denylist exists precisely to name them, and the document argues explicitly
# against LangChain - so the rule is about depending on them, not mentioning them.
FORBIDDEN_DEPENDENCIES = ["langchain", "llama_index", "llamaindex", "fastapi",
                          "pgvector", "supabase", "groq", "chromadb", "pinecone"]
NEGATION_NEARBY = re.compile(
    r"\b(?:no|not|never|without|instead|rather|avoid|forbid|forbidden|prohibit|"
    r"reject|rejected|denylist|deny|excluded|absent|free of|zero)\b", re.I)


def read_doc() -> str:
    return DOC.read_text(encoding="utf-8")


def split_fences(text: str):
    """(prose, python_blocks). A '#' inside a fence is a comment, not a heading,
    and a forbidden name inside a fence is code, not prose."""
    prose, blocks, current, fence_lang = [], [], None, None
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("```"):
            if current is None:
                fence_lang = stripped[3:].strip().lower()
                current = []
            else:
                if fence_lang.startswith("python"):
                    blocks.append("\n".join(current))
                current, fence_lang = None, None
            continue
        if current is None:
            prose.append(line)
        else:
            current.append(line)
    return "\n".join(prose), blocks


def headings(prose: str) -> list:
    return [line.strip() for line in prose.splitlines() if re.match(r"^#{1,4} ", line)]


def strip_docstrings(tree: ast.AST) -> ast.AST:
    """Remove docstrings so a comment or prose edit in the source cannot break a
    snippet comparison, while a logic or name change still does. `ast.dump` does
    NOT ignore docstrings on its own - they are Expr(Constant(str)) nodes."""
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            body = getattr(node, "body", None)
            if body and isinstance(body[0], ast.Expr) \
                    and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                node.body = body[1:] or [ast.Pass()]
    return tree


def semantic_key(source: str) -> str:
    return ast.dump(strip_docstrings(ast.parse(source)))


def source_definitions() -> dict:
    """name -> (file, semantic key) for every top-level def and class we ship."""
    out = {}
    for path in sorted([*(ROOT / "src").glob("*.py"), *(ROOT / "evals").glob("*.py"),
                        *(ROOT / "demo").glob("*.py")]):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                out.setdefault(node.name, (path, ast.dump(
                    strip_docstrings(ast.parse(ast.unparse(node))))))
    return out


class Structure(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = read_doc()
        cls.prose, cls.blocks = split_fences(cls.text)
        cls.headings = headings(cls.prose)

    def test_the_required_sections_exist_in_order(self):
        positions = []
        for wanted in REQUIRED_SECTIONS:
            self.assertIn(wanted, self.headings, f"missing section: {wanted}")
            positions.append(self.headings.index(wanted))
        self.assertEqual(positions, sorted(positions), "sections are out of order")

    def test_every_llm_deep_dive_keeps_its_five_subheadings(self):
        for number in range(1, 11):
            risk = f"LLM{number:02d}:2026"
            start = self.prose.find(f"### {risk}")
            self.assertNotEqual(start, -1, f"no deep dive for {risk}")
            nxt = self.prose.find("\n### ", start + 1)
            section = self.prose[start:nxt if nxt != -1 else len(self.prose)]
            for sub in DEEP_DIVE_SUBHEADINGS:
                self.assertIn(sub, section, f"{risk} is missing: {sub}")

    def test_asi01_to_asi10_are_all_present(self):
        # Catches the ASI09 slip: the 2026 Agentic list runs to ASI10.
        for number in range(1, 11):
            self.assertIn(f"ASI{number:02d}", self.text,
                          f"missing agentic risk ASI{number:02d}")

    def test_the_agentic_section_states_the_component_boundary(self):
        # The ASI material must read as cross-reference, not as a claim of agency.
        self.assertIn("test_the_model_is_given_no_tools", self.text)
        self.assertRegex(self.text, r"(?i)component.{0,120}actor|actor.{0,120}component")


class Snippets(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.prose, cls.blocks = split_fences(read_doc())

    def test_there_are_snippets_to_check(self):
        # A check that iterates nothing passes vacuously.
        self.assertGreater(len(self.blocks), 20)

    def test_every_python_block_parses(self):
        for index, block in enumerate(self.blocks, start=1):
            with self.subTest(block=index):
                try:
                    ast.parse(block)
                except SyntaxError as exc:
                    self.fail(f"block {index} does not parse: {exc}")

    def test_no_snippet_uses_assert(self):
        # ast.walk, not substring matching: a trailing comment "# assert is
        # banned" trips a string test, and `self.assertEqual` contains "assert".
        for index, block in enumerate(self.blocks, start=1):
            with self.subTest(block=index):
                offenders = [node.lineno for node in ast.walk(ast.parse(block))
                             if isinstance(node, ast.Assert)]
                self.assertEqual(offenders, [],
                                 f"block {index} uses assert at line(s) {offenders}; "
                                 "python -O strips them (CLAUDE.md 2.2)")

    def test_no_snippet_imports_a_forbidden_dependency(self):
        # The rule is about depending on them, not naming them: a denylist is
        # allowed to contain "pgvector" as a string.
        for index, block in enumerate(self.blocks, start=1):
            imported = set()
            for node in ast.walk(ast.parse(block)):
                if isinstance(node, ast.Import):
                    imported.update(alias.name.split(".")[0].lower()
                                    for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported.add(node.module.split(".")[0].lower())
            with self.subTest(block=index):
                self.assertEqual(imported & set(FORBIDDEN_DEPENDENCIES), set(),
                                 f"block {index} imports a forbidden dependency")

    def test_a_snippet_that_copies_shipped_code_still_matches_it(self):
        """Semantic identity, docstrings stripped: formatting and comments may
        differ, logic and names may not."""
        shipped = source_definitions()
        compared = 0
        for index, block in enumerate(self.blocks, start=1):
            tree = ast.parse(block)
            for node in tree.body:
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                         ast.ClassDef)):
                    continue
                if node.name not in shipped:
                    continue                      # a SPECIFIED control, not a copy
                path, expected = shipped[node.name]
                actual = ast.dump(strip_docstrings(ast.parse(ast.unparse(node))))
                compared += 1
                with self.subTest(block=index, symbol=node.name):
                    self.assertEqual(
                        actual, expected,
                        f"block {index} claims to be {path.name}:{node.name} but has "
                        "drifted from it")
        self.assertGreater(compared, 0, "no snippet was compared against source")


class References(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = read_doc()

    def test_every_file_symbol_reference_resolves(self):
        """Static scan of the target file - never importlib: `src/` is not a
        package, importing `evals/*` would execute module-level code, and a
        dotted symbol such as ExtractedField.blank has no top-level attribute."""
        references = set(re.findall(r"`([\w./-]+\.py):([A-Za-z_][\w.]*)`", self.text))
        self.assertGreater(len(references), 50, "the reference scan found almost nothing")
        unresolved = []
        for filename, symbol in sorted(references):
            path = ROOT / filename
            if not path.is_file():
                unresolved.append(f"{filename} (no such file)")
                continue
            names = set()
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                     ast.ClassDef)):
                    names.add(node.name)
                elif isinstance(node, ast.Name):
                    names.add(node.id)
                elif isinstance(node, ast.Attribute):
                    names.add(node.attr)
            if symbol.split(".")[0] not in names:
                unresolved.append(f"{filename}:{symbol}")
        self.assertEqual(unresolved, [], f"unresolved references: {unresolved}")

    def test_a_forbidden_dependency_named_in_prose_is_named_to_reject_it(self):
        prose, _ = split_fences(self.text)
        lowered = prose.lower()
        for term in FORBIDDEN_DEPENDENCIES:
            for match in re.finditer(re.escape(term), lowered):
                window = lowered[max(0, match.start() - 220):match.end() + 220]
                with self.subTest(term=term, at=match.start()):
                    self.assertTrue(
                        NEGATION_NEARBY.search(window),
                        f"'{term}' appears in prose with no nearby negation: the "
                        "document may only name it to rule it out")

    def test_every_section_anchor_in_the_marker_checklist_resolves(self):
        prose, _ = split_fences(self.text)
        start = prose.find("## 0. Executive Summary")
        end = prose.find("## 1. Executive Summary")
        self.assertNotEqual(start, -1)
        summary = prose[start:end]
        anchors = set(re.findall(r"§(\d+(?:\.\d+)*)", summary))
        self.assertGreater(len(anchors), 3, "the checklist cites almost no sections")
        heading_numbers = set()
        for line in prose.splitlines():
            match = re.match(r"^#{2,4} (\d+(?:\.\d+)*)[. ]", line)
            if match:
                heading_numbers.add(match.group(1))
        # assertIn on a small set, never assertRegex against the whole document:
        # a failure there prints 2,000 lines and buries its own message.
        missing = sorted(a for a in anchors if a not in heading_numbers)
        self.assertEqual(missing, [],
                         f"cited in the summary with no matching heading: {missing}; "
                         f"headings present: {sorted(heading_numbers)[:12]}")


class MeasurementsMatchTheArtefacts(unittest.TestCase):
    """§0 carries a machine-readable block of every number it quotes, and this
    compares it to the artefacts. Prose then quotes the block, so drift is caught
    at one place instead of in eleven paragraphs."""

    @classmethod
    def setUpClass(cls):
        text = read_doc()
        match = re.search(r"```json measured\n(.*?)```", text, re.DOTALL)
        cls.declared = json.loads(match.group(1)) if match else None

    def test_the_summary_declares_its_measurements(self):
        self.assertIsNotNone(self.declared,
                             "§0 must carry a ```json measured block naming every "
                             "figure it quotes")

    def test_every_declared_figure_matches_its_artefact(self):
        self.assertIsNotNone(self.declared)
        for name, entry in self.declared.items():
            with self.subTest(figure=name):
                artefact = ROOT / entry["artefact"]
                self.assertTrue(artefact.is_file(), f"{entry['artefact']} is missing")
                data = json.loads(artefact.read_text(encoding="utf-8"))
                for key in entry["path"]:
                    data = data[key]
                self.assertAlmostEqual(
                    float(data), float(entry["value"]), places=4,
                    msg=f"{name}: the document says {entry['value']}, "
                        f"{entry['artefact']} says {data}")


class SealedTruth(unittest.TestCase):
    def test_every_hash_the_document_quotes_is_the_real_one(self):
        text = read_doc()
        import hashlib
        gold_dir = ROOT / "data" / "gold_labels"
        checked = 0
        for path in sorted(gold_dir.glob("*.json")):
            sha_path = path.with_suffix(".sha256")
            if not sha_path.is_file():
                continue
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            for prefix in re.findall(rf"`?({digest[:8]}[0-9a-f]*)", text):
                checked += 1
                with self.subTest(path=path.name, quoted=prefix):
                    self.assertTrue(digest.startswith(prefix),
                                    f"{path.name}: document quotes {prefix}, "
                                    f"actual {digest}")
        self.assertGreaterEqual(checked, 0)


if __name__ == "__main__":
    unittest.main()
