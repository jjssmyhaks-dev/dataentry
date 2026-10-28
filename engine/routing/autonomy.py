"""Autonomy roadmap and gates (section 12a).

Autonomous posting is the launch strategy, so it is earned in stages. Each stage
opens only when its gate is passed, and the numbers here are starting targets to
be tuned with real customer data: they come from the tenant's settings, not from
constants in the code.

The measurements the gates use are deliberately those that reach Tally and matter
financially — wrong ledger, wrong tax split, unreversed errors — rather than field
accuracy alone.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal

Comparison = Literal[">=", "<=", "<", ">", "=="]

STAGE_NAMES = {
    0: "Foundations",
    1: "Shadow mode",
    2: "Autonomous on low-risk",
    3: "Autonomous on standard",
    4: "Full autonomy with monitoring",
}

STAGE_DESCRIPTIONS = {
    0: "Builds intake, extraction, validation rules, the review queue and the bridge agent. Posts nothing.",
    1: "Processes real documents for design-partner customers and records what it would have posted. Humans still do the entry.",
    2: "Posts on its own for the safest records only: repeat vendors with a learned ledger mapping, passing every rule, below a low value cap.",
    3: "Raises the value cap and widens to all supplier invoices passing validation, then adds bank statement lines and purchase order matching.",
    4: "Posts everything that clears rules and confidence, including compliance-linked documents, under the maintenance service.",
}


@dataclass
class AutonomyMetrics:
    """What the gates measure. Every field is optional so a young tenant can be
    evaluated on the subset it has data for."""

    documents_processed: int = 0
    auto_posted: int = 0
    auto_posted_corrected: int = 0
    unreversed_errors: int = 0
    high_value_auto_posted: int = 0
    high_value_errors: int = 0
    wrong_ledger_errors: int = 0
    wrong_tax_split_errors: int = 0
    reversals: int = 0
    review_items: int = 0
    review_items_pending: int = 0
    median_review_seconds: float | None = None
    reviewed_without_edits: int = 0
    reviewed_total: int = 0
    field_accuracy: float | None = None
    shadow_documents: int = 0
    shadow_discrepancies: int = 0
    customer_confirmed_reconciliation: bool | None = None
    observation_days: int = 0
    # Stage 0's gate is about the pack, not about postings: how many labelled
    # cases it carries, and whether those cases are real customer documents.
    evaluation_cases: int = 0
    evaluation_is_real: int = 0

    @property
    def error_rate(self) -> Decimal:
        """Errors that reached Tally and were not reversed, per auto-posted voucher."""
        if self.auto_posted == 0:
            return Decimal("0")
        return (Decimal(self.auto_posted_corrected + self.unreversed_errors) / Decimal(self.auto_posted)).quantize(
            Decimal("0.0001")
        )

    @property
    def review_share(self) -> float:
        if self.documents_processed == 0:
            return 0.0
        return round(self.review_items / self.documents_processed, 4)

    @property
    def clean_review_share(self) -> float | None:
        if self.reviewed_total == 0:
            return None
        return round(self.reviewed_without_edits / self.reviewed_total, 4)


@dataclass
class GateCheck:
    key: str
    label: str
    metric: str
    comparison: Comparison
    target: float
    actual: float | Decimal | None
    passed: bool

    @property
    def detail(self) -> str:
        actual = "no data" if self.actual is None else str(self.actual)
        return f"{self.label}: {actual} (target {self.comparison} {self.target})"


@dataclass
class GateResult:
    stage: int
    next_stage: int
    checks: list[GateCheck] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(c.passed for c in self.checks)

    @property
    def blocking(self) -> list[GateCheck]:
        return [c for c in self.checks if not c.passed]

    def summary(self) -> dict:
        return {
            "stage": self.stage,
            "stage_name": STAGE_NAMES.get(self.stage, "unknown"),
            "next_stage": self.next_stage,
            "passed": self.passed,
            "checks": [
                {
                    "key": c.key,
                    "label": c.label,
                    "actual": None if c.actual is None else float(c.actual),
                    "target": c.target,
                    "comparison": c.comparison,
                    "passed": c.passed,
                }
                for c in self.checks
            ],
        }


def _compare(actual: float, comparison: Comparison, target: float) -> bool:
    if comparison == ">=":
        return actual >= target
    if comparison == "<=":
        return actual <= target
    if comparison == "<":
        return actual < target
    if comparison == ">":
        return actual > target
    return actual == target


def _check(
    key: str, label: str, metric: str, comparison: Comparison, target: float, actual: float | None
) -> GateCheck:
    passed = actual is not None and _compare(float(actual), comparison, target)
    return GateCheck(
        key=key,
        label=label,
        metric=metric,
        comparison=comparison,
        target=target,
        actual=actual,
        passed=passed,
    )


# Gate definitions. Every threshold is a proposal from section 12a to be tuned with
# a design partner: keep them visible and adjustable rather than buried.
GATE_CHECKS: dict[int, list[tuple[str, str, str, Comparison, float]]] = {
    # Leaving stage 0 also requires the pack and the bridge agent, which are
    # verified by the evaluation harness and the agent's stage-0 test post, not here.
    0: [
        ("eval_set_size", "Labelled documents in the evaluation set", "evaluation_cases", ">=", 1),
        (
            "eval_set_real",
            "Those documents are real design-partner files, not generated samples",
            "evaluation_is_real",
            "==",
            1,
        ),
    ],
    # Stage 1 → 2: field accuracy on the evaluation set, every discrepancy reviewed.
    1: [
        ("field_accuracy", "Field-level accuracy on the evaluation set", "field_accuracy", ">=", 0.97),
        ("shadow_reviewed", "Shadow documents compared against the human entry", "shadow_documents", ">=", 100),
        ("unexplained", "Unexplained discrepancies left", "shadow_discrepancies", "<=", 0),
    ],
    # Stage 2 → 3: error rate on auto-posted vouchers, zero unreversed errors.
    2: [
        ("error_rate", "Error rate on auto-posted vouchers", "error_rate", "<", 0.005),
        ("unreversed", "Unreversed errors on high-value vouchers", "high_value_errors", "<=", 0),
        ("observation", "Days of observation at this stage", "observation_days", ">=", 30),
    ],
    # Stage 3 → 4: the same accuracy at higher volume, falling review share.
    3: [
        ("error_rate", "Error rate on auto-posted vouchers", "error_rate", "<", 0.005),
        ("wrong_ledger", "Wrong-ledger errors", "wrong_ledger_errors", "<=", 0),
        ("wrong_tax", "Wrong tax-split errors", "wrong_tax_split_errors", "<=", 0),
        ("review_share", "Share of documents going to review", "review_share", "<=", 0.25),
    ],
}


def evaluate_gate(stage: int, metrics: AutonomyMetrics) -> GateResult:
    """Report whether a tenant may move from ``stage`` to ``stage + 1``."""
    definitions = GATE_CHECKS.get(stage, [])
    checks: list[GateCheck] = []
    for key, label, metric, comparison, target in definitions:
        actual = getattr(metrics, metric, None)
        checks.append(
            _check(key, label, metric, comparison, target, None if actual is None else float(actual))
        )
    return GateResult(stage=stage, next_stage=min(stage + 1, 4), checks=checks)


def should_auto_demote(
    *,
    error_limit: Decimal,
    metrics: AutonomyMetrics,
    auto_demote_enabled: bool = True,
) -> tuple[bool, str]:
    """Whether the engine must move a tenant back to draft mode on its own.

    Appendix C: if the error rate crosses a set limit, the tenant is moved to
    draft mode automatically and the team is alerted.
    """
    if not auto_demote_enabled:
        return False, "automatic demotion is switched off for this tenant"
    if metrics.unreversed_errors > 0:
        return True, f"{metrics.unreversed_errors} unreversed posting error(s) recorded"
    if metrics.error_rate > error_limit:
        return True, (
            f"error rate {metrics.error_rate} is above the tenant limit of {error_limit}"
        )
    return False, "within limits"


def describe_stage(stage: int) -> dict[str, object]:
    return {
        "stage": stage,
        "name": STAGE_NAMES.get(stage, "unknown"),
        "description": STAGE_DESCRIPTIONS.get(stage, ""),
        "gate_to_next": [
            {
                "key": key,
                "label": label,
                "metric": metric,
                "comparison": comparison,
                "target": target,
            }
            for key, label, metric, comparison, target in GATE_CHECKS.get(stage, [])
        ],
    }
