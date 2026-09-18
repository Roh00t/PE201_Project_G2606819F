"""Pydantic schema for MediExtract.

Four critical fields only. Every field carries three things:

  value     - the normalised clinical fact (what goes in the chart)
  evidence  - a VERBATIM span copied from the dictation, char-for-char
  status    - found | not_stated | unsure

The `evidence` field is what makes the deterministic safety gate in
guardrails.py possible: if the span is not a literal substring of the
dictation, the field is unverifiable and gets blanked.

BLANK is the empty string rather than None inside the wire schema: the
schema the model is constrained to stays free of nullable types and anyOf,
which structured-output implementations translate unevenly. The payload
extract.py prints converts BLANK to null ("hard wipe"), so a consumer never
sees an empty string posing as a value.
"""

import copy
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

BLANK = ""

CRITICAL_FIELDS = ("medication", "dose", "frequency", "allergy")

# What each field means. The single source of truth: extract.py puts this text
# in the Gemini system prompt, and evals/label_gold_v1.py shows the same text
# to the human labeller. If the two ever read different definitions, recall
# would measure the difference between the definitions, not the model.
FIELD_RULES = {
    "medication": (
        "The single most clinically significant medication prescribed or "
        "continued at this visit. Brand names count ('Dolo 650', 'Telma'). If "
        "two are equally significant and there is no principled way to choose, "
        "use status unsure."
    ),
    "dose": (
        "The STRENGTH of that same medication exactly as dictated: '500', "
        "'500 mg', '0.5 mg'. Dictation often omits the unit - leave it omitted, "
        "do not infer it. A strength inside a product name counts ('Dolo 650' -> "
        "value '650'). A quantity per administration ('1 tablet', 'half') is "
        "NOT a dose; if only a quantity is given, dose is not_stated."
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

from typing import Optional  # noqa: E402

from pydantic import field_validator, model_validator  # noqa: E402


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
