#!/usr/bin/env python3
"""MediExtract - one dictated note in, four verified fields out.

    python src/extract.py --note gold/case_001.txt

The whole pipeline is this one file (CLAUDE.md 1.1): no framework, nothing
between the note and the API call that is not visible line by line. It is
arranged in sections, in the order data flows:

    1. Configuration   model, prices, SDK limits, routing, exit codes
    2. Schema          the four fields (the model's contract) and the gold
                       annotation schema the labelling tool uses
    3. Prompt          the system instruction and the offline mock response
    4. Gates           deterministic checks, no model calls: verbatim evidence,
                       value and number/unit grounding, input rejection,
                       encoding anomalies, injection phrasings
    5. Spend ledger    the hard spending ceiling
    6. Pipeline        read the note, one OpenRouter call, parse, gate
    7. CLI             one JSON document on stdout, everything else on stderr

evals/label_gold_v1.py imports verify_evidence from here, so gold labels and
model output face the identical verbatim gate; evals/run_ekacare.py and the
tests import from here too.

Transport: the OpenAI Python SDK pointed at OpenRouter
(https://openrouter.ai/api/v1), model google/gemini-2.5-flash, key in
OPENROUTER_API_KEY. Timeout, retries and the output-token cap are set on
the SDK, because its defaults (2 retries, 600 s read timeout) would let a
hung call stall a batch.

Useful flags:
    --mock              run the gates against a canned response; no API call,
                        no credit spent (the fixture contains deliberate
                        fabrications so the gates have something to catch)
    --no-gate           report gate verdicts but do not wipe anything.
                        This is the Week 3 abstention test.
    --budget-usd N      hard ceiling on total spend across runs (default 8.00,
                        or MEDIEXTRACT_BUDGET_USD). Checked before every call.

stdout carries exactly one JSON document of machine-checkable values: the
extraction payload (status "ok") or an error envelope (status "error", an
error code, data null). No human-readable text goes there - no display
labels, no gate reasons, no error messages. All of that - the gate table,
error messages, any stray print from a library - goes to stderr, because
stdout is redirected to stderr while the pipeline runs.

Exit codes (pinned by tests/test_extract_cli.py):
    0  every field verified, not stated, or routed to review
    1  at least one field wiped by a gate (including a forced abstention)
    2  input rejected: missing, unreadable, empty, oversized, binary or not
       UTF-8. argparse usage errors also exit 2, but print no envelope.
    3  upstream failure: timeout, unreachable, rate limited, auth, model or
       provider unavailable. There is no automatic fallback to another model.
    4  the model output failed the schema twice, or was truncated
    5  spend ceiling reached (the call was not made) or OpenRouter credit
       exhausted
    6  configuration or internal error

A crash can therefore never be mistaken for an abstention (exit 1), which
is what the batch evaluation counts.
"""

import argparse
import contextlib
import copy
import hashlib
import json
import math
import os
import re
import sys
import time
import traceback
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass, field as dataclass_field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


# ============================================================================
# 1. Configuration
# ============================================================================

ROOT = Path(__file__).resolve().parent.parent
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
MODEL = "google/gemini-2.5-flash"
OUTPUT_SCHEMA_VERSION = "mediextract.output.v3"

# USD per million tokens, by model, as listed by OpenRouter's public models
# API on 2026-09-18 (reasoning tokens bill at the output rate). A model not
# listed here cannot be called without --price-in-per-mtok and
# --price-out-per-mtok: the spend ceiling is only as honest as its prices.
PRICES_PER_MTOK = {
    "google/gemini-2.5-flash": (0.30, 2.50),
}

# The physician-facing budget from the persona work: verification must fit
# inside 3 seconds, so the extraction itself needs to land well under that.
LATENCY_BUDGET_MS = 3000

HTTP_TIMEOUT_S = 15.0
HTTP_MAX_RETRIES = 1                 # SDK default is 2; one retry = 2 attempts
MAX_OUTPUT_TOKENS = 1024             # about 250 are needed: 4x headroom
SCHEMA_ATTEMPTS = 2                  # one retry when the output fails the schema
# The largest note in the Eka Care split is 9,450 characters.
MAX_NOTE_CHARS = 20_000
MAX_NOTE_BYTES = MAX_NOTE_CHARS * 4  # UTF-8 worst case, checked before reading

# OpenRouter routing: only endpoints that honour every parameter we send
# (so the JSON schema is enforced, not ignored), and only providers whose
# data policy does not allow collecting the prompt. "allow" is accepted for
# public or synthetic notes only (--provider-data-collection).
PROVIDER_PREFERENCES = {"require_parameters": True, "data_collection": "deny"}
REASONING = {"effort": "none"}       # thinking costs latency we do not have

NOTE_TEMPLATE = "<note>\n{note}\n</note>"

EXIT_OK = 0
EXIT_BLANKED = 1
EXIT_INPUT = 2
EXIT_UPSTREAM = 3
EXIT_SCHEMA = 4
EXIT_BUDGET = 5
EXIT_INTERNAL = 6


# ============================================================================
# 2. Schema: the four fields, the strict response schema, the gold labels
# ============================================================================
# Pydantic schema for MediExtract.
#
# Four critical fields only. Every field carries three things:
#
#   value     - the normalised clinical fact (what goes in the chart)
#   evidence  - a VERBATIM span copied from the dictation, char-for-char
#   status    - found | not_stated | unsure
#
# The `evidence` field is what makes the deterministic safety gate in
# section 4 possible: if the span is not a literal substring of the
# dictation, the field is unverifiable and gets blanked.
#
# BLANK is the empty string rather than None inside the wire schema: the
# schema the model is constrained to stays free of nullable types and anyOf,
# which structured-output implementations translate unevenly. The payload
# extract.py prints converts BLANK to null ("hard wipe"), so a consumer never
# sees an empty string posing as a value.

BLANK = ""

CRITICAL_FIELDS = ("medication", "dose", "frequency", "allergy")

# What each field means. The single source of truth: extract.py puts this text
# in the Gemini system prompt, and evals/label_gold_v1.py shows the same text
# to the human labeller. If the two ever read different definitions, recall
# would measure the difference between the definitions, not the model.
FIELD_RULES = {
    "medication": (
        "The single most clinically significant medication prescribed or "
        "continued at this visit. Give the product NAME ONLY, never its "
        "strength: dictated 'Dolo 650' -> medication 'Dolo' and dose '650'. "
        "Brand names count ('Telma', 'Moxclav', 'Pan D'). If two are equally "
        "significant and there is no principled way to choose, use status "
        "unsure."
    ),
    "dose": (
        "The STRENGTH of that same medication exactly as dictated: '500', "
        "'500 mg', '0.5 mg'. Dictation often omits the unit - leave it omitted, "
        "do not infer it. A strength inside a product name counts ('Dolo 650' -> "
        "value '650'). A dose value always contains a digit - if the dictation "
        "gives no number, dose is not_stated rather than a word. A quantity per "
        "administration ('1 tablet', 'half') is NOT a dose; if only a quantity "
        "is given, dose is not_stated."
    ),
    "frequency": (
        "How often that same medication is taken, as dictated: 'twice daily', "
        "'TDS', '1-0-1', 'three times a day'. Timing relative to food ('after "
        "food') is not a frequency."
    ),
    "allergy": (
        "A drug or other substance the patient is allergic to. A denial "
        "('no known allergies') is not_stated, not an allergy."
    ),
}

SHARED_FIELD_RULE = (
    "dose and frequency always describe the medication reported in `medication`. "
    "If that medication has no dictated strength, dose is not_stated even when "
    "another medication in the note has one."
)


def render_field_rules(indent: str = "  ") -> str:
    """The rules as plain text, for a prompt or a terminal."""
    import textwrap

    width = 78 - len(indent)
    lines = []
    for name, rule in FIELD_RULES.items():
        wrapped = textwrap.wrap(rule, width - 13)
        lines.append(f"{indent}{name:<11}- {wrapped[0]}")
        lines.extend(f"{indent}{'':<13}{w}" for w in wrapped[1:])
    lines.append("")
    lines.extend(indent + w for w in textwrap.wrap(SHARED_FIELD_RULE, width))
    return "\n".join(lines)


class Status(str, Enum):
    """Why a field looks the way it does."""

    FOUND = "found"          # stated in the dictation, span captured
    NOT_STATED = "not_stated"  # the dictation is silent on this field
    UNSURE = "unsure"        # stated but ambiguous -> route to human review


class ExtractedField(BaseModel):
    """Field order here is the order Gemini emits, and that is deliberate.

    Evidence first: with thinking disabled, the token order IS the reasoning
    order, so the model must copy a span out of the dictation before it is
    allowed to commit to a value or a confidence. Asking for the value first
    invites it to decide the answer and then hunt for justification.

    Nothing is optional. A Pydantic default would drop the key from the
    `required` list in the generated Gemini schema, which lets the model
    silently omit `evidence` - exactly the field the whole design rests on.
    An explicit empty string is a claim; a missing key is an ambiguity.

    Unknown keys are rejected (extra="forbid"): a model that invents a
    fourth subfield has not followed the contract, and its output is not
    trusted in part.
    """

    model_config = ConfigDict(extra="forbid")

    evidence: str = Field(
        description=(
            "VERBATIM substring of the dictation supporting the value. Copy "
            "character for character - do not fix spelling, expand shorthand, "
            "change case or re-wrap whitespace. Empty string if status is "
            "not_stated."
        ),
    )
    value: str = Field(
        description=(
            "The clinical fact, lightly normalised (e.g. 'Metformin', '500 mg', "
            "'twice daily'). Empty string if status is not_stated."
        ),
    )
    status: Status = Field(description="found, not_stated, or unsure.")

    @property
    def is_blank(self) -> bool:
        return self.value == BLANK and self.evidence == BLANK

    def blank(self) -> "ExtractedField":
        """Return the field with value and evidence stripped.

        Status becomes `unsure` rather than `not_stated`: the dictation may
        well have stated the fact, we just could not verify it, so it is a
        human-review item and not a confident negative.
        """
        return ExtractedField(value=BLANK, evidence=BLANK, status=Status.UNSURE)


class ClinicalExtraction(BaseModel):
    """The whole payload the model is constrained to emit."""

    model_config = ConfigDict(extra="forbid")

    medication: ExtractedField
    dose: ExtractedField
    frequency: ExtractedField
    allergy: ExtractedField


def response_json_schema() -> dict:
    """ClinicalExtraction as a strict, self-contained JSON Schema for
    OpenRouter's `response_format: json_schema` with `strict: true`.

    Pydantic emits $defs/$ref; strict structured-output implementations
    want every object inline, every property required, and
    additionalProperties false. Built from the Pydantic model so the two
    can never disagree.
    """
    raw = ClinicalExtraction.model_json_schema()
    defs = raw.pop("$defs", {})

    def inline(node):
        if isinstance(node, dict):
            if "$ref" in node:
                return inline(copy.deepcopy(defs[node["$ref"].split("/")[-1]]))
            out = {k: inline(v) for k, v in node.items() if k not in ("title", "default")}
            if out.get("type") == "object" and "properties" in out:
                out["required"] = list(out["properties"])
                out["additionalProperties"] = False
            return out
        if isinstance(node, list):
            return [inline(item) for item in node]
        return node

    return inline(raw)


# ---------------------------------------------------------------------------
# Gold annotation schema (evaluation side, never sent to a model)
# ---------------------------------------------------------------------------
#
# Deliberately NOT the same type as ExtractedField above.
#
# ExtractedField is a wire schema: every key required, BLANK is the empty
# string, because Gemini's OpenAPI subset is happier that way and because a
# missing key there is a silent failure.
#
# GoldField is a human artefact. Here `None` is load-bearing and means
# "a human has not touched this yet", which must be distinguishable from
# "a human looked and the note does not state it". Collapsing those two
# would hand us 120 free not_stated labels the moment we generate the
# template, and every one of them would count as a correct abstention at
# scoring time. That is how a gold set silently lies.
#
# `to_extracted_field()` is the one bridge between the two worlds, so
# scoring.py compares like with like.



class GoldField(BaseModel):
    """One hand-labelled field. `status is None` means unlabelled."""

    value: Optional[str] = None
    evidence: Optional[str] = None
    status: Optional[Status] = None

    @property
    def is_labelled(self) -> bool:
        return self.status is not None

    def to_extracted_field(self) -> ExtractedField:
        """Project onto the wire type so the pipeline gate can judge it."""
        return ExtractedField(
            evidence=self.evidence or BLANK,
            value=self.value or BLANK,
            status=self.status or Status.UNSURE,
        )

    @model_validator(mode="after")
    def _coherent(self) -> "GoldField":
        if self.status is Status.NOT_STATED and (self.value or self.evidence):
            raise ValueError(
                "status=not_stated cannot carry a value or an evidence span"
            )
        if self.status is Status.FOUND and not self.evidence:
            raise ValueError("status=found requires a verbatim evidence span")
        return self


class GoldCase(BaseModel):
    """One note plus its four hand-labelled fields."""

    case_id: str
    source_text: str
    ground_truth: dict[str, GoldField]
    labeller: Optional[str] = None
    labelled_at: Optional[str] = None
    notes: Optional[str] = None

    @field_validator("ground_truth")
    @classmethod
    def _exactly_the_critical_fields(
        cls, v: dict[str, GoldField]
    ) -> dict[str, GoldField]:
        if set(v) != set(CRITICAL_FIELDS):
            missing = sorted(set(CRITICAL_FIELDS) - set(v))
            extra = sorted(set(v) - set(CRITICAL_FIELDS))
            raise ValueError(
                f"ground_truth keys must be exactly {list(CRITICAL_FIELDS)}"
                + (f"; missing {missing}" if missing else "")
                + (f"; unexpected {extra}" if extra else "")
            )
        # Fixed order, so a diff of two gold files is readable.
        return {name: v[name] for name in CRITICAL_FIELDS}

    @property
    def is_complete(self) -> bool:
        return all(f.is_labelled for f in self.ground_truth.values())

    def unlabelled(self) -> list[str]:
        return [n for n, f in self.ground_truth.items() if not f.is_labelled]

    @classmethod
    def blank_case(cls, case_id: str, source_text: str) -> "GoldCase":
        return cls(
            case_id=case_id,
            source_text=source_text,
            ground_truth={name: GoldField() for name in CRITICAL_FIELDS},
        )


class GoldSet(BaseModel):
    """The whole gold_v1 file, provenance included.

    `provenance` is not decoration. A recall number is only reproducible if
    someone else can pull byte-identical notes, which means pinning the
    dataset revision, not just its name.
    """

    schema_version: str = "gold-v1"
    provenance: dict = Field(default_factory=dict)
    cases: list[GoldCase]

    @property
    def complete_cases(self) -> list[GoldCase]:
        return [c for c in self.cases if c.is_complete]

    def progress(self) -> tuple[int, int]:
        labelled = sum(
            1 for c in self.cases for f in c.ground_truth.values() if f.is_labelled
        )
        return labelled, len(self.cases) * len(CRITICAL_FIELDS)


# ============================================================================
# 3. Prompt
# ============================================================================

SYSTEM_INSTRUCTION = f"""\
You extract four fields from a dictated clinical note for a polyclinic physician.

FIELDS
{render_field_rules()}

EVIDENCE IS THE PRODUCT
For every field you mark `found` or `unsure`, `evidence` must be a span copied
VERBATIM from the note - character for character, including original casing,
spelling, abbreviations and spacing. Do not expand shorthand, do not correct
typos, do not re-wrap whitespace, do not add surrounding words that are not
contiguous in the note. A span that is not a literal substring of the note is
discarded and the field is shown to the physician as blank, so a paraphrased
span is worse than no span.

WHAT DOES NOT COUNT
  Negation      - "denies any drug allergies", "no known allergies" -> the
                  allergy field is not_stated, not an allergy.
  Attribution   - "mother has diabetes on metformin" -> not the patient's
                  medication. not_stated.
  Temporality   - "was on metformin until March" -> discontinued, not current.
                  not_stated.
  Contemplation - "consider starting amlodipine if BP stays high" -> not
                  prescribed. not_stated.

STATUS
  found      - stated for this patient, now, and you have a verbatim span.
  not_stated - the note is silent, or the mention is excluded by the rules
               above. Leave value and evidence as "" - the empty string
               itself, never the word "not_stated" and never a dash.
  unsure     - stated but genuinely ambiguous. Supply your best verbatim span.
               Prefer `unsure` over a confident guess; an unsure field costs
               the physician one glance, a wrong field costs a patient.\
"""

MOCK_RESPONSE = {
    # medication + dose: verbatim, should pass.
    "medication": {"value": "Metformin", "evidence": "metformin", "status": "found"},
    "dose": {"value": "500 mg", "evidence": "500 mg", "status": "found"},
    # frequency: paraphrased shorthand ("po bid" -> "twice daily"). Near miss
    # in spirit, absent in fact -> blanked.
    "frequency": {"value": "twice daily", "evidence": "twice daily", "status": "found"},
    # allergy: outright fabrication -> blanked.
    "allergy": {
        "value": "Penicillin",
        "evidence": "patient reports a penicillin allergy",
        "status": "found",
    },
}


# ============================================================================
# 4. Gates: deterministic, no model calls in this section, ever
# ============================================================================
# Deterministic safety gates. No model calls in this file, ever.
#
# The proposal states the headline rule as
#
#     assert evidence in source_text
#
# and it is implemented below as an explicit `if`, never as an `assert`
# statement: `python -O` strips assert statements, which would silently switch
# the gate off. The same holds for every check in this file, and
# tests/test_guardrails.py fails the build if an `assert` statement appears
# anywhere under src/.
#
# Per field, in order:
#
# 1. verify_evidence - the evidence span must be a verbatim substring of the
#    dictation. The labelling tool (evals/label_gold_v1.py) imports this exact
#    function, so gold labels and model output face the same gate.
# 2. verify_value - the value must be grounded in that evidence. The evidence
#    gate alone proves a quote is real, not that the value derived from it is:
#    `evidence="metformin"` with `value="Warfarin 10 mg"` passes gate 1.
#      medication, allergy : every word/number of the value is in the evidence
#      frequency           : the value is a phrase of the evidence, or a
#                            recognised equivalent of one (bd -> twice daily)
#      dose                : every number and unit matches the evidence
#                            (15 mg vs 50 mg, mg vs mcg, no invented unit)
#
# A field failing either gate is overwritten to BLANK, value and evidence
# both, so a fabricated quote is never carried forward, and it carries an
# ABSTAIN_* code saying why. DISPLAY turns the code into the text a physician
# reads; display text never goes into a data field.
#
# Input side, scan_input() inspects the note without ever modifying it: the
# physician's dictation is not "cleaned" to make a check pass.
#   - Encoding anomalies (bidirectional controls, invisible characters, and
#     homoglyphs - letters that only look like the ones she dictated) force
#     a safe abstention: every field is wiped, and extract.py does not call
#     the model at all. What the physician sees and what the model would read
#     must be the same text; when they may not be, nothing is extracted.
#   - Known prompt-injection phrasings downgrade every surviving `found`
#     field to `unsure`, so nothing reaches the physician pre-verified. That
#     is a tripwire for known phrasings, not a defence against paraphrase;
#     the defences are the absence of tools, the gates, and the physician
#     reading the quoted span.
#
# `near_miss` stays: a span that matches only after whitespace/case
# normalisation is still blanked, but counted separately, so strict and
# normalised matching are both reported.

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


# ============================================================================
# 5. Spend ledger
# ============================================================================
# Hard spend ceiling for rented model calls.
#
# The project has $8 of API credit in total. Every live call first checks its
# worst-case cost against a ledger, and records what it actually cost after.
# When the next worst case would cross the ceiling, the call is refused (exit
# code 5) instead of the overspend being discovered on the invoice.
#
# The ledger holds totals only - never note text, evidence or values - and
# lives under data/cache/, which .gitignore already excludes.

DEFAULT_LEDGER = ROOT / "data" / "cache" / "spend_ledger.json"
DEFAULT_BUDGET_USD = 8.00

# English prompts run about 4 characters per token. Dividing by 3 over-counts
# on purpose: a budget check has to err towards refusing.
CHARS_PER_TOKEN_FLOOR = 3


class BudgetExceeded(Exception):
    """The next call could cross the ceiling, or the ledger cannot be trusted."""


def worst_case_cost(
    prompt_chars: int,
    max_output_tokens: int,
    price_in_per_mtok: float,
    price_out_per_mtok: float,
) -> float:
    input_tokens = math.ceil(prompt_chars / CHARS_PER_TOKEN_FLOOR)
    return (
        input_tokens / 1e6 * price_in_per_mtok
        + max_output_tokens / 1e6 * price_out_per_mtok
    )


class SpendLedger:
    def __init__(self, path: Path = DEFAULT_LEDGER):
        self.path = Path(path)

    def _read(self) -> dict:
        if not self.path.is_file():
            return {"spent_usd": 0.0, "calls": 0, "by_model": {}}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            # A ledger that cannot be read cannot vouch for the budget, so
            # refuse to spend rather than assume it is zero.
            raise BudgetExceeded(
                f"spend ledger {self.path} is unreadable ({type(exc).__name__}); "
                "fix or remove it deliberately before spending more"
            ) from None

    @contextmanager
    def _locked(self):
        """Serialise read-modify-write across processes (POSIX). A check and
        the record that follows it are not one transaction, so parallel
        callers can overshoot by at most one call's cost each."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        with open(lock_path, "a", encoding="utf-8") as handle:
            try:
                import fcntl
            except ImportError:  # Windows: no advisory lock, single process only
                yield
                return
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def spent(self) -> float:
        return float(self._read().get("spent_usd", 0.0))

    def ensure_room(self, worst_case_usd: float, ceiling_usd: float) -> None:
        spent = self.spent()
        if spent + worst_case_usd > ceiling_usd:
            raise BudgetExceeded(
                f"spent ${spent:.4f} of the ${ceiling_usd:.2f} ceiling; this call could "
                f"cost up to ${worst_case_usd:.4f}, which would cross it"
            )

    def record(self, cost_usd: float, model: str) -> None:
        with self._locked():
            data = self._read()
            data["spent_usd"] = round(float(data.get("spent_usd", 0.0)) + cost_usd, 6)
            data["calls"] = int(data.get("calls", 0)) + 1
            by_model = data.setdefault("by_model", {})
            by_model[model] = round(float(by_model.get(model, 0.0)) + cost_usd, 6)
            data["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
            tmp.replace(self.path)


# ============================================================================
# 6-7. Pipeline and CLI
# ============================================================================

class PipelineError(Exception):
    """A request-level failure: becomes an error envelope and an exit code."""

    def __init__(self, code: str, message: str, exit_code: int):
        super().__init__(message)
        self.code = code
        self.message = message
        self.exit_code = exit_code


def error_envelope(code: str, note: str | None = None) -> dict:
    """The stdout document for a failed request: a code and null data. The
    human-readable message goes to stderr, never into this payload."""
    return {
        "status": "error",
        "code": code,
        "data": None,
        "schema_version": OUTPUT_SCHEMA_VERSION,
        "note": note,
    }


def read_note(path: Path) -> str:
    """The canonical source text: UTF-8 (a leading BOM dropped), universal
    newlines exactly as Path.read_text() produces them, otherwise untouched.
    The model, the gates and the physician all see this same text."""
    if not path.is_file():
        raise PipelineError("ERR_INPUT_NOT_FOUND", f"note not found: {path}", EXIT_INPUT)
    size = path.stat().st_size
    if size > MAX_NOTE_BYTES:
        raise PipelineError(
            "ERR_INPUT_TOO_LARGE",
            f"the file is {size:,} bytes; notes are limited to {MAX_NOTE_CHARS:,} characters",
            EXIT_INPUT,
        )
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise PipelineError(
            "ERR_INPUT_UNREADABLE", f"could not read the note ({type(exc).__name__})", EXIT_INPUT,
        ) from None
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise PipelineError(
            "ERR_INPUT_ENCODING", f"the note is not valid UTF-8 (byte {exc.start})", EXIT_INPUT,
        ) from None
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    check_text(text)
    return text


def check_text(text: str) -> None:
    """check_input, mapped onto this file's envelope and exit code."""
    try:
        check_input(text)
    except InputRejected as exc:
        raise PipelineError(exc.code, str(exc), EXIT_INPUT) from None


def _looks_like_placeholder(key: str) -> bool:
    return "..." in key or "<" in key or len(key) < 20


def load_api_key(dotenv: Path = ROOT / ".env") -> str:
    """OPENROUTER_API_KEY from the environment, then from .env at the repo
    root. A placeholder pasted from documentation is refused, because a
    shell variable would otherwise shadow the real key in .env."""
    key = os.environ.get("OPENROUTER_API_KEY")
    source = "the OPENROUTER_API_KEY environment variable"
    if not key and dotenv.is_file():
        for line in dotenv.read_text(encoding="utf-8").splitlines():
            name, _, value = line.strip().partition("=")
            if name.strip() == "OPENROUTER_API_KEY" and value.strip():
                key, source = value.strip().strip("'\""), f"{dotenv.name}"
    if not key:
        raise PipelineError(
            "ERR_CONFIG_NO_API_KEY",
            "no API key: set OPENROUTER_API_KEY in your shell or in .env at the repo "
            "root, or run with --mock to exercise the gates without an API call",
            EXIT_INTERNAL,
        )
    if _looks_like_placeholder(key):
        raise PipelineError(
            "ERR_CONFIG_PLACEHOLDER_KEY",
            f"OPENROUTER_API_KEY from {source} is a placeholder, not a key "
            "(run `unset OPENROUTER_API_KEY` if a pasted export is shadowing .env)",
            EXIT_INTERNAL,
        )
    return key


def resolve_prices(model: str, price_in: float | None, price_out: float | None):
    if price_in is not None and price_out is not None:
        return price_in, price_out
    if model in PRICES_PER_MTOK:
        return PRICES_PER_MTOK[model]
    raise PipelineError(
        "ERR_CONFIG_UNPRICED_MODEL",
        f"no price on file for {model!r}: pass --price-in-per-mtok and "
        "--price-out-per-mtok from the current pricing page, or the spend "
        "ceiling would multiply by the wrong number",
        EXIT_INTERNAL,
    )


def resolve_budget(budget_usd: float | None) -> float:
    if budget_usd is not None:
        return budget_usd
    raw = os.environ.get("MEDIEXTRACT_BUDGET_USD")
    if raw is None:
        return DEFAULT_BUDGET_USD
    try:
        return float(raw)
    except ValueError:
        raise PipelineError(
            "ERR_CONFIG_BAD_BUDGET", f"MEDIEXTRACT_BUDGET_USD={raw!r} is not a number",
            EXIT_INTERNAL,
        ) from None


def build_request(model: str, note: str, data_collection: str = "deny"):
    """The complete request, built without touching the network so tests
    can pin it. Returns (client settings, create() arguments). There are no
    tools and no tool choice: the model has nothing it could call (OWASP
    LLM03:2026, Excessive Agency)."""
    client_kwargs = {
        "base_url": OPENROUTER_BASE_URL,
        "timeout": HTTP_TIMEOUT_S,
        "max_retries": HTTP_MAX_RETRIES,
    }
    request = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_INSTRUCTION},
            {"role": "user", "content": NOTE_TEMPLATE.format(note=note)},
        ],
        "temperature": 0.0,
        "seed": 0,
        "max_tokens": MAX_OUTPUT_TOKENS,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "clinical_extraction",
                "strict": True,
                "schema": response_json_schema(),
            },
        },
        "extra_body": {
            "provider": {**PROVIDER_PREFERENCES, "data_collection": data_collection},
            "reasoning": dict(REASONING),
        },
    }
    return client_kwargs, request


def prompt_fingerprint() -> str:
    """Hash of everything that steers the model except the note and the
    model ID: instructions, note template, response schema, decoding
    settings. Results are comparable only within one fingerprint."""
    material = json.dumps({
        "system": SYSTEM_INSTRUCTION,
        "template": NOTE_TEMPLATE,
        "schema": response_json_schema(),
        "temperature": 0.0,
        "seed": 0,
        "max_tokens": MAX_OUTPUT_TOKENS,
        "reasoning": REASONING,
    }, sort_keys=True)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def upstream_error(exc: Exception) -> PipelineError | None:
    """Map an SDK exception to an envelope. Only the status is echoed, never
    the upstream message: OpenRouter error bodies can carry metadata such
    as flagged input, and the input is the note."""
    import openai

    if isinstance(exc, openai.APITimeoutError):
        return PipelineError(
            "ERR_UPSTREAM_TIMEOUT",
            f"no response within {HTTP_TIMEOUT_S:.0f} s on either of {HTTP_MAX_RETRIES + 1} attempts",
            EXIT_UPSTREAM,
        )
    if isinstance(exc, openai.APIConnectionError):
        return PipelineError(
            "ERR_UPSTREAM_UNREACHABLE", "could not reach OpenRouter", EXIT_UPSTREAM,
        )
    if not isinstance(exc, openai.APIStatusError):
        return None

    status = exc.status_code
    upstream = str(getattr(exc, "message", "") or "").lower()
    if status == 402:
        return PipelineError(
            "ERR_UPSTREAM_CREDITS_EXHAUSTED",
            "OpenRouter refused the call for lack of credit (402)", EXIT_BUDGET,
        )
    if status == 404 and "endpoint" in upstream:
        return PipelineError(
            "ERR_NO_ELIGIBLE_PROVIDER",
            "no OpenRouter provider serves this model with every required parameter "
            "and the requested data policy (404). Do not relax the policy for real "
            "patient data; for public or synthetic notes only, "
            "--provider-data-collection allow widens routing",
            EXIT_UPSTREAM,
        )
    if status == 404:
        return PipelineError(
            "ERR_MODEL_UNAVAILABLE",
            "the model returned 404 - it may be retired or renamed. Pass --model "
            "explicitly; there is no automatic fallback, because a different model "
            "invalidates every number measured on this one",
            EXIT_UPSTREAM,
        )
    if status == 408:
        return PipelineError("ERR_UPSTREAM_TIMEOUT", "OpenRouter timed out (408)", EXIT_UPSTREAM)
    if status == 429:
        return PipelineError(
            "ERR_RATE_LIMITED", f"rate limited after {HTTP_MAX_RETRIES + 1} attempts", EXIT_UPSTREAM,
        )
    if status in (401, 403):
        return PipelineError(
            "ERR_UPSTREAM_AUTH",
            f"OpenRouter refused the request ({status}): invalid key, missing "
            "permission, or input flagged by moderation",
            EXIT_UPSTREAM,
        )
    if status >= 500:
        return PipelineError(
            "ERR_UPSTREAM_ERROR", f"OpenRouter or the provider failed ({status})", EXIT_UPSTREAM,
        )
    return PipelineError(
        "ERR_UPSTREAM_REJECTED", f"OpenRouter rejected the request ({status})", EXIT_UPSTREAM,
    )


_CODE_FENCE = re.compile(r"\s*```(?:json)?[ \t]*\n(.*?)\n?[ \t]*```\s*", re.DOTALL)


def _strip_code_fence(text: str) -> str:
    """Some providers wrap JSON in a markdown fence even when a schema is
    requested. Unwrap exactly one whole-document fence; anything else is
    left for the validator to reject."""
    match = _CODE_FENCE.fullmatch(text)
    return match.group(1) if match else text


def parse_output(content: str | None) -> ClinicalExtraction | None:
    """Raw model text -> validated extraction, or None. Pydantic is strict
    here: every key required, no unknown keys, a closed status enum."""
    try:
        return ClinicalExtraction.model_validate_json(_strip_code_fence(content or ""))
    except (ValueError, TypeError):  # pydantic's ValidationError is a ValueError
        return None


def _usage(completion, prices) -> dict:
    usage = getattr(completion, "usage", None)
    details = getattr(usage, "completion_tokens_details", None)
    # Cached input tokens are billed at a lower rate by some providers. Recorded
    # for the cost audit (evals/metrics.py); the estimate below still prices
    # every input token at full rate, so the estimate can only over-state.
    prompt_details = getattr(usage, "prompt_tokens_details", None)
    extra = (getattr(usage, "model_extra", None) or {}) if usage is not None else {}
    reported = extra.get("cost")
    price_in, price_out = prices
    input_tokens = getattr(usage, "prompt_tokens", 0) or 0
    output_tokens = getattr(usage, "completion_tokens", 0) or 0  # includes reasoning
    estimate = round(input_tokens / 1e6 * price_in + output_tokens / 1e6 * price_out, 6)
    reported_ok = isinstance(reported, (int, float)) and not isinstance(reported, bool)
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "reasoning_tokens": (getattr(details, "reasoning_tokens", 0) or 0) if details else 0,
        "cached_tokens": ((getattr(prompt_details, "cached_tokens", 0) or 0)
                         if prompt_details else 0),
        "est_cost_usd": estimate,
        "provider_cost_usd": float(reported) if reported_ok else None,
        # What the ledger records: OpenRouter's own charge when it reports
        # one, the list-price estimate otherwise.
        "billed_usd": round(float(reported), 6) if reported_ok else estimate,
    }


@dataclass
class ModelResult:
    extraction: ClinicalExtraction
    usage: dict
    provenance: dict
    api_ms: float


def call_model(note, model, prices, ledger, budget_usd, data_collection="deny", client=None):
    """One constrained call through OpenRouter, plus at most one retry if
    the output fails the schema. Every attempt is checked against the
    spend ceiling first and recorded in the ledger after. `client` is for
    tests; normally one is built from the API key."""
    import importlib.metadata

    client_kwargs, request = build_request(model, note, data_collection)
    prompt_chars = sum(len(m["content"]) for m in request["messages"])
    prompt_chars += len(json.dumps(request["response_format"]))
    worst = worst_case_cost(prompt_chars, MAX_OUTPUT_TOKENS, *prices)

    totals = {"called": True, "input_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0,
              "cached_tokens": 0, "est_cost_usd": 0.0, "provider_cost_usd": None,
              "billed_usd": 0.0, "attempts": 0}
    api_ms = 0.0
    for attempt in range(1, SCHEMA_ATTEMPTS + 1):
        try:
            ledger.ensure_room(worst, budget_usd)
        except BudgetExceeded as exc:
            raise PipelineError("ERR_BUDGET_EXCEEDED", str(exc), EXIT_BUDGET) from None
        if client is None:
            import openai
            client = openai.OpenAI(api_key=load_api_key(), **client_kwargs)

        started = time.perf_counter()
        try:
            completion = client.chat.completions.create(**request)
        except Exception as exc:
            mapped = upstream_error(exc)
            if mapped is None:
                raise
            raise mapped from None
        api_ms += (time.perf_counter() - started) * 1000

        usage = _usage(completion, prices)
        ledger.record(usage["billed_usd"], model)
        for key in ("input_tokens", "output_tokens", "reasoning_tokens", "cached_tokens"):
            totals[key] += usage[key]
        for key in ("est_cost_usd", "billed_usd"):
            totals[key] = round(totals[key] + usage[key], 6)
        if usage["provider_cost_usd"] is not None:
            totals["provider_cost_usd"] = round((totals["provider_cost_usd"] or 0.0)
                                                + usage["provider_cost_usd"], 6)
        totals["attempts"] = attempt

        choices = getattr(completion, "choices", None) or []
        if not choices:
            raise PipelineError(
                "ERR_UPSTREAM_ERROR", "the response carried no choices", EXIT_UPSTREAM,
            )
        if getattr(choices[0], "finish_reason", None) == "length":
            # Retrying would hit the same cap and bill twice.
            raise PipelineError(
                "ERR_OUTPUT_TRUNCATED",
                f"the output hit the {MAX_OUTPUT_TOKENS}-token cap", EXIT_SCHEMA,
            )
        extraction = parse_output(getattr(choices[0].message, "content", None))
        if extraction is not None:
            extra = getattr(completion, "model_extra", None) or {}
            try:
                sdk = f"openai {importlib.metadata.version('openai')}"
            except importlib.metadata.PackageNotFoundError:
                sdk = "openai (version unknown)"
            provenance = {
                "called": True,
                "requested_model": model,
                "served_model": getattr(completion, "model", None),
                "provider": extra.get("provider"),
                "response_id": getattr(completion, "id", None),
                "sdk": sdk,
                "prompt_fingerprint": prompt_fingerprint(),
            }
            return ModelResult(extraction, totals, provenance, api_ms)

    raise PipelineError(
        "ERR_SCHEMA_INVALID",
        f"the model output failed the schema on {SCHEMA_ATTEMPTS} attempts",
        EXIT_SCHEMA,
    )


def to_output(extraction: ClinicalExtraction) -> dict:
    """The data a consumer stores. Wiped and absent values are null (a hard
    wipe), never an empty string and never display text. Display text lives
    only in the separate `gate` list."""
    return {
        name: {
            "evidence": field.evidence if field.evidence != BLANK else None,
            "value": field.value if field.value != BLANK else None,
            "status": field.status.value,
        }
        for name, field in extraction
    }


def build_payload(extraction, source_text, scan, gate_enabled, *, note, model_label,
                  usage, provenance, api_ms, started):
    """Gate the extraction and assemble the stdout document.
    Returns (payload, exit code, gate results)."""
    gated, results = apply_gates(extraction, source_text, enabled=gate_enabled, scan=scan)
    # `started` marks the beginning of the LOCAL work, so end to end is the API
    # time plus ours. Measuring only the local clock made `within_budget` true
    # for a 12-second call in a batch run, because there the payload is built
    # from a cached result and the local clock never saw the call.
    local_ms = (time.perf_counter() - started) * 1000
    total_ms = api_ms + local_ms
    payload = {
        "status": "ok",
        "schema_version": OUTPUT_SCHEMA_VERSION,
        "note": note,
        "model": model_label,
        "provenance": provenance,
        "gate_enabled": gate_enabled,
        "latency_ms": {"api": round(api_ms, 1), "local": round(local_ms, 1),
                       "total": round(total_ms, 1)},
        "latency_budget_ms": LATENCY_BUDGET_MS,
        "within_budget": total_ms < LATENCY_BUDGET_MS,
        "usage": usage,
        "input_scan": {"flags": scan.flags, "review_required": scan.review_required},
        "extraction": to_output(gated),
        # Codes only. The review screen turns a code into its label with
        # DISPLAY (section 4); reasons are printed to stderr.
        "gate": [
            {"field": r.field, "outcome": r.outcome, "code": r.code, "near_miss": r.near_miss}
            for r in results
        ],
    }
    exit_code = EXIT_BLANKED if any(r.outcome == BLANKED for r in results) else EXIT_OK
    return payload, exit_code, results


def _mark(result) -> str:
    if result.outcome == BLANKED:
        return "WIPED"
    if result.outcome == SKIPPED:
        return "----"
    return "REVIEW" if result.code.startswith("REVIEW") else "OK"


def print_gate_report(results, gate_enabled: bool, scan: InputScan, out=sys.stderr) -> None:
    print("\nSAFETY GATES  (evidence in source_text -> value -> number/unit)", file=out)
    print("-" * 78, file=out)
    for result in results:
        note = " [near miss]" if result.near_miss else ""
        print(f"  {_mark(result):<6} {result.field:<11} {result.display}{note}", file=out)
        if result.outcome != PASS or result.code.startswith("REVIEW"):
            print(f"  {'':<6} {'':<11} {result.reason}", file=out)
    print("-" * 78, file=out)
    if scan.review_required:
        print(f"  input flags: {', '.join(scan.flags)} -> nothing is shown pre-verified", file=out)

    failed = [r.field for r in results if r.outcome == BLANKED]
    if not failed:
        print("  verdict: no field wiped", file=out)
    elif gate_enabled:
        print(f"  verdict: wiped {', '.join(failed)} -> manual entry", file=out)
    else:
        print(
            f"  verdict: would have wiped {', '.join(failed)}, "
            "but --no-gate left the payload untouched",
            file=out,
        )


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--note", required=True, type=Path, help="path to a note .txt")
    parser.add_argument("--model", default=MODEL)
    parser.add_argument("--mock", action="store_true", help="no API call, canned response")
    parser.add_argument("--no-gate", action="store_true", help="report but do not wipe")
    parser.add_argument(
        "--json-only", action="store_true", help="suppress the stderr gate table",
    )
    parser.add_argument(
        "--budget-usd", type=float, default=None,
        help=f"total spend ceiling (default {DEFAULT_BUDGET_USD:.2f}, or MEDIEXTRACT_BUDGET_USD)",
    )
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER, help=argparse.SUPPRESS)
    parser.add_argument("--price-in-per-mtok", type=float, default=None)
    parser.add_argument("--price-out-per-mtok", type=float, default=None)
    parser.add_argument(
        "--provider-data-collection", choices=["deny", "allow"], default="deny",
        help="OpenRouter data policy; 'allow' only for public or synthetic notes",
    )
    parser.add_argument("--debug", action="store_true", help="traceback on internal errors")
    return parser.parse_args(argv)


def run(args):
    """The pipeline for one note. Returns (payload, exit code, results, scan)."""
    source_text = read_note(args.note)
    scan = scan_input(source_text)

    if scan.encoding_anomaly:
        # Forced safe abstention, decided before any call: hidden or
        # look-alike characters mean the note is not what the physician
        # sees, so nothing is extracted and nothing is sent upstream.
        extraction = wiped_extraction()
        usage = {"called": False, "billed_usd": 0.0}
        provenance = {"requested_model": "mock" if args.mock else args.model, "called": False,
                      "prompt_fingerprint": prompt_fingerprint()}
        api_ms = 0.0
    elif args.mock:
        extraction = ClinicalExtraction.model_validate(MOCK_RESPONSE)
        usage = {"called": False, "billed_usd": 0.0}
        provenance = {"called": False, "requested_model": "mock",
                      "prompt_fingerprint": prompt_fingerprint()}
        api_ms = 0.0
    else:
        prices = resolve_prices(args.model, args.price_in_per_mtok, args.price_out_per_mtok)
        result = call_model(
            source_text, args.model, prices, SpendLedger(args.ledger),
            resolve_budget(args.budget_usd), args.provider_data_collection,
        )
        extraction, usage, provenance, api_ms = (
            result.extraction, result.usage, result.provenance, result.api_ms)

    local_started = time.perf_counter()
    payload, exit_code, results = build_payload(
        extraction, source_text, scan, not args.no_gate,
        note=str(args.note), model_label="mock" if args.mock else args.model,
        usage=usage, provenance=provenance, api_ms=api_ms, started=local_started,
    )
    return payload, exit_code, results, scan


def main(argv=None) -> int:
    args = parse_args(argv)
    note = str(args.note)
    stdout = sys.stdout
    try:
        # Anything printed while the pipeline runs - by this code or by a
        # library - lands on stderr. stdout receives exactly one JSON
        # document, written below.
        with contextlib.redirect_stdout(sys.stderr):
            payload, exit_code, results, scan = run(args)
            if not args.json_only:
                print_gate_report(results, not args.no_gate, scan)
                latency = payload["latency_ms"]
                print(
                    f"\nlatency: {latency['total']:.0f} ms total ({latency['api']:.0f} ms model) "
                    f"vs {LATENCY_BUDGET_MS} ms budget   "
                    f"billed: ${payload['usage'].get('billed_usd', 0):.6f}",
                    file=sys.stderr,
                )
    except PipelineError as exc:
        payload, exit_code = error_envelope(exc.code, note), exc.exit_code
        # Errors are telemetry: always on stderr, --json-only or not.
        print(f"ERROR {exc.code} (exit {exit_code}): {exc.message}", file=sys.stderr)
    except Exception as exc:
        # The backstop. A crash must never exit 1, which means "a gate wiped
        # a field". The exception text is withheld because it can quote the
        # note (pydantic errors echo their input).
        payload = error_envelope("ERR_INTERNAL", note)
        exit_code = EXIT_INTERNAL
        print(f"ERROR ERR_INTERNAL (exit {exit_code}): {type(exc).__name__} "
              "(re-run with --debug for the traceback)", file=sys.stderr)
        if args.debug:
            traceback.print_exc()
    stdout.write(json.dumps(payload, indent=2) + "\n")
    stdout.flush()
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
