"""Domain models shared across the pipeline.

These are the engine's internal vocabulary: what a field value carries, what a
rule returns, how a routing decision is expressed, and what a voucher looks like
before it reaches a connector.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

RuleOutcome = Literal["pass", "warn", "fail", "skipped"]
RuleSeverity = Literal["fail", "warn", "info"]
RoutingDecisionName = Literal["auto_post", "review", "exception"]
PostingMode = Literal["shadow", "draft", "autonomous"]


class FieldRef(BaseModel):
    """Where a value was found, so the reviewer can click through to the page."""

    page_no: int | None = None
    bbox: dict[str, float] | None = None  # normalised {x,y,w,h} in 0..1
    source_span: str | None = None


class FieldValue(BaseModel):
    """One extracted field: a value, a confidence, and a page pointer."""

    model_config = ConfigDict(extra="ignore")

    path: str
    label: str | None = None
    value_text: str | None = None
    value_num: Decimal | None = None
    value_date: dt.date | None = None
    value_json: Any | None = None
    confidence: float = 0.0
    page_no: int | None = None
    bbox: dict[str, float] | None = None
    source_span: str | None = None
    mandatory: bool = False
    corrected: bool = False
    corrected_by: str | None = None


class RuleResult(BaseModel):
    """One rule's verdict on one record (Appendix B)."""

    rule_key: str
    rule_version: str | None = None
    severity: RuleSeverity = "warn"
    outcome: RuleOutcome
    title: str | None = None
    message: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)
    fields: list[str] = Field(default_factory=list)
    degraded: bool = False
    recheck_at: dt.datetime | None = None

    @property
    def blocks_auto_post(self) -> bool:
        return self.outcome == "fail"

    @property
    def lowers_confidence(self) -> bool:
        return self.outcome == "warn"


class ValidationSummary(BaseModel):
    results: list[RuleResult] = Field(default_factory=list)

    @property
    def failures(self) -> list[RuleResult]:
        return [r for r in self.results if r.outcome == "fail"]

    @property
    def warnings(self) -> list[RuleResult]:
        return [r for r in self.results if r.outcome == "warn"]

    @property
    def degraded(self) -> list[RuleResult]:
        return [r for r in self.results if r.degraded]

    @property
    def failed_keys(self) -> list[str]:
        return [r.rule_key for r in self.failures]

    @property
    def warned_keys(self) -> list[str]:
        return [r.rule_key for r in self.warnings]

    @property
    def all_pass(self) -> bool:
        return not self.failures and not self.warnings


class ExtractedRecord(BaseModel):
    """A single logical document lifted out of a file."""

    doc_type: str
    pack_key: str
    pack_version: str
    payload: dict[str, Any]
    fields: list[FieldValue] = Field(default_factory=list)
    page_from: int | None = None
    page_to: int | None = None
    order_index: int = 0
    model: str | None = None
    prompt_version: str | None = None
    schema_ok: bool = True

    @property
    def overall_confidence(self) -> float:
        """Mean of per-field confidence; mandatory fields weigh double."""
        weighted = 0.0
        weight = 0.0
        for field in self.fields:
            w = 2.0 if field.mandatory else 1.0
            weighted += field.confidence * w
            weight += w
        return round(weighted / weight, 4) if weight else 0.0

    @property
    def min_field_confidence(self) -> float:
        return round(min((f.confidence for f in self.fields), default=0.0), 4)

    def low_confidence(self, threshold: float, mandatory_only: bool = True) -> list[str]:
        return [
            f.path
            for f in self.fields
            if f.confidence < threshold and (f.mandatory or not mandatory_only)
        ]

    def field_map(self) -> dict[str, FieldValue]:
        return {f.path: f for f in self.fields}


class RoutingInput(BaseModel):
    """Everything the confidence router needs to decide (section 6.4)."""

    record: ExtractedRecord
    validation: ValidationSummary
    confidence_threshold: float
    autopost_value_cap: Decimal | None = None
    posting_mode: PostingMode = "draft"
    autonomy_stage: int = 0
    is_known_vendor: bool = False
    ledger_mapping_confidence: float | None = None
    daily_autopost_used: int = 0
    daily_autopost_cap: int | None = None
    duplicate_exact: bool = False
    document_value: Decimal | None = None
    critical_rules: list[str] = Field(default_factory=list)
    # Tenant policy: whether a rule warning may still auto-post. Conservative by
    # default (section 6.4: a warn lowers confidence and may route to review).
    warns_allow_autopost: bool = False
    # Appendix D: records near a GST cut-off or vendor payment date are raised.
    due_soon: bool = False
    # Appendix D: approval rights limited by amount.
    approval_limit: Decimal | None = None


class RoutingReason(BaseModel):
    code: str
    message: str
    fields: list[str] = Field(default_factory=list)
    rule_key: str | None = None


class RoutingDecision(BaseModel):
    decision: RoutingDecisionName
    reasons: list[RoutingReason] = Field(default_factory=list)
    priority: int = 100
    priority_band: Literal["rule_failure", "low_confidence", "routine"] = "routine"
    posting_mode: PostingMode = "draft"
    requires_supervisor: bool = False

    @property
    def needs_human(self) -> bool:
        return self.decision != "auto_post"


class VoucherLine(BaseModel):
    ledger: str | None = None
    stock_item: str | None = None
    amount: Decimal = Decimal("0")
    is_debit: bool = True
    description: str | None = None
    quantity: Decimal | None = None
    unit: str | None = None
    rate: Decimal | None = None
    gst_rate: Decimal | None = None
    hsn: str | None = None


class VoucherDraft(BaseModel):
    """A voucher as it will appear in Tally (Appendix C)."""

    voucher_type: str
    voucher_date: dt.date | None = None
    party_ledger: str | None = None
    narration: str | None = None
    place_of_supply: str | None = None
    lines: list[VoucherLine] = Field(default_factory=list)
    amount_total: Decimal = Decimal("0")
    gst_cgst: Decimal = Decimal("0")
    gst_sgst: Decimal = Decimal("0")
    gst_igst: Decimal = Decimal("0")
    roundoff: Decimal = Decimal("0")
    idempotency_key: str = ""
    tally_company: str | None = None
    warnings: list[str] = Field(default_factory=list)
    proposals: list[dict[str, Any]] = Field(default_factory=list)

    def totals_balanced(self, tolerance: Decimal = Decimal("0.02")) -> bool:
        debit = sum((line.amount for line in self.lines if line.is_debit), Decimal("0"))
        credit = sum((line.amount for line in self.lines if not line.is_debit), Decimal("0"))
        return abs(debit - credit) <= tolerance
