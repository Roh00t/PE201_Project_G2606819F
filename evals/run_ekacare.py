#!/usr/bin/env python3
"""Batch evaluation of MediExtract over the gold set.

    ./.venv/bin/python evals/run_ekacare.py                  # live: sealed gold-v1 + OPENROUTER_API_KEY
    ./.venv/bin/python evals/run_ekacare.py --mock --gold data/gold_labels/gold_v1_template.json --limit 3

For every gold case: one model call (cached, so a resumed run never pays
twice), the same output gated and ungated (the abstention test costs no
extra call), then scoring against the sealed labels (evals/scoring.py).

Live mode refuses, before any call is made:
  - any gold file other than the sealed data/gold_labels/gold_v1.json;
  - a gold file whose SHA-256 differs from gold_v1.sha256 (edited after sealing);
  - unlabelled fields, or note text that drifted from its md5 at pull time;
  - a prompt that changed since the seal, unless --experiment NAME;
  - uncached cases whose worst-case cost exceeds the remaining budget.

extract.py's exit codes feed a circuit breaker (S-10 in guardrails.md):
3 system failures in a row, or more than 5% after 20 calls, halt the run;
a spend refusal halts it at once.

stdout: one JSON summary of codes and numbers only. stderr: progress, error
messages and the evaluation table. Files: evals/results/<run_id>/.
Exit codes: 0 finished; 2 refused before any call; 3 halted by the breaker;
5 spend ceiling or credit; 6 configuration or internal error.
"""

import argparse
import contextlib
import hashlib
import importlib.metadata
import json
import platform
import subprocess
import sys
import time
import traceback
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "evals"))

import extract  # noqa: E402
import spend_guard  # noqa: E402
import scoring  # noqa: E402
from extract import (  # noqa: E402
    DEFAULT_LEDGER, ClinicalExtraction, GoldSet, SpendLedger, scan_input, wiped_extraction,
    worst_case_cost,
)

DATASET = "ekacare/clinical_note_generation_dataset"
MTSAMPLES = "mtsamples"
GOLD_DIR = ROOT / "data" / "gold_labels"
SEALED_PATH = GOLD_DIR / "gold_v1.json"
SEALED_SHA_PATH = GOLD_DIR / "gold_v1.sha256"
RESULTS_DIR = ROOT / "evals" / "results"
CACHE_DIR = ROOT / "data" / "cache" / "runs"

# Labellers tag a case in its note ("#negation") to put it in a slice.
SLICE_TAGS = ("negation", "attribution", "temporality")

EXIT_DONE = 0
EXIT_REFUSED = 2
EXIT_HALTED = 3
EXIT_BUDGET = 5
EXIT_INTERNAL = 6

# extract.py exit codes that mean "the system failed", as opposed to "the
# note was rejected" (2) or "a gate abstained" (1).
FAILURE_EXITS = {extract.EXIT_UPSTREAM, extract.EXIT_SCHEMA, extract.EXIT_INTERNAL}


class Refused(Exception):
    """The run must not start (or continue), with a reason and exit code."""

    def __init__(self, code: str, message: str, exit_code: int):
        super().__init__(message)
        self.code, self.message, self.exit_code = code, message, exit_code


@dataclass(frozen=True)
class SealedGold:
    """One registered gold set: where it lives, what must hash to what, and
    which corpus it is allowed to score.

    Before this existed the sealed path was a module constant, so a live run
    could only ever score `gold_v1.json` from the Eka Care corpus. That put
    gold-v2 corrections and a second corpus out of reach without editing the
    guard that protects the ground truth - which is how guards get weakened
    under deadline pressure. Registering versions instead keeps every check
    (path identity, SHA-256, dataset provenance, label schema, completeness,
    prompt fingerprint) and only makes the target selectable.

    Sealing a new version must stamp the matching `schema_version` into the
    file, or loading it is refused.
    """

    version: str
    path: Path
    sha_path: Path
    dataset: str
    schema_version: str
    describes: str


REGISTRY: dict[str, SealedGold] = {
    "gold-v1": SealedGold(
        "gold-v1", SEALED_PATH, SEALED_SHA_PATH, DATASET, "gold-v1",
        "67 Latin-script Eka Care cases, 268 hand labels, sealed 2026-09-20"),
    "gold-v2": SealedGold(
        "gold-v2", GOLD_DIR / "gold_v2.json", GOLD_DIR / "gold_v2.sha256", DATASET, "gold-v2",
        "gold-v1 with the 16 labels that contradict their own field rules corrected, "
        "plus slice tags. Registered, not yet sealed."),
    "allergy-v1": SealedGold(
        "allergy-v1", GOLD_DIR / "allergy_v1.json", GOLD_DIR / "allergy_v1.sha256",
        MTSAMPLES, "allergy-v1",
        "allergy-bearing notes with ALL-CAPS section headers, for the headers-intact "
        "versus headers-stripped leakage report. Registered, not yet sealed."),
}
DEFAULT_GOLD_VERSION = "gold-v1"


@dataclass
class CircuitBreaker:
    max_consecutive_failures: int = 3
    max_error_rate: float = 0.05
    min_calls_for_rate: int = 20
    calls: int = 0
    failures: int = 0
    consecutive: int = 0
    reason_code: str | None = None

    def record(self, exit_code: int) -> str | None:
        """Feed every per-case exit code in. Returns a human-readable reason
        to halt (for stderr), or None; sets reason_code for the summary."""
        self.calls += 1
        if exit_code == extract.EXIT_BUDGET:
            self.reason_code = "SPEND_EXHAUSTED"
            return "spend ceiling or credit exhausted (exit 5): stop, do not retry"
        if exit_code in FAILURE_EXITS:
            self.failures += 1
            self.consecutive += 1
        else:
            self.consecutive = 0
        if self.consecutive >= self.max_consecutive_failures:
            self.reason_code = "CONSECUTIVE_FAILURES"
            return f"{self.consecutive} consecutive failures: upstream or configuration problem"
        if self.calls >= self.min_calls_for_rate and self.failures / self.calls > self.max_error_rate:
            self.reason_code = "ERROR_RATE"
            return f"error rate {self.failures / self.calls:.0%} exceeds {self.max_error_rate:.0%}"
        return None


@dataclass
class BatchConfig:
    gold_version: str = DEFAULT_GOLD_VERSION
    gold_path: Path | None = None
    mock: bool = False
    model: str = extract.MODEL
    limit: int | None = None
    run_id: str | None = None
    experiment: str | None = None
    budget_usd: float | None = None
    ledger_path: Path = DEFAULT_LEDGER
    run_cap_usd: float = spend_guard.DEFAULT_RUN_CAP_USD
    data_collection: str = "deny"
    price_in: float | None = None
    price_out: float | None = None
    results_dir: Path = RESULTS_DIR
    cache_dir: Path = CACHE_DIR
    sealed_path: Path | None = None
    sealed_sha_path: Path | None = None
    dataset: str | None = None
    gold_schema_version: str | None = None

    def __post_init__(self):
        """Anything not given explicitly comes from the registered version.
        Explicit values win, so a test can point at a temporary seal."""
        if self.gold_version not in REGISTRY:
            raise Refused("ERR_GOLD_REFUSED",
                          f"unknown gold version {self.gold_version!r}; registered: "
                          f"{', '.join(sorted(REGISTRY))}", EXIT_REFUSED)
        entry = REGISTRY[self.gold_version]
        self.sealed_path = self.sealed_path or entry.path
        self.sealed_sha_path = self.sealed_sha_path or entry.sha_path
        self.gold_path = self.gold_path or self.sealed_path
        self.dataset = self.dataset or entry.dataset
        self.gold_schema_version = self.gold_schema_version or entry.schema_version


def _rel(path: Path) -> str:
    try:
        return str(Path(path).resolve().relative_to(ROOT))
    except ValueError:
        return str(path)


def sha256_of(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load_gold(cfg: BatchConfig) -> tuple[GoldSet, str]:
    """Load the gold set and refuse anything a live run must not trust."""
    if not cfg.gold_path.is_file():
        entry = REGISTRY.get(cfg.gold_version)
        detail = f" ({entry.describes})" if entry else ""
        raise Refused(
            "ERR_GOLD_REFUSED",
            f"{cfg.gold_version} is not sealed: {_rel(cfg.gold_path)} does not exist"
            f"{detail}. Label it, then run label_gold_v1.py validate --seal.",
            EXIT_REFUSED)
    gold = GoldSet.model_validate_json(cfg.gold_path.read_text(encoding="utf-8"))
    digest = sha256_of(cfg.gold_path)
    problems = []

    row_map = gold.provenance.get("row_map") or {}
    for case in gold.cases:
        expected = row_map.get(case.case_id, {}).get("text_md5")
        if expected and hashlib.md5(case.source_text.encode()).hexdigest() != expected:
            problems.append(f"{case.case_id}: note text drifted from the md5 recorded at pull")

    if not cfg.mock:
        if cfg.gold_path.resolve() != cfg.sealed_path.resolve():
            problems.append(
                f"live runs score only the sealed {_rel(cfg.sealed_path)}, not "
                f"{_rel(cfg.gold_path)}: label, then run label_gold_v1.py validate --seal")
        elif not cfg.sealed_sha_path.is_file():
            problems.append(f"{_rel(cfg.sealed_sha_path)} is missing: {cfg.gold_version} "
                            "was not sealed by the tool")
        elif cfg.sealed_sha_path.read_text(encoding="utf-8").split()[0] != digest:
            problems.append(f"{_rel(cfg.gold_path)} no longer matches {_rel(cfg.sealed_sha_path)}: "
                            f"it was edited after sealing; {cfg.gold_version} is immutable")
        if gold.provenance.get("dataset") != cfg.dataset:
            problems.append(f"{cfg.gold_version} must be pulled from {cfg.dataset}, but this "
                            f"file records {gold.provenance.get('dataset')!r}")
        if gold.schema_version != cfg.gold_schema_version:
            # Third signal after path identity and the hash: a file that does
            # not say which version it is cannot be scored as that version.
            problems.append(f"{_rel(cfg.gold_path)} declares schema_version "
                            f"{gold.schema_version!r}, but {cfg.gold_version} expects "
                            f"{cfg.gold_schema_version!r}")
        unlabelled = [c.case_id for c in gold.cases if not c.is_complete]
        if unlabelled:
            problems.append(f"{len(unlabelled)} of {len(gold.cases)} cases have unlabelled fields")
        sealed_fp = gold.provenance.get("prompt_fingerprint")
        current_fp = extract.prompt_fingerprint()
        if sealed_fp is None:
            problems.append("the seal carries no prompt fingerprint: seal with the current labelling tool")
        elif sealed_fp != current_fp and not cfg.experiment:
            problems.append(f"the prompt changed since gold-v1 was sealed ({sealed_fp} -> {current_fp}); "
                            "rerun with --experiment NAME so its results are reported separately")

    if problems:
        raise Refused("ERR_GOLD_REFUSED", "; ".join(problems), EXIT_REFUSED)
    return gold, digest


def _identity(cfg: BatchConfig) -> dict:
    return {"model": "mock" if cfg.mock else cfg.model,
            "prompt_fingerprint": extract.prompt_fingerprint(), "mock": cfg.mock}


def _cache_file(cfg: BatchConfig, run_id: str, case_id: str) -> Path:
    return cfg.cache_dir / run_id / f"{case_id}.json"


def load_cached(cfg: BatchConfig, run_id: str, case_id: str, identity: dict) -> dict | None:
    path = _cache_file(cfg, run_id, case_id)
    if not path.is_file():
        return None
    entry = json.loads(path.read_text(encoding="utf-8"))
    if entry.get("identity") != identity:
        raise Refused(
            "ERR_CACHE_MISMATCH",
            f"run {run_id} already holds {case_id} from {entry.get('identity')}, but this run is "
            f"{identity}; mixing them would pool two different systems - use a new --run-id",
            EXIT_REFUSED,
        )
    return entry


def save_cached(cfg: BatchConfig, run_id: str, case_id: str, entry: dict) -> None:
    path = _cache_file(cfg, run_id, case_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(entry, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def worst_case_one(cfg: BatchConfig, case, prices) -> float:
    """Upper bound for one case: its schema retry taken, its output hitting
    the token cap. The per-run cap in metrics.py is checked against this
    before each call, so a runaway is refused rather than discovered."""
    _, request = extract.build_request(cfg.model, case.source_text, cfg.data_collection)
    chars = sum(len(m["content"]) for m in request["messages"])
    chars += len(json.dumps(request["response_format"]))
    return extract.SCHEMA_ATTEMPTS * worst_case_cost(chars, extract.MAX_OUTPUT_TOKENS, *prices)


def worst_case_for(cfg: BatchConfig, cases, prices) -> float:
    """Upper bound for calling these cases: every one needing its schema
    retry, every output hitting the token cap."""
    return sum(worst_case_one(cfg, case, prices) for case in cases)


def _read_only_git(*args: str) -> str | None:
    """Provenance only. Read-only commands; this tool never changes git."""
    try:
        done = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def _version(package: str) -> str | None:
    try:
        return importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError:
        return None


def _short(code: str) -> str:
    return {"VERIFIED": "OK", "NOT_STATED": "--"}.get(code, code.replace("ABSTAIN_", "X:").replace("REVIEW_", "R:"))


def run_batch(cfg: BatchConfig, client=None) -> tuple[dict, int]:
    """The whole run. `client` is for tests; live runs build an OpenAI
    client for OpenRouter once and reuse it for every case."""
    started_at = datetime.now(timezone.utc)
    run_id = cfg.run_id or (
        f"{started_at:%Y%m%dT%H%M%SZ}-{'mock' if cfg.mock else cfg.model.split('/')[-1]}")
    gold, gold_sha = load_gold(cfg)
    cases = gold.cases[: cfg.limit] if cfg.limit else list(gold.cases)
    identity = _identity(cfg)
    cached = {c.case_id: load_cached(cfg, run_id, c.case_id, identity) for c in cases}
    anomalous = {c.case_id for c in cases if scan_input(c.source_text).encoding_anomaly}
    to_call = [c for c in cases if cached[c.case_id] is None and c.case_id not in anomalous]

    prices = ledger = ceiling = None
    spent_before = None
    if not cfg.mock:
        prices = extract.resolve_prices(cfg.model, cfg.price_in, cfg.price_out)
        ceiling = extract.resolve_budget(cfg.budget_usd)
        ledger = SpendLedger(cfg.ledger_path)
        spent_before = ledger.spent()
        worst = worst_case_for(cfg, to_call, prices)
        if worst > ceiling - spent_before:
            raise Refused(
                "ERR_BUDGET_EXCEEDED",
                f"the {len(to_call)} uncached cases could cost up to ${worst:.4f}; only "
                f"${ceiling - spent_before:.4f} of the ${ceiling:.2f} ceiling remains",
                EXIT_BUDGET,
            )
        if client is None and to_call:  # no client at all if every case is cached or anomalous
            import openai
            client_kwargs, _ = extract.build_request(cfg.model, "", cfg.data_collection)
            client = openai.OpenAI(api_key=extract.load_api_key(), **client_kwargs)

    # Two bounds, not one: `ceiling` is the project's $8, `run_cap_usd` is this
    # run's. A loop that stayed under the project ceiling could still burn it.
    guard = spend_guard.CostGuard(run_id=run_id, cap_usd=cfg.run_cap_usd, ledger=ledger,
                              ceiling_usd=ceiling, expected_calls=len(to_call),
                              verbose=False)
    print(f"run {run_id}: {len(cases)} cases ({len(cases) - len(to_call)} cached), "
          f"{'mock' if cfg.mock else cfg.model}, prompt {identity['prompt_fingerprint']}"
          + ("" if cfg.mock else f", run cap ${cfg.run_cap_usd:.2f}"),
          file=sys.stderr)

    breaker = CircuitBreaker()
    gated, ungated, exit_counts, errors = {}, {}, Counter(), {}
    halted = None
    for index, case in enumerate(cases, start=1):
        entry = cached[case.case_id]
        if entry is None:
            try:
                extract.check_text(case.source_text)
                if case.case_id in anomalous:
                    # Forced safe abstention: no call is made for this note.
                    extraction = wiped_extraction()
                    usage, api_ms = {"called": False, "billed_usd": 0.0}, 0.0
                    provenance = {"requested_model": identity["model"], "called": False,
                                  "prompt_fingerprint": identity["prompt_fingerprint"]}
                elif cfg.mock:
                    extraction = ClinicalExtraction.model_validate(extract.MOCK_RESPONSE)
                    usage, api_ms = {"called": False, "billed_usd": 0.0}, 0.0
                    provenance = {"called": False, "requested_model": "mock",
                                  "prompt_fingerprint": identity["prompt_fingerprint"]}
                else:
                    try:
                        guard.check_before(worst_case_one(cfg, case, prices))
                    except extract.BudgetExceeded as exc:
                        # Same code and the same immediate halt as the global
                        # ceiling: a spend refusal is never an abstention.
                        raise extract.PipelineError(
                            "ERR_BUDGET_EXCEEDED", str(exc), extract.EXIT_BUDGET) from None
                    result = extract.call_model(case.source_text, cfg.model, prices, ledger,
                                                ceiling, cfg.data_collection, client=client)
                    extraction, usage, provenance, api_ms = (
                        result.extraction, result.usage, result.provenance, result.api_ms)
                    guard.record(spend_guard.CallRecord.from_result(
                        run_id, case.case_id, cfg.model, usage, provenance, api_ms))
            except extract.PipelineError as exc:
                envelope = extract.error_envelope(exc.code, case.case_id)
                gated[case.case_id] = ungated[case.case_id] = envelope
                exit_counts[exc.exit_code] += 1
                errors[case.case_id] = {"code": exc.code, "exit_code": exc.exit_code,
                                        "message": exc.message}
                print(f"[{index:>2}/{len(cases)}] {case.case_id}  {exc.code} (exit {exc.exit_code}): "
                      f"{exc.message}", file=sys.stderr)
                halted = breaker.record(exc.exit_code)
                if halted:
                    print(f"HALT: {halted}", file=sys.stderr)
                    break
                continue
            entry = {"identity": identity, "case_id": case.case_id,
                     "extraction": extraction.model_dump(mode="json"),
                     "usage": usage, "provenance": provenance, "api_ms": api_ms}
            save_cached(cfg, run_id, case.case_id, entry)

        extraction = ClinicalExtraction.model_validate(entry["extraction"])
        scan = scan_input(case.source_text)
        arms = {}
        for arm, enabled in (("gated", True), ("ungated", False)):
            arms[arm] = extract.build_payload(
                extraction, case.source_text, scan, enabled,
                note=case.case_id, model_label=identity["model"], usage=entry["usage"],
                provenance=entry["provenance"], api_ms=entry["api_ms"], started=time.perf_counter(),
            )
        gated[case.case_id], exit_code, results = arms["gated"]
        ungated[case.case_id] = arms["ungated"][0]
        exit_counts[exit_code] += 1
        breaker.record(exit_code)
        codes = "  ".join(f"{r.field[:4]}={_short(r.code)}" for r in results)
        billed = entry["usage"].get("billed_usd", 0.0) or 0.0
        running = ("" if cfg.mock
                   else f"  run ${guard.spent_usd:.4f}/${cfg.run_cap_usd:.2f}")
        print(f"[{index:>2}/{len(cases)}] {case.case_id}  {codes}  ${billed:.4f}{running}"
              f"{'  (cached)' if cached[case.case_id] else ''}", file=sys.stderr)

    all_labelled = all(c.is_complete for c in cases)
    scores = None
    if all_labelled and not halted:
        slices = {tag: {c.case_id for c in cases if c.notes and f"#{tag}" in c.notes.lower()}
                  for tag in SLICE_TAGS}
        scores = scoring.score([c.model_dump(mode="json") for c in cases], gated, ungated,
                               {k: v for k, v in slices.items() if v})

    finished_at = datetime.now(timezone.utc)
    billed_this_run = round(sum(
        (p.get("usage") or {}).get("billed_usd", 0.0) or 0.0
        for case_id, p in gated.items() if p.get("status") == "ok" and not cached[case_id]), 6)
    reportable = (not cfg.mock and not halted and len(cases) == len(gold.cases))
    manifest = {
        "run_id": run_id,
        "started_at": started_at.isoformat(timespec="seconds"),
        "finished_at": finished_at.isoformat(timespec="seconds"),
        "mode": "mock" if cfg.mock else "live",
        "reportable": reportable,
        "experiment": cfg.experiment,
        "model": identity["model"],
        "prompt_fingerprint": identity["prompt_fingerprint"],
        "data_collection": cfg.data_collection,
        "gold": {
            "version": cfg.gold_version, "dataset": cfg.dataset,
            "path": _rel(cfg.gold_path), "sha256": gold_sha,
            "cases_total": len(gold.cases), "cases_run": len(cases),
            "sealed_at": gold.provenance.get("sealed_at"),
            "sealed_prompt_fingerprint": gold.provenance.get("prompt_fingerprint"),
            "dataset_revision": gold.provenance.get("revision"),
        },
        "environment": {
            "git_commit": _read_only_git("rev-parse", "--short", "HEAD"),
            "git_dirty": bool(_read_only_git("status", "--porcelain")),
            "python": platform.python_version(), "openai": _version("openai"),
            "pydantic": _version("pydantic"),
        },
        "spend": {"ceiling_usd": ceiling, "spent_before_usd": spent_before,
                  "billed_this_run_usd": billed_this_run},
        "breaker": {"halted": bool(halted), "reason_code": breaker.reason_code, "reason": halted,
                    "calls": breaker.calls, "failures": breaker.failures},
        "errors": errors,
        "exit_counts": {str(k): v for k, v in sorted(exit_counts.items())},
        "cached_cases": sum(1 for c in cases if cached[c.case_id]),
    }

    out_dir = cfg.results_dir / run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    for arm, payloads in (("gated", gated), ("ungated", ungated)):
        with open(out_dir / f"{arm}.jsonl", "w", encoding="utf-8") as handle:
            for case_id, payload in payloads.items():
                handle.write(json.dumps({"case_id": case_id, **payload}) + "\n")
    if scores is not None:
        scoring.write_rows_csv(out_dir / "rows.csv", scores["rows"])
        (out_dir / "scores.json").write_text(
            json.dumps({k: v for k, v in scores.items() if k != "rows"}, indent=2) + "\n",
            encoding="utf-8")

    summary = {
        "status": "halted" if halted else "done",
        "run_id": run_id,
        "mode": manifest["mode"],
        "reportable": reportable,
        "experiment": cfg.experiment,
        "model": identity["model"],
        "prompt_fingerprint": identity["prompt_fingerprint"],
        "gold_sha256": gold_sha,
        "cases_run": len(cases),
        "exit_counts": manifest["exit_counts"],
        "billed_this_run_usd": billed_this_run,
        "breaker": {k: manifest["breaker"][k] for k in ("halted", "reason_code", "calls", "failures")},
        "error_codes": {case_id: e["code"] for case_id, e in errors.items()},
        "scored": scores is not None,
        "unscored_code": None if scores is not None else ("HALTED" if halted else "GOLD_INCOMPLETE"),
        "scores": None if scores is None else {
            "coverage": scores["coverage"], "errored_cases": scores["errored_cases"],
            "pooled": scores["pooled"],
            "per_field": {f: {k: v for k, v in c.items() if k in (
                "recall", "precision", "abstention_rate", "silent_failure_rate")}
                for f, c in scores["per_field"].items()},
            "per_slice": {n: {k: v for k, v in c.items() if k in (
                "recall", "precision", "silent_failure_rate")}
                for n, c in scores["per_slice"].items()},
        },
        "results_dir": _rel(out_dir),
        # Financial audit: tokens, dollars and destination for every call.
        "cost": guard.summary(),
    }
    guard.write_summary(out_dir / "metrics.json")
    print_evaluation_table(summary, scores, sys.stderr)
    guard.print_summary(sys.stderr)
    if breaker.reason_code == "SPEND_EXHAUSTED":
        # A spend refusal is a spend refusal wherever it happens. Returning the
        # generic halt code here would file it under "upstream problem" and let
        # a budget breach hide among network failures (CLAUDE.md 3.3).
        return summary, EXIT_BUDGET
    return summary, EXIT_HALTED if halted else EXIT_DONE


def print_evaluation_table(summary: dict, scores: dict | None, out) -> None:
    """The human-readable result, on stderr. stdout carries only the JSON."""
    print(f"\nrun {summary['run_id']} ({summary['mode']}): {summary['cases_run']} cases, "
          f"exit codes {summary['exit_counts']}, billed ${summary['billed_this_run_usd']:.4f}",
          file=out)
    if scores is None:
        print(f"not scored: {summary['unscored_code']}", file=out)
        return

    def pct(value):
        return "  -  " if value is None else f"{value:5.1%}"

    print(f"{'':<12}{'recall':>8}{'precision':>11}{'abstain':>9}{'silent':>8}", file=out)
    rows = [("pooled", scores["pooled"]), *scores["per_field"].items()]
    for name, c in rows:
        print(f"{name:<12}{pct(c['recall']):>8}{pct(c['precision']):>11}"
              f"{pct(c['abstention_rate']):>9}{pct(c['silent_failure_rate']):>8}", file=out)
    low, high = scores["pooled"]["recall_ci95"]
    print(f"pooled recall 95% CI {low:.2f}-{high:.2f}; coverage {scores['coverage']:.0%}; "
          f"forced abstentions {scores['pooled']['forced']}; errored {len(scores['errored_cases'])}",
          file=out)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--gold-version", choices=sorted(REGISTRY),
                        default=DEFAULT_GOLD_VERSION,
                        help="which registered gold set to score against "
                             f"(default {DEFAULT_GOLD_VERSION}); "
                             + "; ".join(f"{k}: {v.describes}" for k, v in sorted(REGISTRY.items())))
    parser.add_argument("--gold", type=Path, default=None,
                        help="an explicit gold file; a live run still accepts only the "
                             "sealed path of the chosen --gold-version")
    parser.add_argument("--mock", action="store_true", help="no API call; canned response per case")
    parser.add_argument("--model", default=extract.MODEL)
    parser.add_argument("--limit", type=int, default=None, help="first N cases only (not reportable)")
    parser.add_argument("--run-id", default=None, help="reuse to resume a run from its cache")
    parser.add_argument("--experiment", default=None,
                        help="name a run whose prompt differs from the one sealed with gold-v1")
    parser.add_argument("--budget-usd", type=float, default=None,
                        help="the project-wide ceiling (default from the environment or $8)")
    parser.add_argument("--run-cap-usd", type=float, default=spend_guard.DEFAULT_RUN_CAP_USD,
                        help="hard cap for THIS run, checked before every call "
                             f"(default ${spend_guard.DEFAULT_RUN_CAP_USD:.2f})")
    parser.add_argument("--price-in-per-mtok", type=float, default=None)
    parser.add_argument("--price-out-per-mtok", type=float, default=None)
    parser.add_argument("--provider-data-collection", choices=["deny", "allow"], default="deny")
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER, help=argparse.SUPPRESS)
    parser.add_argument("--results-dir", type=Path, default=RESULTS_DIR, help=argparse.SUPPRESS)
    parser.add_argument("--cache-dir", type=Path, default=CACHE_DIR, help=argparse.SUPPRESS)
    parser.add_argument("--debug", action="store_true")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    cfg = BatchConfig(
        gold_version=args.gold_version, gold_path=args.gold, mock=args.mock,
        model=args.model, limit=args.limit,
        run_id=args.run_id, experiment=args.experiment, budget_usd=args.budget_usd,
        ledger_path=args.ledger, run_cap_usd=args.run_cap_usd,
        data_collection=args.provider_data_collection,
        price_in=args.price_in_per_mtok, price_out=args.price_out_per_mtok,
        results_dir=args.results_dir, cache_dir=args.cache_dir,
    )
    stdout = sys.stdout
    try:
        with contextlib.redirect_stdout(sys.stderr):
            summary, code = run_batch(cfg)
    except (Refused, extract.PipelineError) as exc:
        summary = {"status": "refused", "code": exc.code}
        code = exc.exit_code
        print(f"REFUSED {exc.code} (exit {code}): {exc.message}", file=sys.stderr)
    except Exception as exc:
        summary = {"status": "error", "code": "ERR_INTERNAL"}
        code = EXIT_INTERNAL
        print(f"ERROR ERR_INTERNAL (exit {code}): {type(exc).__name__} "
              "(re-run with --debug for the traceback)", file=sys.stderr)
        if args.debug:
            traceback.print_exc()
    stdout.write(json.dumps(summary, indent=2) + "\n")
    stdout.flush()
    return code


if __name__ == "__main__":
    sys.exit(main())
