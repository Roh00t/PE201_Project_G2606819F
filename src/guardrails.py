"""Deterministic safety gates. No model calls in this file, ever.

The proposal states the headline rule as

    assert evidence in source_text

and it is implemented below as an explicit `if`, never as an `assert`
statement: `python -O` strips assert statements, which would silently switch
the gate off. The same holds for every check in this file, and
tests/test_guardrails.py fails the build if an `assert` statement appears
anywhere under src/.

Per field, in order:

1. verify_evidence - the evidence span must be a verbatim substring of the
   dictation. The labelling tool (evals/label_gold_v1.py) imports this exact
   function, so gold labels and model output face the same gate.
2. verify_value - the value must be grounded in that evidence. The evidence
   gate alone proves a quote is real, not that the value derived from it is:
   `evidence="metformin"` with `value="Warfarin 10 mg"` passes gate 1.
     medication, allergy : every word/number of the value is in the evidence
     frequency           : the value is a phrase of the evidence, or a
                           recognised equivalent of one (bd -> twice daily)
     dose                : every number and unit matches the evidence
                           (15 mg vs 50 mg, mg vs mcg, no invented unit)

A field failing either gate is overwritten to BLANK, value and evidence
both, so a fabricated quote is never carried forward, and it carries an
ABSTAIN_* code saying why. DISPLAY turns the code into the text a physician
reads; display text never goes into a data field.

Input side, scan_input() inspects the note without ever modifying it: the
physician's dictation is not "cleaned" to make a check pass.
  - Encoding anomalies (bidirectional controls, invisible characters, and
    homoglyphs - letters that only look like the ones she dictated) force
    a safe abstention: every field is wiped, and extract.py does not call
    the model at all. What the physician sees and what the model would read
    must be the same text; when they may not be, nothing is extracted.
  - Known prompt-injection phrasings downgrade every surviving `found`
    field to `unsure`, so nothing reaches the physician pre-verified. That
    is a tripwire for known phrasings, not a defence against paraphrase;
    the defences are the absence of tools, the gates, and the physician
    reading the quoted span.

`near_miss` stays: a span that matches only after whitespace/case
normalisation is still blanked, but counted separately, so strict and
normalised matching are both reported.
"""

import re
import unicodedata
from dataclasses import dataclass, field as dataclass_field
from decimal import Decimal, InvalidOperation

from schema import BLANK, CRITICAL_FIELDS, ClinicalExtraction, ExtractedField, Status

PASS = "pass"
BLANKED = "blanked"
SKIPPED = "skipped"  # model claimed not_stated; nothing to verify

# ---------------------------------------------------------------------------
# Field-level outcome codes and what the physician sees for each
# ---------------------------------------------------------------------------

VERIFIED = "VERIFIED"
NOT_STATED = "NOT_STATED"
REVIEW_MODEL_UNSURE = "REVIEW_MODEL_UNSURE"
REVIEW_INJECTION_PATTERN = "REVIEW_INJECTION_PATTERN"
ABSTAIN_UNGROUNDED = "ABSTAIN_UNGROUNDED"
ABSTAIN_NEAR_MISS = "ABSTAIN_NEAR_MISS"
ABSTAIN_NO_EVIDENCE = "ABSTAIN_NO_EVIDENCE"
ABSTAIN_INCOHERENT = "ABSTAIN_INCOHERENT"
ABSTAIN_VALUE_UNGROUNDED = "ABSTAIN_VALUE_UNGROUNDED"
ABSTAIN_NUMERIC_MISMATCH = "ABSTAIN_NUMERIC_MISMATCH"
ABSTAIN_UNIT_MISMATCH = "ABSTAIN_UNIT_MISMATCH"
ABSTAIN_ENCODING_ANOMALY = "ABSTAIN_ENCODING_ANOMALY"

# The "BLANK (Abstained: ...)" wording is the Colab prototype's own
# convention, kept so the two artifacts read the same to a physician.
DISPLAY = {
    VERIFIED: "VERIFIED",
    NOT_STATED: "Not stated",
    REVIEW_MODEL_UNSURE: "REVIEW (Model unsure)",
    REVIEW_INJECTION_PATTERN: "REVIEW (Injection pattern in note)",
    ABSTAIN_UNGROUNDED: "BLANK (Abstained: Ungrounded)",
    ABSTAIN_NEAR_MISS: "BLANK (Abstained: Ungrounded - quote differs in spacing or case)",
    ABSTAIN_NO_EVIDENCE: "BLANK (Abstained: No quote given)",
    ABSTAIN_INCOHERENT: "BLANK (Abstained: Contradictory output)",
    ABSTAIN_VALUE_UNGROUNDED: "BLANK (Abstained: Value not in the quote)",
    ABSTAIN_NUMERIC_MISMATCH: "BLANK (Abstained: Number differs from dictation)",
    ABSTAIN_UNIT_MISMATCH: "BLANK (Abstained: Unit differs from dictation)",
    ABSTAIN_ENCODING_ANOMALY: "BLANK (Abstained: Hidden or look-alike characters in note)",
}


@dataclass
class GateResult:
    field: str
    outcome: str          # pass | blanked | skipped
    reason: str
    near_miss: bool = False
    code: str = ""

    @property
    def ok(self) -> bool:
        return self.outcome != BLANKED

    @property
    def display(self) -> str:
        return DISPLAY.get(self.code, self.code)


def _normalise(text: str) -> str:
    """Collapse the differences a paraphrasing model tends to introduce."""
    text = unicodedata.normalize("NFKC", text)
    return re.sub(r"\s+", " ", text).strip().casefold()


# ---------------------------------------------------------------------------
# Gate 1: the evidence span is verbatim
# ---------------------------------------------------------------------------

def verify_evidence(name: str, field: ExtractedField, source_text: str) -> GateResult:
    """Gate one field against the dictation it came from."""
    if field.status is Status.NOT_STATED:
        if field.evidence != BLANK or field.value != BLANK:
            return GateResult(
                name, BLANKED,
                "status=not_stated but value/evidence non-empty (incoherent output)",
                code=ABSTAIN_INCOHERENT,
            )
        return GateResult(name, SKIPPED, "not stated in dictation", code=NOT_STATED)

    if field.evidence == BLANK:
        return GateResult(
            name, BLANKED, f"status={field.status.value} with no evidence span",
            code=ABSTAIN_NO_EVIDENCE,
        )

    if field.evidence in source_text:
        return GateResult(
            name, PASS, "evidence span found verbatim",
            code=VERIFIED if field.status is Status.FOUND else REVIEW_MODEL_UNSURE,
        )

    near = _normalise(field.evidence) in _normalise(source_text)
    return GateResult(
        name, BLANKED,
        "evidence span matches only after whitespace/case normalisation"
        if near else "evidence span NOT present in dictation (fabricated)",
        near_miss=near,
        code=ABSTAIN_NEAR_MISS if near else ABSTAIN_UNGROUNDED,
    )


# ---------------------------------------------------------------------------
# Gate 2: the value is grounded in the (already verified) evidence
# ---------------------------------------------------------------------------

# Letters and numbers as separate tokens, so "250mg" -> ["250", "mg"] and
# "Pan-D" -> ["pan", "d"]: punctuation and spacing never decide a match.
_TOKEN = re.compile(r"[^\W\d_]+|\d+(?:\.\d+)?")


def _tokens(text: str) -> list[str]:
    return _TOKEN.findall(unicodedata.normalize("NFKC", text).casefold())


def _norm_phrase(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).casefold()
    text = re.sub(r"\s*-\s*", "-", text)
    text = re.sub(r"[^\w\s-]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


# A dosing regimen written as digits and hyphens: 1-0-1, 1-1-1-1.
_REGIMEN = re.compile(r"(?<![\d-])\d(?:-\d){2,3}(?![\d-])")


def _phrase_in(phrase: str, evidence_norm: str) -> bool:
    """Whole-phrase containment. Regimens compare as whole units, so
    "1-1-1" is not found inside "1-1-1-1"."""
    if _REGIMEN.fullmatch(phrase):
        return phrase in _REGIMEN.findall(evidence_norm)
    return re.search(rf"(?<![\w-]){re.escape(phrase)}(?![\w-])", evidence_norm) is not None


# Groups of dictation shorthand that mean the same schedule. A value from
# one group is accepted only when the evidence contains a member of that
# same group. Bare "daily" is deliberately absent: it sits inside "twice
# daily" and would let "once daily" through.
FREQUENCY_EQUIVALENTS = (
    ("od", "qd", "once daily", "once a day", "1-0-0", "0-1-0", "0-0-1"),
    ("bd", "bid", "twice daily", "twice a day", "two times a day", "1-0-1"),
    ("tds", "tid", "three times daily", "three times a day", "thrice daily",
     "thrice a day", "1-1-1"),
    ("qid", "qds", "four times daily", "four times a day", "1-1-1-1"),
    ("hs", "qhs", "at night", "at bedtime", "nightly", "0-0-1"),
    ("prn", "sos", "as needed", "as required", "when required", "when needed"),
)


def _frequency_equivalent(value: str, evidence: str) -> bool:
    value_norm, evidence_norm = _norm_phrase(value), _norm_phrase(evidence)
    for group in FREQUENCY_EQUIVALENTS:
        if value_norm in group and any(_phrase_in(member, evidence_norm) for member in group):
            return True
    return False


_UNIT_ALIASES = {
    "mg": "mg", "mgs": "mg", "milligram": "mg", "milligrams": "mg",
    "mcg": "mcg", "μg": "mcg", "ug": "mcg", "microgram": "mcg", "micrograms": "mcg",
    "g": "g", "gm": "g", "gms": "g", "gram": "g", "grams": "g",
    "ml": "ml", "millilitre": "ml", "millilitres": "ml",
    "milliliter": "ml", "milliliters": "ml",
    "iu": "iu", "unit": "iu", "units": "iu",
    "%": "%",
}
_UNIT_PATTERN = "|".join(
    re.escape(u) for u in sorted(_UNIT_ALIASES, key=len, reverse=True)
)
# A number (1,000 / 0.5 / .5 / 500) optionally followed by a unit. The
# lookbehind keeps "B12" and "v2" from yielding numbers.
_QUANTITY = re.compile(
    rf"(?<![\w.])(\d{{1,3}}(?:,\d{{3}})+(?:\.\d+)?|\d+(?:\.\d+)?|\.\d+)"
    rf"\s*({_UNIT_PATTERN})?(?![a-z])"
)


def _quantities(text: str) -> list[tuple[Decimal, str | None]]:
    """Every (number, canonical unit or None) in the text. After NFKC, the
    micro sign U+00B5 has become Greek mu U+03BC, which is the alias above."""
    found = []
    for number, unit in _QUANTITY.findall(unicodedata.normalize("NFKC", text).casefold()):
        try:
            amount = Decimal(number.replace(",", ""))
        except InvalidOperation:
            continue
        found.append((amount, _UNIT_ALIASES[unit] if unit else None))
    return found


def _fmt(amount: Decimal) -> str:
    return format(amount.normalize(), "f")


def _check_dose(value: str, evidence: str) -> GateResult | None:
    value_q, evidence_q = _quantities(value), _quantities(evidence)
    if not value_q:
        return GateResult(
            "dose", BLANKED,
            "dose value has no numeric strength (numbers written as words are not verified)",
            code=ABSTAIN_NUMERIC_MISMATCH,
        )
    evidence_numbers = {amount for amount, _ in evidence_q}
    for amount, unit in value_q:
        if amount not in evidence_numbers:
            return GateResult(
                "dose", BLANKED,
                f"dose number {_fmt(amount)} does not appear in the evidence",
                code=ABSTAIN_NUMERIC_MISMATCH,
            )
        dictated_units = {u for a, u in evidence_q if a == amount}
        if unit is None and None not in dictated_units:
            return GateResult(
                "dose", BLANKED,
                f"unit {sorted(dictated_units)} was dictated but dropped from the value",
                code=ABSTAIN_UNIT_MISMATCH,
            )
        if unit is not None and unit not in dictated_units:
            detail = (
                "added a unit that was not dictated"
                if dictated_units == {None}
                else f"unit {unit} differs from dictated "
                     f"{sorted(u for u in dictated_units if u)}"
            )
            return GateResult("dose", BLANKED, f"dose {detail}", code=ABSTAIN_UNIT_MISMATCH)
    return None


def verify_value(name: str, field: ExtractedField) -> GateResult | None:
    """Gate 2. Returns None when the value is grounded in the evidence,
    otherwise the blanking result. Only meaningful after verify_evidence
    passed, so the evidence itself is already known to be verbatim."""
    if not field.value.strip():
        return GateResult(
            name, BLANKED, f"status={field.status.value} but the value is empty",
            code=ABSTAIN_INCOHERENT,
        )

    if name == "dose":
        return _check_dose(field.value, field.evidence)

    if name == "frequency":
        if _phrase_in(_norm_phrase(field.value), _norm_phrase(field.evidence)):
            return None
        if _frequency_equivalent(field.value, field.evidence):
            return None
        return GateResult(
            name, BLANKED,
            "frequency value is neither a phrase of the evidence nor an equivalent of one",
            code=ABSTAIN_VALUE_UNGROUNDED,
        )

    missing = sorted(set(_tokens(field.value)) - set(_tokens(field.evidence)))
    if missing:
        return GateResult(
            name, BLANKED,
            f"{name} value has words not in the evidence: {', '.join(missing)}",
            code=ABSTAIN_VALUE_UNGROUNDED,
        )
    return None


# ---------------------------------------------------------------------------
# Input side: reject what is not a note, flag what looks like an injection
# ---------------------------------------------------------------------------

# The largest note in the Eka Care split is 9,450 characters.
MAX_NOTE_CHARS = 20_000

# C0 control characters other than tab, newline, form feed and carriage
# return. A dictated note has none; a binary file has plenty.
_CONTROL = re.compile("[\x00-\x08\x0b\x0e-\x1f\x7f]")


class InputRejected(ValueError):
    """The input is not a note this system should process at all."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def check_input(text: str) -> None:
    """Raise InputRejected for input that must not reach the model."""
    if not text.strip():
        raise InputRejected("ERR_INPUT_EMPTY", "the note is empty")
    if len(text) > MAX_NOTE_CHARS:
        raise InputRejected(
            "ERR_INPUT_TOO_LARGE",
            f"the note is {len(text):,} characters; the limit is {MAX_NOTE_CHARS:,}",
        )
    control = _CONTROL.search(text)
    if control:
        raise InputRejected(
            "ERR_INPUT_BINARY",
            f"control character U+{ord(control.group()):04X} at offset "
            f"{control.start()} - this is not a text note",
        )


# Each pattern had zero hits across all 156 Eka Care notes and the local
# synthetic notes when adopted (2026-09-18). Re-measure before adding one:
# a tripwire that fires on ordinary dictation trains the reader to ignore it.
INJECTION_PATTERNS = {
    "special_token": re.compile(
        r"<\|[a-z_]{2,40}\|>|\[/?INST\]|<</?SYS>>|<(?:start|end)_of_turn>", re.I),
    "delimiter_collision": re.compile(r"</?\s*note\b[^>]*>", re.I),
    "role_label": re.compile(r"(?im)^[ \t]*(?:system|assistant|developer)[ \t]*:"),
    "ignore_instructions": re.compile(
        r"\b(?:ignore|disregard|forget|override)\b[^.\n]{0,40}?"
        r"\b(?:previous|prior|above|earlier|preceding|all|any)\b[^.\n]{0,40}?"
        r"\b(?:instructions?|prompts?|rules?|directions?)\b", re.I),
    "system_prompt": re.compile(r"\bsystem\s+prompt\b", re.I),
    "persona_switch": re.compile(
        r"\byou\s+are\s+now\s+(?:an?\s+|the\s+|in\s+)?(?:ai|assistant|model|"
        r"language\s+model|llm|chatbot|dan|jailbroken|unrestricted|developer\s+mode)\b",
        re.I),
    "as_an_ai": re.compile(r"\bas\s+an?\s+(?:ai|language\s+model|llm)\b", re.I),
    "jailbreak_terms": re.compile(
        r"\b(?:do\s+anything\s+now|developer\s+mode|jailbreak(?:ed)?)\b", re.I),
    "new_instructions": re.compile(
        r"\b(?:new|updated|revised|additional)\s+instructions?\s*:", re.I),
    "addressed_to_ai": re.compile(
        r"\bnote\s+to\s+(?:the\s+)?(?:ai|model|assistant|llm|extractor)\b", re.I),
}


# Encoding anomalies. None of the 156 Eka Care notes or the local synthetic
# notes contains a format (Cf) character, and the only non-ASCII character
# in the 67 Latin-script notes is a degree sign (measured 2026-09-18).
#
# Bidirectional controls: text that renders in a different order than it
# reads (Trojan Source), including the LRM/RLM/ALM marks.
BIDI_CONTROLS = frozenset(
    [chr(c) for c in range(0x202A, 0x202F)] + [chr(c) for c in range(0x2066, 0x206A)]
    + ["\u200e", "\u200f", "\u061c"]
)
# Characters that render as nothing but are not format characters.
FILLERS = frozenset(["\u115f", "\u1160", "\u3164", "\uffa0"])
ENCODING_ANOMALIES = ("bidi_control", "invisible_char", "homoglyph")

# Letters that belong in English clinical dictation although not ASCII:
# accented Latin (Meniere with accents), the micro sign and Greek mu (ug).
_LATIN_COMPATIBLE = ("LATIN ", "MICRO SIGN", "GREEK SMALL LETTER MU")
_LETTER_RUN = re.compile(r"[^\W\d_]+")
# Scripts with letters that pass for Latin ones. A word mixing Latin with
# Devanagari (Hindi or Marathi code-mixing) is not a look-alike and is not
# flagged here; non-Latin notes are out of scope for other reasons.
_CONFUSABLE_SCRIPTS = frozenset({"CYRILLIC", "GREEK", "ARMENIAN", "CHEROKEE", "COPTIC", "LISU"})
_LETTER_OR_DIGIT = ("Lu", "Ll", "Lt", "Lm", "Lo", "Nd")


def _invisible(ch: str) -> bool:
    """Zero-width and other format characters (category Cf: ZWSP, ZWJ,
    ZWNJ, word joiner, soft hyphen, BOM, invisible operators, Unicode tag
    characters), Hangul fillers, and variation selectors (steganography)."""
    code = ord(ch)
    return (
        (unicodedata.category(ch) == "Cf" and ch not in BIDI_CONTROLS)
        or ch in FILLERS
        or 0xFE00 <= code <= 0xFE0F
        or 0xE0100 <= code <= 0xE01EF
    )


def _script(ch: str) -> str:
    name = unicodedata.name(ch, "")
    if name.startswith(_LATIN_COMPATIBLE):
        return "LATIN"
    return name.split(" ", 1)[0] or "UNKNOWN"


def has_homoglyph(text: str) -> bool:
    """A letter or digit that only looks like the one dictated:
      - a compatibility form of a single ASCII letter or digit (full-width
        letters, mathematical bold/italic letters, the Kelvin sign);
      - a word mixing Latin letters with letters of a look-alike script
        (a Cyrillic 'e' inside metformin).
    Only letters count: superscripts, fractions and the degree sign are not
    letters, so 'mg/m2' written with a superscript and '37 degrees' pass."""
    for ch in text:
        if ch.isascii():
            continue
        folded = unicodedata.normalize("NFKC", ch)
        if (len(folded) == 1 and folded.isascii() and folded.isalnum()
                and unicodedata.category(ch) in _LETTER_OR_DIGIT):
            return True
    for run in _LETTER_RUN.findall(text):
        scripts = {_script(ch) for ch in run if unicodedata.category(ch).startswith("L")}
        if "LATIN" in scripts and scripts & _CONFUSABLE_SCRIPTS:
            return True
    return False


def encoding_flags(text: str) -> list[str]:
    flags = []
    if any(ch in BIDI_CONTROLS for ch in text):
        flags.append("bidi_control")
    if any(_invisible(ch) for ch in text):
        flags.append("invisible_char")
    if has_homoglyph(text):
        flags.append("homoglyph")
    return flags


@dataclass
class InputScan:
    flags: list[str] = dataclass_field(default_factory=list)

    @property
    def encoding_anomaly(self) -> bool:
        """Forced safe abstention: nothing is extracted from this note."""
        return any(flag in ENCODING_ANOMALIES for flag in self.flags)

    @property
    def review_required(self) -> bool:
        return bool(self.flags)


def scan_input(text: str) -> InputScan:
    """Encoding anomalies first, then injection phrasings. Never modifies
    the text. The phrasings are also checked on the NFKC form, so a
    full-width 'ignore' is caught even if the homoglyph check were
    ever relaxed."""
    folded = unicodedata.normalize("NFKC", text)
    return InputScan(encoding_flags(text) + [
        name for name, pattern in INJECTION_PATTERNS.items()
        if pattern.search(text) or pattern.search(folded)
    ])


def wiped_extraction() -> ClinicalExtraction:
    """All four fields wiped. What a note with an encoding anomaly yields,
    without any model call."""
    return ClinicalExtraction(**{
        name: ExtractedField(evidence=BLANK, value=BLANK, status=Status.UNSURE)
        for name in CRITICAL_FIELDS
    })


# ---------------------------------------------------------------------------
# Putting it together
# ---------------------------------------------------------------------------

def apply_gates(
    extraction: ClinicalExtraction,
    source_text: str,
    enabled: bool = True,
    scan: InputScan | None = None,
) -> tuple[ClinicalExtraction, list[GateResult]]:
    """Run every gate and return the (possibly blanked) extraction.

    `enabled=False` reports the same verdicts but leaves the payload
    untouched - that is the Week 3 abstention test: same run, gate off,
    count how many false fields would have reached the physician.

    An encoding anomaly in the note wipes every field whatever the model
    said: the note is not what it looks like, so no quote from it can be
    verified by eye.
    """
    if scan is not None and scan.encoding_anomaly:
        reason = f"encoding anomaly in note ({', '.join(scan.flags)}): nothing is extracted from it"
        results = [GateResult(name, BLANKED, reason, code=ABSTAIN_ENCODING_ANOMALY)
                   for name in CRITICAL_FIELDS]
        return (extraction if not enabled else wiped_extraction()), results

    results = []
    for name in CRITICAL_FIELDS:
        field = getattr(extraction, name)
        result = verify_evidence(name, field, source_text)
        if result.outcome == PASS:
            result = verify_value(name, field) or result
        if result.outcome == PASS and scan is not None and scan.review_required:
            result = GateResult(
                name, PASS,
                f"injection pattern in note ({', '.join(scan.flags)}) - verify by hand",
                code=REVIEW_INJECTION_PATTERN,
            )
        results.append(result)

    if not enabled:
        return extraction, results

    gated = extraction.model_copy(deep=True)
    for result in results:
        current = getattr(gated, result.field)
        if result.outcome == BLANKED:
            setattr(gated, result.field, current.blank())
        elif result.code == REVIEW_INJECTION_PATTERN and current.status is Status.FOUND:
            setattr(gated, result.field, current.model_copy(update={"status": Status.UNSURE}))
    return gated, results
