"""Record to voucher mapping (Appendix C).

Every approved record becomes one voucher, built here so the review queue can show
the same ledgers, amounts and GST split the connector will send. Ledgers come from
the tenant's own masters: the connector matches to them and *proposes* anything
missing for approval rather than creating it silently.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from engine.db import TenantRef
from engine.models import ExtractedRecord, VoucherDraft, VoucherLine
from engine.packs.loader import Pack
from engine.validation.gstn import state_code_of_gstin, state_code_of_place_of_supply
from engine.validation.lookups import LedgerMapping

ZERO = Decimal("0")
TWO = Decimal("0.01")

DOC_TYPE_SHORT = {
    "supplier_invoice": "INV",
    "supplier_credit_note": "CRN",
    "purchase_order": "PO",
    "goods_receipt": "GRN",
    "e_way_bill": "EWB",
    "e_invoice": "EINV",
    "bank_statement": "BNK",
}


@dataclass
class MappingResult:
    draft: VoucherDraft
    proposals: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def can_post(self) -> bool:
        return bool(self.draft.lines) and self.draft.totals_balanced()


def _dec(value: Any) -> Decimal:
    if value is None:
        return ZERO
    try:
        return Decimal(str(value))
    except Exception:
        return ZERO


def _money(value: Decimal) -> Decimal:
    return value.quantize(TWO)


def conditions_for(record: ExtractedRecord, mapping: LedgerMapping | None, tenant: TenantRef) -> str:
    """Which voucher-map entry applies (Appendix C)."""
    if record.doc_type == "supplier_invoice":
        policy = (tenant.settings if hasattr(tenant, "settings") else {}) or {}
        uses_inventory = bool(
            (mapping and mapping.default_stock_item)
            or any(line.get("hsn") for line in (record.payload.get("line_items") or []))
        )
        if uses_inventory and not policy.get("inventory_off"):
            return "has_stock_items"
        return "services_or_expenses"
    if record.doc_type == "supplier_credit_note":
        return "always"
    if record.doc_type in {"purchase_order", "goods_receipt"}:
        return "tenant_uses_order_processing"
    if record.doc_type == "bank_statement":
        debit = record.payload.get("debit")
        credit = record.payload.get("credit")
        category = record.payload.get("category")
        if category in {"bank_charges", "interest"}:
            return "category_in(bank_charges,interest)"
        return "payment_out" if debit is not None else "receipt_in"
    return "always"


def narration_for(
    record: ExtractedRecord, record_id: uuid.UUID, pack: Pack
) -> str:
    """The engine's record reference travels in the narration so every posted
    voucher can be traced back to its source document (Appendix C)."""
    template = pack.connectors.narration_template
    try:
        text = template.format(
            doc_type_short=DOC_TYPE_SHORT.get(record.doc_type, "DOC"),
            record_short=str(record_id)[:8],
            vendor_name=record.payload.get("vendor_name") or record.payload.get("party_name") or "",
            invoice_number=record.payload.get("invoice_number")
            or record.payload.get("document_number")
            or "",
        )
    except KeyError:
        text = f"ADE/{DOC_TYPE_SHORT.get(record.doc_type, 'DOC')}/{str(record_id)[:8]}"
    return " ".join(text.split())


def idempotency_key_for(
    record: ExtractedRecord,
    *,
    tenant: TenantRef,
    voucher_type: str,
    business_key: str | None,
    pack: Pack,
) -> str:
    template = pack.connectors.idempotency_key_template
    return template.format(
        tenant_id=tenant.id,
        doc_type=record.doc_type,
        voucher_type=voucher_type,
        business_key=business_key or str(uuid.uuid4())[:12],
    )


def build_voucher(
    record: ExtractedRecord,
    *,
    pack: Pack,
    tenant: TenantRef,
    record_id: uuid.UUID,
    business_key: str | None,
    mapping: LedgerMapping | None = None,
    condition: str | None = None,
    tally_company: str | None = None,
) -> MappingResult:
    condition = condition or conditions_for(record, mapping, tenant)
    payload = record.payload
    proposals: list[dict[str, Any]] = []
    warnings: list[str] = []

    voucher_type = pack.connectors.voucher_type_for(record.doc_type, condition) or _default_type(record)
    tax_ledgers = _tax_ledgers(mapping, payload, proposals)
    party_ledger = (mapping.target_party_ledger if mapping else None) or payload.get("vendor_name")

    if mapping is None or not mapping.target_party_ledger:
        supplier = payload.get("vendor_name") or "Unknown supplier"
        warnings.append(
            f"No ledger mapping for {supplier}; a reviewer must confirm the party ledger."
        )
        proposals.append(
            {
                "kind": "party",
                "proposed_name": supplier,
                "gstin": payload.get("vendor_gstin"),
                "reason": "Supplier found on a document but not present in Tally.",
            }
        )

    lines: list[VoucherLine] = []
    cgst = _money(_dec(payload.get("cgst")))
    sgst = _money(_dec(payload.get("sgst")))
    igst = _money(_dec(payload.get("igst")))
    cess = _money(_dec(payload.get("cess")))
    round_off = _money(_dec(payload.get("round_off")))
    total = _money(_dec(payload.get("total")))

    if record.doc_type == "supplier_invoice":
        lines = _purchase_lines(
            payload, condition, mapping, tax_ledgers, party_ledger, proposals, warnings
        )
        for amount, ledger in ((cgst, tax_ledgers["cgst"]), (sgst, tax_ledgers["sgst"]), (igst, tax_ledgers["igst"]), (cess, None)):
            if amount:
                lines.append(VoucherLine(ledger=ledger, amount=amount, is_debit=True))
        if round_off:
            lines.append(
                VoucherLine(
                    ledger=(mapping.roundoff_ledger if mapping else None),
                    amount=abs(round_off),
                    is_debit=round_off > 0,
                    description="Round off",
                )
            )
        credit = total or _money(
            _dec(payload.get("taxable_value")) + cgst + sgst + igst + cess + round_off
        )
        lines.append(VoucherLine(ledger=party_ledger, amount=credit, is_debit=False))

        tds_amount = _dec(payload.get("tds_amount"))
        if tds_amount or (payload.get("tds_section") and mapping and mapping.tds_ledger):
            if tds_amount:
                lines.append(
                    VoucherLine(ledger=mapping.tds_ledger, amount=tds_amount, is_debit=False)
                )

    elif record.doc_type in {"supplier_credit_note"}:
        base = _money(_dec(payload.get("taxable_value")))
        lines.append(VoucherLine(ledger=(mapping.purchase_ledger if mapping else None), amount=base, is_debit=False))
        for amount, ledger in ((cgst, tax_ledgers["cgst"]), (sgst, tax_ledgers["sgst"]), (igst, tax_ledgers["igst"])):
            if amount:
                lines.append(VoucherLine(ledger=ledger, amount=amount, is_debit=False))
        lines.append(VoucherLine(ledger=party_ledger, amount=total, is_debit=True))

    elif record.doc_type == "bank_statement":
        debit = _money(_dec(payload.get("debit")))
        credit_amount = _money(_dec(payload.get("credit")))
        category = payload.get("category") or "unmatched"
        bank_ledger = (tenant.settings.get("bank_ledger") if tenant.settings else None) or "Bank Account"
        counter = _counter_ledger(category, payload, mapping)
        if counter is None:
            counter = payload.get("matched_party_name") or "Suspense"
            warnings.append(
                "No confident ledger match for this bank line; the counter ledger is a place holder."
            )
        if debit:
            lines = [
                VoucherLine(ledger=counter, amount=debit, is_debit=True),
                VoucherLine(ledger=bank_ledger, amount=debit, is_debit=False),
            ]
        else:
            lines = [
                VoucherLine(ledger=bank_ledger, amount=credit_amount, is_debit=True),
                VoucherLine(ledger=counter, amount=credit_amount, is_debit=False),
            ]

    elif record.doc_type == "purchase_order":
        for item in payload.get("line_items") or []:
            lines.append(
                VoucherLine(
                    stock_item=item.get("description"),
                    ledger=(mapping.purchase_ledger if mapping else None),
                    amount=_money(_dec(item.get("amount"))),
                    is_debit=True,
                    description=item.get("description"),
                    quantity=_dec(item.get("quantity")),
                    unit=item.get("unit"),
                    rate=_dec(item.get("rate")),
                    hsn=item.get("hsn"),
                )
            )

    elif record.doc_type == "goods_receipt":
        for item in payload.get("line_items") or []:
            lines.append(
                VoucherLine(
                    stock_item=item.get("description"),
                    amount=ZERO,
                    is_debit=True,
                    description=item.get("description"),
                    quantity=_dec(item.get("quantity_received")),
                    unit=item.get("unit"),
                    hsn=item.get("hsn"),
                )
            )

    place_of_supply = _place_of_supply(payload)

    draft = VoucherDraft(
        voucher_type=voucher_type,
        voucher_date=_voucher_date(record),
        party_ledger=party_ledger,
        narration=narration_for(record, record_id, pack),
        place_of_supply=place_of_supply,
        lines=lines,
        amount_total=total or _money(sum((line.amount for line in lines if line.is_debit), ZERO)),
        gst_cgst=cgst,
        gst_sgst=sgst,
        gst_igst=igst,
        roundoff=round_off,
        idempotency_key=idempotency_key_for(
            record, tenant=tenant, voucher_type=voucher_type, business_key=business_key, pack=pack
        ),
        tally_company=tally_company,
        warnings=warnings,
        proposals=proposals,
    )
    return MappingResult(draft=draft, proposals=proposals, warnings=warnings)


def _default_type(record: ExtractedRecord) -> str:
    return {
        "supplier_invoice": "Purchase",
        "supplier_credit_note": "Debit Note",
        "purchase_order": "Purchase Order",
        "goods_receipt": "Receipt Note",
        "bank_statement": "Payment",
    }.get(record.doc_type, "Journal")


def _tax_ledgers(
    mapping: LedgerMapping | None, payload: dict[str, Any], proposals: list[dict[str, Any]]
) -> dict[str, str | None]:
    ledgers = {
        "cgst": mapping.cgst_ledger if mapping else None,
        "sgst": mapping.sgst_ledger if mapping else None,
        "igst": mapping.igst_ledger if mapping else None,
    }
    # Tax ledgers are mapped once during onboarding, per tenant (Appendix C).
    for key, label in (("cgst", "Input CGST"), ("sgst", "Input SGST"), ("igst", "Input IGST")):
        if _dec(payload.get(key)) and not ledgers[key]:
            proposals.append(
                {
                    "kind": "ledger",
                    "proposed_name": label,
                    "parent_group": "Duties & Taxes",
                    "reason": f"{label} ledger is not mapped for this tenant yet.",
                }
            )
    return ledgers


def _purchase_lines(
    payload: dict[str, Any],
    condition: str,
    mapping: LedgerMapping | None,
    tax_ledgers: dict[str, str | None],
    party_ledger: str | None,
    proposals: list[dict[str, Any]],
    warnings: list[str],
) -> list[VoucherLine]:
    lines: list[VoucherLine] = []
    items = payload.get("line_items") or []

    if condition == "has_stock_items" and items:
        for item in items:
            stock_item = (mapping.default_stock_item if mapping else None) or item.get("description")
            lines.append(
                VoucherLine(
                    stock_item=stock_item,
                    ledger=(mapping.purchase_ledger if mapping else None),
                    amount=_money(_dec(item.get("amount"))),
                    is_debit=True,
                    description=item.get("description"),
                    quantity=_dec(item.get("quantity")),
                    unit=item.get("unit"),
                    rate=_dec(item.get("rate")),
                    gst_rate=_dec(item.get("gst_rate")),
                    hsn=item.get("hsn"),
                )
            )
        return lines

    expense_ledger = (mapping.expense_ledger if mapping else None) or (
        mapping.purchase_ledger if mapping else None
    )
    if not expense_ledger:
        expense_ledger = payload.get("expense_ledger") or _guess_expense_ledger(payload)
        warnings.append(
            "No expense or purchase ledger is mapped for this vendor; the purchase ledger is "
            "a proposal until a reviewer confirms it."
        )
        proposals.append(
            {
                "kind": "ledger",
                "proposed_name": expense_ledger,
                "parent_group": "Purchase Accounts",
                "reason": "Expense ledger suggested from the document, awaiting approval.",
            }
        )
    lines.append(
        VoucherLine(
            ledger=expense_ledger,
            amount=_money(_dec(payload.get("taxable_value"))),
            is_debit=True,
            description=payload.get("narration") or "Services or expenses",
        )
    )
    return lines


def _guess_expense_ledger(payload: dict[str, Any]) -> str:
    text = " ".join(
        str(item.get("description") or "") for item in (payload.get("line_items") or [])
    ).lower()
    rules = [
        (("freight", "transport", "lorry"), "Freight Inward"),
        (("repair", "maintenance"), "Repairs & Maintenance"),
        (("consult", "professional", "design", "drafting"), "Professional Fees"),
        (("electric", "power"), "Power & Fuel"),
        (("rent",), "Rent"),
        (("software", "licence", "license", "subscription"), "Software Subscription"),
    ]
    for needles, ledger in rules:
        if any(needle in text for needle in needles):
            return ledger
    return "Purchase Accounts"


def _counter_ledger(
    category: str, payload: dict[str, Any], mapping: LedgerMapping | None
) -> str | None:
    mapping_by_category = {
        "bank_charges": "Bank Charges",
        "interest": "Interest",
        "salary": "Salary",
        "gst_payment": "GST Payable",
        "tds_payment": "TDS Payable",
        "cheque_return": "Bank Charges",
    }
    if category in mapping_by_category:
        return mapping_by_category[category]
    if payload.get("suggested_ledger"):
        return payload["suggested_ledger"]
    if mapping and mapping.target_party_ledger:
        return mapping.target_party_ledger
    return None


def _place_of_supply(payload: dict[str, Any]) -> str | None:
    code = state_code_of_place_of_supply(payload.get("place_of_supply")) or state_code_of_gstin(
        payload.get("buyer_gstin")
    ) or state_code_of_gstin(payload.get("vendor_gstin"))
    return code


def _voucher_date(record: ExtractedRecord):
    for key in ("invoice_date", "po_date", "challan_date", "generation_date", "date"):
        value = record.payload.get(key)
        if value:
            return value
    return None
