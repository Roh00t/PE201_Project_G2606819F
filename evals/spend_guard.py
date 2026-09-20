#!/usr/bin/env python3
"""Pre-call spend ENFORCEMENT: a hard per-run cost cap and a per-call audit trail.

    from spend_guard import CostGuard, CallRecord
    guard = CostGuard(run_id="...", cap_usd=1.00, ledger=SpendLedger(), ceiling_usd=8.00)
    guard.check_before(worst_case_usd)      # raises before any money is spent
    guard.record(CallRecord.from_result(...))   # after the call
    guard.write_summary(out_dir / "metrics.json")

Enforcement lives here; *measurement* lives in `evals/metrics.py`, which is a set
of pure functions over a finished run and never touches the network. Keeping the
two apart is deliberate: a module that can refuse a call should not also be the
one that scores it, or a bug in the scoring can silence the refusal.

`src/extract.py` already owns the *global* $8 ceiling through `SpendLedger`: a
pre-call worst-case check, a file-locked read-modify-write, and a refusal if the
ledger cannot be read. This module does not replace any of that and is never
imported by the pipeline - CLAUDE.md 1.1 keeps `src/extract.py` standalone, so
this is evaluation-side infrastructure used by `evals/run_ekacare.py` and the
probe scripts.

What it adds on top of the ledger, which records only dollars and calls:

  per-call audit    prompt, completion, cached and reasoning tokens, both the
                    list-price estimate and the provider's own charge, latency,
                    attempts, and the destination - provider, endpoint, model
                    requested versus model actually served, and the response id.
  per-run cap       a second, tighter ceiling scoped to one run. The global $8
                    budget is the project's; a runaway loop inside one run would
                    stay under it while burning the lot, so `cap_usd` refuses
                    before the call that would cross it.
  live total        one line per call on stderr, so an operator watching a batch
                    sees the money move rather than discovering it afterwards.
  summary artifact  aggregate JSON beside the run's other artefacts.

Two deliberate choices about what is written where. The per-call log goes to
`data/cache/metrics/<run_id>.jsonl`, which is gitignored, because it carries
provider response ids that are operational detail rather than results. The
summary written next to the run is aggregate plus destinations, and never
contains a key, a note, or any extracted clinical value.

Costs are only as honest as the price table, so a call whose cost cannot be
computed is a refusal upstream in `extract.resolve_prices`, not a zero here.
"""

import json
import statistics
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from extract import BudgetExceeded, OPENROUTER_BASE_URL  # noqa: E402

DEFAULT_LOG_DIR = ROOT / "data" / "cache" / "metrics"

# One run of the 67-case gold set costs about $0.04. A cap of $1.00 leaves room
# for a v4 schema (more output tokens) and for a retry storm, while still
# stopping a loop two orders of magnitude before it reaches the $8 ceiling.
DEFAULT_RUN_CAP_USD = 1.00


def money(amount: float) -> str:
    """Dollars at a precision that does not hide the number. A $0.002 cap
    printed as "$0.00" is worse than not printing it."""
    if amount == 0:
        return "$0.00"
    return f"${amount:.6f}" if abs(amount) < 0.01 else f"${amount:.2f}"


class RunCapExceeded(BudgetExceeded):
    """The per-run cap would be crossed. A subclass of BudgetExceeded so callers
    that already handle a spend refusal keep working unchanged, and so it maps
    to the same exit code."""


@dataclass(frozen=True)
class CallRecord:
    """One model call, as billed. Field names match extract.py's usage dict so
    the two cannot drift apart silently."""

    run_id: str
    case_id: str
    at: str
    model_requested: str
    model_served: str | None
    provider: str | None
    endpoint: str
    response_id: str | None
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    reasoning_tokens: int
    est_cost_usd: float
    provider_cost_usd: float | None
    billed_usd: float
    latency_ms: float
    attempts: int
    ok: bool = True
    code: str | None = None

    @classmethod
    def from_result(cls, run_id: str, case_id: str, model: str, usage: dict,
                    provenance: dict, api_ms: float, *, ok: bool = True,
                    code: str | None = None, endpoint: str = OPENROUTER_BASE_URL
                    ) -> "CallRecord":
        usage = usage or {}
        provenance = provenance or {}
        return cls(
            run_id=run_id, case_id=case_id,
            at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            model_requested=provenance.get("requested_model") or model,
            model_served=provenance.get("served_model"),
            provider=provenance.get("provider"),
            endpoint=endpoint,
            response_id=provenance.get("response_id"),
            input_tokens=int(usage.get("input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
            cached_tokens=int(usage.get("cached_tokens") or 0),
            reasoning_tokens=int(usage.get("reasoning_tokens") or 0),
            est_cost_usd=float(usage.get("est_cost_usd") or 0.0),
            provider_cost_usd=(None if usage.get("provider_cost_usd") is None
                               else float(usage["provider_cost_usd"])),
            billed_usd=float(usage.get("billed_usd") or 0.0),
            latency_ms=round(float(api_ms or 0.0), 1),
            attempts=int(usage.get("attempts") or 0),
            ok=ok, code=code,
        )


@dataclass
class CostGuard:
    """Live cost accounting for one run, bounded twice over."""

    run_id: str
    cap_usd: float = DEFAULT_RUN_CAP_USD
    ledger: object | None = None
    ceiling_usd: float | None = None
    log_dir: Path = DEFAULT_LOG_DIR
    out: object = sys.stderr
    expected_calls: int | None = None
    verbose: bool = True
    # Who writes the project ledger. `extract.call_model` already records every
    # call it makes, so the batch loop leaves this False or the $8 ceiling would
    # count each call twice. A script that talks to the endpoint directly - a
    # probe, a one-off experiment - must set it True, or its spend is invisible
    # to the ceiling that is supposed to bound it.
    records_to_ledger: bool = False
    records: list = field(default_factory=list)

    def __post_init__(self):
        self.log_path = Path(self.log_dir) / f"{self.run_id}.jsonl"
        self._spent = 0.0

    # -- before the money moves ------------------------------------------
    def check_before(self, worst_case_usd: float) -> None:
        """Refuse a call that could cross either bound. The per-run cap is
        checked here; the global ceiling is delegated to the ledger that owns
        it, so there is exactly one authority for the project budget."""
        if self._spent + worst_case_usd > self.cap_usd:
            raise RunCapExceeded(
                f"run {self.run_id} has spent {money(self._spent)} of its "
                f"{money(self.cap_usd)} cap; this call could cost up to "
                f"{money(worst_case_usd)}, which would cross it. Raise the cap "
                "deliberately if that is expected."
            )
        if self.ledger is not None and self.ceiling_usd is not None:
            self.ledger.ensure_room(worst_case_usd, self.ceiling_usd)

    # -- after it has ----------------------------------------------------
    def record(self, record: CallRecord) -> float:
        self.records.append(record)
        self._spent = round(self._spent + record.billed_usd, 6)
        if self.records_to_ledger and self.ledger is not None and record.billed_usd:
            self.ledger.record(record.billed_usd, record.model_requested)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.log_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(asdict(record)) + "\n")
        if self.verbose:
            print(self.live_line(record), file=self.out, flush=True)
        return self._spent

    def live_line(self, record: CallRecord) -> str:
        position = (f"[{len(self.records)}/{self.expected_calls}]" if self.expected_calls
                    else f"[{len(self.records)}]")
        served = record.model_served or record.model_requested
        drift = "" if served == record.model_requested else f" served:{served}"
        cached = f" cached:{record.cached_tokens}" if record.cached_tokens else ""
        return (f"{position} {record.case_id} {record.latency_ms:.0f}ms "
                f"{record.input_tokens}+{record.output_tokens}tok{cached} "
                f"{money(record.billed_usd)}  run {money(self._spent)}/"
                f"{money(self.cap_usd)}{drift}")

    @property
    def spent_usd(self) -> float:
        return self._spent

    # -- what it all came to ---------------------------------------------
    def summary(self) -> dict:
        billed = [r.billed_usd for r in self.records]
        latency = [r.latency_ms for r in self.records] or [0.0]
        estimates = [r.est_cost_usd for r in self.records]
        reported = [r.provider_cost_usd for r in self.records if r.provider_cost_usd is not None]
        by_model: dict[str, dict] = {}
        for record in self.records:
            bucket = by_model.setdefault(record.model_requested,
                                         {"calls": 0, "billed_usd": 0.0,
                                          "input_tokens": 0, "output_tokens": 0})
            bucket["calls"] += 1
            bucket["billed_usd"] = round(bucket["billed_usd"] + record.billed_usd, 6)
            bucket["input_tokens"] += record.input_tokens
            bucket["output_tokens"] += record.output_tokens
        destinations = sorted({(r.endpoint, r.provider or "-", r.model_served or "-")
                               for r in self.records})
        drift = sorted({(r.model_requested, r.model_served) for r in self.records
                        if r.model_served and r.model_served != r.model_requested})
        return {
            "run_id": self.run_id,
            "calls": len(self.records),
            "cap_usd": self.cap_usd,
            "ceiling_usd": self.ceiling_usd,
            "billed_usd": round(sum(billed), 6),
            "cap_used": round(sum(billed) / self.cap_usd, 4) if self.cap_usd else None,
            "cost_per_call_usd": round(statistics.mean(billed), 6) if billed else 0.0,
            "tokens": {
                "input": sum(r.input_tokens for r in self.records),
                "output": sum(r.output_tokens for r in self.records),
                "cached": sum(r.cached_tokens for r in self.records),
                "reasoning": sum(r.reasoning_tokens for r in self.records),
            },
            "latency_ms": {
                "p50": round(statistics.median(latency), 1),
                # Nearest-rank, round-half-up: the same expression as
                # metrics.percentile, repeated rather than imported so that
                # enforcement never depends on measurement. If one changes,
                # change both - tests/test_spend_guard.py pins this one.
                "p95": round(sorted(latency)[min(len(latency),
                             max(1, int(round(0.95 * len(latency) + 0.5)))) - 1], 1),
                "max": round(max(latency), 1),
            },
            "retried_calls": sum(1 for r in self.records if r.attempts > 1),
            "failed_calls": sum(1 for r in self.records if not r.ok),
            # The list-price model against the provider's own accounting. A gap
            # here means the price table is stale and the ceiling is a guess.
            "price_check": {
                "calls_with_provider_cost": len(reported),
                "estimate_total_usd": round(sum(estimates), 6),
                "provider_total_usd": round(sum(reported), 6) if reported else None,
                "drift_usd": (round(sum(reported) - sum(estimates[:len(reported)]), 6)
                              if reported else None),
            },
            "by_model": by_model,
            # Destination audit: where the money actually went.
            "destinations": [{"endpoint": e, "provider": p, "model_served": m}
                             for e, p, m in destinations],
            "model_drift": [{"requested": a, "served": b} for a, b in drift],
            "per_call_log": str(self.log_path.relative_to(ROOT))
            if self.log_path.is_relative_to(ROOT) else str(self.log_path),
        }

    def write_summary(self, path: Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.summary(), indent=2) + "\n", encoding="utf-8")
        return path

    def print_summary(self, out=None) -> None:
        summary = self.summary()
        out = out or self.out
        tokens = summary["tokens"]
        print(f"\ncost: {money(summary['billed_usd'])} over {summary['calls']} calls "
              f"({summary['cap_used']:.1%} of the {money(self.cap_usd)} run cap); "
              f"{tokens['input']:,} in / {tokens['output']:,} out tokens"
              + (f" / {tokens['cached']:,} cached" if tokens["cached"] else ""), file=out)
        check = summary["price_check"]
        if check["provider_total_usd"] is not None:
            print(f"price check: list-price model ${check['estimate_total_usd']:.6f} vs "
                  f"provider ${check['provider_total_usd']:.6f} "
                  f"(drift ${check['drift_usd']:+.6f})", file=out)
        for destination in summary["destinations"]:
            print(f"routed to: {destination['endpoint']} | {destination['provider']} | "
                  f"{destination['model_served']}", file=out)
        for entry in summary["model_drift"]:
            print(f"WARNING model drift: asked for {entry['requested']}, "
                  f"served {entry['served']}", file=out)
