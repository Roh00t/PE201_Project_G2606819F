"""Hard spend ceiling for rented model calls.

The project has $8 of API credit in total. Every live call first checks its
worst-case cost against a ledger, and records what it actually cost after.
When the next worst case would cross the ceiling, the call is refused (exit
code 5) instead of the overspend being discovered on the invoice.

The ledger holds totals only - never note text, evidence or values - and
lives under data/cache/, which .gitignore already excludes.
"""

import json
import math
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_LEDGER = Path(__file__).resolve().parent.parent / "data" / "cache" / "spend_ledger.json"
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
