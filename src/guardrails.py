"""Deterministic safety gates. No model calls in this file, ever.

Week 1 ships gate #1 only: the verbatim evidence check.

    assert evidence in source_text

A field whose evidence span is not a literal substring of the dictation
cannot be shown to Dr. Aisha, because she has nothing to verify it against
in under 3 seconds. Such a field is overwritten to BLANK and routed to
human review.

`near_miss` exists to earn the strictness of that rule with data. If the
span matches only after whitespace/case normalisation, the model was
paraphrasing rather than fabricating. We still blank the field, but we log
the distinction so Week 2 can decide whether to relax the gate to
normalised matching, backed by counts instead of vibes.

Not yet implemented (Week 2): the numeric/unit checker, which catches a
correct evidence span paired with a wrong value (15 mg vs 50 mg). The
evidence gate does NOT cover that case - see README.
"""

import re
import unicodedata
from dataclasses import dataclass

from schema import BLANK, CRITICAL_FIELDS, ClinicalExtraction, ExtractedField, Status

PASS = "pass"
BLANKED = "blanked"
SKIPPED = "skipped"  # model claimed not_stated; nothing to verify


@dataclass
class GateResult:
    field: str
    outcome: str          # pass | blanked | skipped
    reason: str
    near_miss: bool = False

    @property
    def ok(self) -> bool:
        return self.outcome != BLANKED


def _normalise(text: str) -> str:
    """Collapse the differences a paraphrasing model tends to introduce."""
    text = unicodedata.normalize("NFKC", text)
    return re.sub(r"\s+", " ", text).strip().casefold()


def verify_evidence(name: str, field: ExtractedField, source_text: str) -> GateResult:
    """Gate one field against the dictation it came from."""
    if field.status is Status.NOT_STATED:
        if field.evidence != BLANK or field.value != BLANK:
            return GateResult(
                name, BLANKED,
                "status=not_stated but value/evidence non-empty (incoherent output)",
            )
        return GateResult(name, SKIPPED, "not stated in dictation")

    if field.evidence == BLANK:
        return GateResult(
            name, BLANKED, f"status={field.status.value} with no evidence span"
        )

    if field.evidence in source_text:
        return GateResult(name, PASS, "evidence span found verbatim")

    near = _normalise(field.evidence) in _normalise(source_text)
    return GateResult(
        name, BLANKED,
        "evidence span matches only after whitespace/case normalisation"
        if near else "evidence span NOT present in dictation (fabricated)",
        near_miss=near,
    )


def apply_gates(
    extraction: ClinicalExtraction, source_text: str, enabled: bool = True
) -> tuple[ClinicalExtraction, list[GateResult]]:
    """Run every gate and return the (possibly blanked) extraction.

    `enabled=False` reports the same verdicts but leaves the payload
    untouched - that is the Week 3 abstention test: same run, gate off,
    count how many false fields would have reached the physician.
    """
    results = [
        verify_evidence(name, getattr(extraction, name), source_text)
        for name in CRITICAL_FIELDS
    ]

    if not enabled:
        return extraction, results

    gated = extraction.model_copy(deep=True)
    for result in results:
        if result.outcome == BLANKED:
            setattr(gated, result.field, getattr(gated, result.field).blank())
    return gated, results
