"""Pre-fetched lookups handed to the rule checks.

The checks themselves are pure functions, so they can be unit tested without a
database or a network. Everything that needs I/O — GSTN status, duplicate
similarity search, purchase order matching, GSTR-2B — is resolved up front into
this bundle by ``engine.validation.rules.RuleEngine``.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any
from uuid import UUID

from engine.validation.gstn import GstinStatus


@dataclass
class DuplicateFinding:
    """Duplicate detection (6.3): exact key match, or near-identical content."""

    exact: bool = False
    similarity: float = 0.0
    matched_record_id: UUID | None = None
    matched_document_number: str | None = None
    matched_date: dt.date | None = None
    method: str | None = None  # key_fields | pgvector | none

    @property
    def near(self) -> bool:
        return not self.exact and self.similarity > 0


@dataclass
class ThreeWayFinding:
    """Invoice against purchase order against goods receipt (6.3)."""

    po_number: str | None = None
    po_found: bool = False
    goods_receipt_found: bool = False
    quantity_variance_pct: Decimal | None = None
    rate_variance_pct: Decimal | None = None
    unmatched_lines: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def checked(self) -> bool:
        return self.po_found or self.goods_receipt_found

    @property
    def worst_variance_pct(self) -> Decimal | None:
        candidates = [
            v for v in (self.quantity_variance_pct, self.rate_variance_pct) if v is not None
        ]
        return max(candidates) if candidates else None


@dataclass
class Gstr2bFinding:
    found: bool = False
    period: str | None = None
    value_difference: Decimal | None = None
    tax_difference: Decimal | None = None
    degraded: bool = False
    detail: str | None = None


@dataclass
class EwayBillFinding:
    number: str | None = None
    value: Decimal | None = None
    supplier_gstin: str | None = None
    recipient_gstin: str | None = None
    document_number: str | None = None
    valid_upto: dt.date | None = None
    expired: bool = False


@dataclass
class LedgerMapping:
    party_gstin: str | None = None
    party_name: str | None = None
    target_party_ledger: str | None = None
    purchase_ledger: str | None = None
    expense_ledger: str | None = None
    cgst_ledger: str | None = None
    sgst_ledger: str | None = None
    igst_ledger: str | None = None
    roundoff_ledger: str | None = None
    tds_ledger: str | None = None
    default_stock_item: str | None = None
    confidence: float = 0.0
    source: str = "pack_default"

    @property
    def is_known_vendor(self) -> bool:
        return bool(self.target_party_ledger)


@dataclass
class LookupBundle:
    """Everything the checks may consult. An empty bundle is valid: rules that
    need a lookup then degrade to a warning instead of failing."""

    gstin_status: dict[str, GstinStatus] = field(default_factory=dict)
    duplicate: DuplicateFinding | None = None
    three_way: ThreeWayFinding | None = None
    gstr2b: Gstr2bFinding | None = None
    eway_bill: EwayBillFinding | None = None
    ledger_mapping: LedgerMapping | None = None
    previous_balance: Decimal | None = None
    open_periods: list[tuple[dt.date, dt.date]] = field(default_factory=list)
    e_invoicing_applies: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    def status_of(self, gstin: str | None) -> GstinStatus | None:
        if not gstin:
            return None
        return self.gstin_status.get(gstin)


@dataclass
class VendorHistory:
    """Per-tenant learning lookup (6.6) used by the mapper, not by a rule."""

    vendor_gstin: str | None = None
    correction_count: int = 0
    common_expense_ledger: str | None = None
    field_hints: dict[str, Any] = field(default_factory=dict)
