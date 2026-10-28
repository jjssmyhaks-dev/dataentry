"""Built-in rule checks.

The rules themselves are declarative (``validation_rules.json``): the pack says
which check runs, against which document types, at which severity, with which
tolerances. This module implements the named checks. Adding a document type or
retuning a threshold is a pack change; only a genuinely new kind of check needs
an engine release.

Every check returns ``pass``, ``warn``, ``fail`` or ``skipped`` and a plain
language message, because the review queue shows the reviewer exactly why a
record was flagged (Appendix D).
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

from engine.models import ExtractedRecord, RuleOutcome
from engine.packs.models import RuleDefinition
from engine.validation import gstn as gstn_mod
from engine.validation.lookups import LookupBundle

ZERO = Decimal("0")
TWO_PLACES = Decimal("0.01")


# ── helpers ─────────────────────────────────────────────────────────────────


def as_decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    text = str(value).strip()
    if not text:
        return None
    text = re.sub(r"[₹,\s]", "", text)
    text = re.sub(r"^\((.*)\)$", r"-\1", text)  # (1,234.00) means negative
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def as_date(value: Any) -> dt.date | None:
    if value is None or isinstance(value, dt.date):
        return value
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d", "%d.%m.%Y", "%d %b %Y", "%d %B %Y"):
        try:
            return dt.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def as_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def money(value: Decimal | None) -> Decimal:
    return (value or ZERO).quantize(TWO_PLACES)


def payload(record: ExtractedRecord) -> dict[str, Any]:
    return record.payload or {}


def field_value(record: ExtractedRecord, name: str) -> Any:
    data = payload(record)
    if name in data:
        return data[name]
    # Field values are also kept individually, which lets a corrected field win.
    for fv in record.fields:
        if fv.path == name and fv.value_text is not None:
            return fv.value_text
    return None


def line_items(record: ExtractedRecord, key: str | None = "line_items") -> list[dict[str, Any]]:
    if not key:
        return []
    value = payload(record).get(key)
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def tax_total(record: ExtractedRecord) -> Decimal:
    return money(
        (as_decimal(field_value(record, "cgst")) or ZERO)
        + (as_decimal(field_value(record, "sgst")) or ZERO)
        + (as_decimal(field_value(record, "igst")) or ZERO)
    )


def present_gstins(record: ExtractedRecord, names: list[str]) -> dict[str, str]:
    found: dict[str, str] = {}
    for name in names:
        value = gstn_mod.normalise_gstin(field_value(record, name))
        if value:
            found[name] = value
    return found


@dataclass
class CheckInput:
    record: ExtractedRecord
    rule: RuleDefinition
    settings: dict[str, Any]
    lookups: LookupBundle
    pack: Any
    today: dt.date = field(default_factory=dt.date.today)


@dataclass
class CheckOutcome:
    outcome: RuleOutcome
    message: str | None = None
    details: dict[str, Any] = field(default_factory=dict)
    fields: list[str] = field(default_factory=list)
    degraded: bool = False

    def breach_message(self, rule: RuleDefinition, kind: str) -> str:
        fallback = {
            "fail": rule.breach.fail or f"{rule.title} failed.",
            "warn": rule.breach.warn or f"{rule.title} needs a look.",
        }
        return fallback[kind]


def _num(settings: dict[str, Any], key: str, default: str) -> Decimal:
    value = settings.get(key, default)
    return as_decimal(value) if value is not None else Decimal(default)


# ── GSTIN rules ─────────────────────────────────────────────────────────────


def check_gstin_format(inp: CheckInput) -> CheckOutcome:
    found = present_gstins(inp.record, inp.rule.fields)
    if not found:
        return CheckOutcome("skipped", "No GSTIN on this document.", fields=inp.rule.fields)

    problems: list[str] = []
    for name, value in found.items():
        ok, reason = gstn_mod.validate_gstin(value, allow_urp=inp.settings.get("allow_urp", True))
        if not ok:
            problems.append(f"{name}: {reason}")

    if problems:
        return CheckOutcome(
            "fail",
            "GSTIN is not valid: " + " ".join(problems),
            details={"problems": problems, "gstins": found},
            fields=list(found),
        )
    return CheckOutcome("pass", "GSTIN format is valid.", details={"gstins": found})


def check_gstin_active_status(inp: CheckInput) -> CheckOutcome:
    gstins = present_gstins(inp.record, inp.rule.fields)
    if not gstins:
        return CheckOutcome("skipped", "No GSTIN to check.")

    cancelled: list[str] = []
    unverified: list[str] = []
    for name, value in gstins.items():
        status = inp.lookups.status_of(value)
        if status is None or status.degraded or status.state == "unknown":
            unverified.append(value)
            continue
        if status.is_cancelled:
            cancelled.append(f"{value} is {status.state}")

    if cancelled:
        return CheckOutcome(
            "fail",
            "GSTIN is cancelled or suspended at the GSTN: " + "; ".join(cancelled),
            details={"cancelled": cancelled},
            fields=list(gstins),
        )
    if unverified:
        return CheckOutcome(
            "warn",
            "GSTIN status could not be verified with the GSTN right now; "
            f"the record will be rechecked ({', '.join(unverified)}).",
            details={"unverified": unverified},
            fields=list(gstins),
            degraded=True,
        )
    return CheckOutcome("pass", "GSTIN is active at the GSTN.")


# ── tax rules ───────────────────────────────────────────────────────────────


def _supply_state(record: ExtractedRecord) -> tuple[str | None, str | None, str]:
    """(vendor_state, place_of_supply_state, source_of_place_of_supply)."""
    vendor_state = gstn_mod.state_code_of_gstin(field_value(record, "vendor_gstin"))
    pos_raw = field_value(record, "place_of_supply")
    pos = gstn_mod.state_code_of_place_of_supply(pos_raw)
    source = "place_of_supply"
    if pos is None:
        pos = gstn_mod.state_code_of_gstin(field_value(record, "buyer_gstin"))
        source = "buyer_gstin"
    if pos is None:
        source = "unknown"
    return vendor_state, pos, source


def check_tax_type_vs_place_of_supply(inp: CheckInput) -> CheckOutcome:
    cgst = as_decimal(field_value(inp.record, "cgst")) or ZERO
    sgst = as_decimal(field_value(inp.record, "sgst")) or ZERO
    igst = as_decimal(field_value(inp.record, "igst")) or ZERO
    tolerance = _num(inp.settings, "tolerance_rupees", "1.00")

    if igst > 0 and (cgst > 0 or sgst > 0):
        return CheckOutcome(
            "fail",
            "Both IGST and CGST/SGST are present; a supply is one or the other.",
            details={"cgst": str(cgst), "sgst": str(sgst), "igst": str(igst)},
            fields=["cgst", "sgst", "igst"],
        )
    if igst <= tolerance and cgst <= tolerance and sgst <= tolerance:
        return CheckOutcome("skipped", "No tax split on this document to check.")

    vendor_state, pos_state, source = _supply_state(inp.record)
    if vendor_state is None or pos_state is None:
        return CheckOutcome(
            "warn",
            "Place of supply could not be established from the document, so the tax "
            "split could not be confirmed.",
            details={"vendor_state": vendor_state, "place_of_supply": pos_state},
            fields=["vendor_gstin", "place_of_supply", "buyer_gstin"],
            degraded=True,
        )

    intra_state = vendor_state == pos_state
    details = {
        "vendor_state": vendor_state,
        "place_of_supply_state": pos_state,
        "place_of_supply_source": source,
        "intra_state": intra_state,
    }

    if intra_state and igst > tolerance:
        return CheckOutcome(
            "fail",
            f"IGST is charged but the supply is intra-state "
            f"({gstn_mod.state_name(vendor_state)} to {gstn_mod.state_name(pos_state)}); "
            "CGST and SGST apply instead.",
            details=details,
            fields=["igst", "vendor_gstin", "place_of_supply"],
        )
    if not intra_state and (cgst > tolerance or sgst > tolerance):
        return CheckOutcome(
            "fail",
            f"CGST and SGST are charged but the supply is inter-state "
            f"({gstn_mod.state_name(vendor_state)} to {gstn_mod.state_name(pos_state)}); "
            "IGST applies instead.",
            details=details,
            fields=["cgst", "sgst", "vendor_gstin", "place_of_supply"],
        )
    return CheckOutcome(
        "pass",
        "Tax split matches the place of supply.",
        details=details,
    )


def check_tax_arithmetic(inp: CheckInput) -> CheckOutcome:
    record = inp.record
    doc_key = getattr(inp.pack.document(record.doc_type), "line_item_key", "line_items")
    items = line_items(record, doc_key)
    taxable = as_decimal(field_value(record, "taxable_value"))
    tolerance = _num(inp.settings, "document_tolerance_rupees", "1.00")
    warn_within = _num(inp.settings, "warn_within_rupees", "5.00")
    line_tolerance = _num(inp.settings, "per_line_tolerance_rupees", "1.00")

    details: dict[str, Any] = {}
    problems: list[str] = []

    # 1. Lines add up to the taxable value.
    if items and taxable is not None:
        line_sum = money(sum((as_decimal(i.get("amount")) or ZERO) for i in items))
        line_discount = money(sum((as_decimal(i.get("discount")) or ZERO) for i in items))
        expected_taxable = money(line_sum - line_discount)
        diff = money(taxable - expected_taxable)
        details["line_sum"] = str(line_sum)
        details["taxable_value"] = str(taxable)
        details["taxable_difference"] = str(diff)
        if abs(diff) > warn_within:
            problems.append(
                f"taxable value {taxable} does not match the line total {expected_taxable} "
                f"(difference {diff})"
            )
        elif abs(diff) > tolerance:
            return CheckOutcome(
                "warn",
                f"Taxable value is off the line total by {diff}, which is within rounding.",
                details=details,
                fields=["taxable_value", doc_key],
            )

    # 2. Tax at the stated rate matches the tax charged.
    rates = [
        (i.get("gst_rate"), as_decimal(i.get("amount")), i.get("description"))
        for i in items
        if as_decimal(i.get("gst_rate")) is not None and as_decimal(i.get("amount")) is not None
    ]
    if rates and taxable is not None:
        expected_tax = money(
            sum(
                ((amount or ZERO) * (as_decimal(rate) or ZERO) / Decimal(100))
                for rate, amount, _ in rates
            )
        )
        charged = tax_total(record)
        diff = money(charged - expected_tax)
        details["expected_tax"] = str(expected_tax)
        details["charged_tax"] = str(charged)
        details["tax_difference"] = str(diff)
        if abs(diff) > warn_within:
            return CheckOutcome(
                "fail",
                f"Tax charged is {charged} but the stated rates give {expected_tax} "
                f"(difference {diff}).",
                details=details,
                fields=["cgst", "sgst", "igst", doc_key],
            )
        if abs(diff) > tolerance:
            return CheckOutcome(
                "warn",
                f"Tax charged differs from the stated rates by {diff}, within rounding.",
                details=details,
                fields=["cgst", "sgst", "igst", doc_key],
            )
    elif items and any(as_decimal(i.get("gst_rate")) is None for i in items):
        details["note"] = "some lines have no GST rate, so the rate check was partial"

    if problems:
        return CheckOutcome("fail", problems[0].capitalize() + ".", details=details, fields=["taxable_value"])
    if details and "note" in details:
        return CheckOutcome(
            "fail" if problems else "warn",
            "Tax arithmetic could not be fully confirmed.",
            details=details,
            fields=["taxable_value"],
        )
    return CheckOutcome("pass", "Tax arithmetic is consistent.", details=details)


# What a document calls its grand total. The pack decides this per document type
# (a purchase order states an order value, not an invoice total), so the rule reads
# the same field the extractor filled.
TOTAL_FIELDS = {
    "supplier_invoice": "total",
    "supplier_credit_note": "total",
    "purchase_order": "agreed_rate_total",
}


def _total_field(record: ExtractedRecord) -> str:
    return TOTAL_FIELDS.get(record.doc_type, "total")


def check_total_arithmetic(inp: CheckInput) -> CheckOutcome:
    record = inp.record
    doc_type = record.doc_type
    total_field = _total_field(record)
    noun = {
        "purchase_order": "Order value",
        "supplier_credit_note": "Credit note total",
    }.get(doc_type, "Invoice total")

    taxable = as_decimal(field_value(record, "taxable_value"))
    total = as_decimal(field_value(record, total_field))
    if total is None:
        # Orders carry no tax split, so the lines themselves are the evidence.
        lines_only = sum(
            (as_decimal(item.get("amount")) or ZERO)
            for item in (record.payload.get("line_items") or [])
            if isinstance(item, dict)
        )
        if doc_type == "purchase_order" and lines_only > ZERO:
            total = money(lines_only)
        else:
            return CheckOutcome("fail", f"{noun} is missing.", fields=[total_field])

    cess = as_decimal(field_value(record, "cess")) or ZERO
    round_off = as_decimal(field_value(record, "round_off")) or ZERO
    taxes = tax_total(record) if doc_type != "purchase_order" else ZERO
    if doc_type == "purchase_order":
        # An order has no tax split: its value is the sum of its lines.
        line_total = sum(
            (as_decimal(item.get("amount")) or ZERO)
            for item in (record.payload.get("line_items") or [])
            if isinstance(item, dict)
        )
        expected = money(line_total if line_total > ZERO else (taxable or ZERO))
    else:
        expected = money((taxable or ZERO) + taxes + cess)
    diff = money(total - expected)

    tolerance = _num(inp.settings, "tolerance_rupees", "1.00")
    warn_within = _num(inp.settings, "warn_within_rupees", "3.00")
    details = {
        "total_field": total_field,
        "taxable_value": str(taxable) if taxable is not None else None,
        "taxes": str(taxes),
        "cess": str(cess),
        "round_off_on_document": str(round_off),
        "expected_total": str(expected),
        "stated_total": str(total),
        "difference": str(diff),
    }

    if abs(diff) <= tolerance:
        return CheckOutcome("pass", f"{noun} agrees with its parts.", details=details)

    # A round-off line is meant to bridge exactly this kind of difference.
    if abs(round_off) > ZERO and abs(money(diff - round_off)) <= tolerance:
        return CheckOutcome(
            "pass",
            f"{noun} is met by the round-off line of {round_off}.",
            details=details,
        )
    if abs(diff) <= warn_within:
        return CheckOutcome(
            "warn",
            f"{noun} differs from the sum of its parts by {diff}, within rounding.",
            details=details,
            fields=[total_field, "taxable_value"],
        )
    return CheckOutcome(
        "fail",
        f"{noun} differs from line total plus tax by {diff}.",
        details=details,
        fields=[total_field, "taxable_value"],
    )


def check_hsn_rate_check(inp: CheckInput) -> CheckOutcome:
    record = inp.record
    doc_key = getattr(inp.pack.document(record.doc_type), "line_item_key", "line_items")
    items = line_items(record, doc_key)
    if not items:
        return CheckOutcome("skipped", "No line items to check.")

    skip_missing_rate = inp.settings.get("skip_when_rate_missing", True)
    skip_missing_hsn = inp.settings.get("skip_when_hsn_missing", True)
    mismatches: list[str] = []
    unknown: list[str] = []
    checked = 0

    for index, item in enumerate(items):
        hsn = as_text(item.get("hsn"))
        rate = as_decimal(item.get("gst_rate"))
        label = as_text(item.get("description")) or f"line {index + 1}"
        if hsn is None:
            if not skip_missing_hsn:
                mismatches.append(f"{label}: no HSN")
            continue
        reference = inp.pack.hsn_rate(hsn)
        if reference is None:
            unknown.append(f"{label}: HSN {hsn} is not in the pack's rate table")
            continue
        if rate is None:
            if not skip_missing_rate:
                mismatches.append(f"{label}: no GST rate captured")
            continue
        checked += 1
        expected = as_decimal(reference.get("gst_rate"))
        if expected is not None and rate != expected:
            mismatches.append(
                f"{label}: HSN {hsn} is rated {expected}% but the invoice shows {rate}%"
            )

    details = {"mismatches": mismatches, "unknown_hsn": unknown, "lines_checked": checked}
    if mismatches:
        return CheckOutcome("warn", mismatches[0] + ".", details=details, fields=[doc_key])
    if unknown:
        return CheckOutcome(
            "warn",
            f"{unknown[0]}. The rate could not be confirmed against the pack's table.",
            details=details,
            fields=[doc_key],
        )
    return CheckOutcome("pass", "HSN codes and rates match the pack's table.", details=details)


# ── duplicates and matching ─────────────────────────────────────────────────


def check_duplicate_invoice(inp: CheckInput) -> CheckOutcome:
    finding = inp.lookups.duplicate
    if finding is None:
        return CheckOutcome("skipped", "Duplicate check was not run.", degraded=True)
    if finding.exact:
        return CheckOutcome(
            "fail",
            "The same vendor and invoice number has already been recorded"
            + (f" on {finding.matched_date:%d %b %Y}" if finding.matched_date else "")
            + ".",
            details={
                "matched_record_id": str(finding.matched_record_id) if finding.matched_record_id else None,
                "matched_number": finding.matched_document_number,
                "method": finding.method,
            },
            fields=["vendor_gstin", "invoice_number"],
        )
    threshold = float(inp.settings.get("similarity_threshold", 0.93))
    if finding.near and finding.similarity >= threshold:
        return CheckOutcome(
            "warn",
            f"A near-identical document was recorded earlier "
            f"(similarity {finding.similarity:.2f}); check before posting.",
            details={
                "similarity": finding.similarity,
                "matched_record_id": str(finding.matched_record_id) if finding.matched_record_id else None,
                "method": finding.method,
            },
            fields=["total", "invoice_number"],
        )
    return CheckOutcome("pass", "No duplicate found against recent documents.")


def check_three_way_match(inp: CheckInput) -> CheckOutcome:
    finding = inp.lookups.three_way
    if finding is None:
        return CheckOutcome("skipped", "Three-way match was not run.")

    has_reference = bool(
        as_text(field_value(inp.record, "po_reference"))
        or as_text(field_value(inp.record, "challan_reference"))
    )
    if not finding.checked:
        if not has_reference:
            # No PO on the invoice and none in the tenant's data: nothing to match
            # against. This is a warn, so the reviewer can see it was not checked.
            return CheckOutcome(
                "warn",
                "No purchase order or goods receipt was found for this invoice, so "
                "quantity and rate were not matched.",
                details={"po_number": finding.po_number},
                fields=["po_reference"],
            )
        return CheckOutcome(
            "warn",
            f"Purchase order {finding.po_number or ''} is referenced but was not found "
            "in the tenant's records.",
            details={"po_number": finding.po_number},
            fields=["po_reference"],
        )

    warn_at = _num(inp.settings, "quantity_variance_warn_pct", "2.0")
    fail_at = _num(inp.settings, "quantity_variance_fail_pct", "10.0")
    rate_warn = _num(inp.settings, "rate_variance_warn_pct", "1.0")
    rate_fail = _num(inp.settings, "rate_variance_fail_pct", "5.0")

    details = {
        "po_number": finding.po_number,
        "po_found": finding.po_found,
        "goods_receipt_found": finding.goods_receipt_found,
        "quantity_variance_pct": str(finding.quantity_variance_pct),
        "rate_variance_pct": str(finding.rate_variance_pct),
        "unmatched_lines": finding.unmatched_lines,
        "notes": finding.notes,
    }

    qty = finding.quantity_variance_pct
    rate = finding.rate_variance_pct
    if qty is not None and abs(qty) > fail_at:
        return CheckOutcome(
            "fail",
            f"Invoice quantity differs from the purchase order or goods receipt by {qty:.2f}%, "
            f"beyond the allowed {fail_at}%.",
            details=details,
            fields=["line_items", "po_reference"],
        )
    if rate is not None and abs(rate) > rate_fail:
        return CheckOutcome(
            "fail",
            f"Invoice rate differs from the purchase order by {rate:.2f}%, beyond the "
            f"allowed {rate_fail}%.",
            details=details,
            fields=["line_items", "po_reference"],
        )
    if (qty is not None and abs(qty) > warn_at) or (rate is not None and abs(rate) > rate_warn):
        return CheckOutcome(
            "warn",
            f"Invoice does not agree with the purchase order or goods receipt within "
            f"{warn_at}% on quantity or {rate_warn}% on rate.",
            details=details,
            fields=["line_items", "po_reference"],
        )
    if finding.unmatched_lines:
        return CheckOutcome(
            "warn",
            "Some invoice lines could not be matched to the purchase order.",
            details=details,
            fields=["line_items"],
        )
    return CheckOutcome("pass", "Invoice agrees with the purchase order and goods receipt.", details=details)


def check_gstr2b_match(inp: CheckInput) -> CheckOutcome:
    finding = inp.lookups.gstr2b
    if finding is None or not finding.found:
        degraded = True if finding is None or finding.degraded else False
        detail = (finding.detail if finding else None) or (
            "The invoice was not found in the vendor's filed return, so input credit "
            "is at risk. This is checked against the GSTR-2B data for the period."
        )
        return CheckOutcome("warn", detail, degraded=degraded, fields=["invoice_number"])
    tolerance = _num(inp.settings, "value_tolerance_rupees", "1.00")
    value_diff = finding.value_difference or ZERO
    tax_diff = finding.tax_difference or ZERO
    if abs(value_diff) > tolerance or abs(tax_diff) > tolerance:
        return CheckOutcome(
            "warn",
            f"Invoice appears in the filed return for {finding.period} but the value "
            f"differs by {value_diff} and tax by {tax_diff}.",
            details={"period": finding.period, "value_difference": str(value_diff), "tax_difference": str(tax_diff)},
            fields=["total", "taxable_value"],
        )
    return CheckOutcome("pass", f"Invoice matches the filed return for {finding.period}.")


# ── compliance rules ────────────────────────────────────────────────────────


def check_e_invoice_irn(inp: CheckInput) -> CheckOutcome:
    record = inp.record
    irn = as_text(field_value(record, "irn"))
    required_length = int(inp.settings.get("irn_length", 64))
    append = inp.settings.get("tenant_e_invoicing_applies", inp.lookups.e_invoicing_applies)

    if not irn:
        if append:
            return CheckOutcome(
                "fail",
                "E-invoicing applies to this vendor but no IRN is present on the document.",
                details={"e_invoicing_applies": True},
                fields=["irn"],
            )
        return CheckOutcome(
            "skipped",
            "E-invoicing does not apply to this vendor, so no IRN is expected.",
            details={"e_invoicing_applies": False},
        )

    cleaned = re.sub(r"[^0-9a-fA-F]", "", irn)
    if len(cleaned) != required_length:
        return CheckOutcome(
            "warn",
            f"IRN is present but is {len(cleaned)} characters; a valid IRN is "
            f"{required_length} hexadecimal characters.",
            details={"irn_length": len(cleaned)},
            fields=["irn"],
        )
    return CheckOutcome("pass", "IRN is present and well formed.")


def check_eway_bill_consistency(inp: CheckInput) -> CheckOutcome:
    record = inp.record
    number = as_text(field_value(record, "eway_bill_number"))
    finding = inp.lookups.eway_bill
    threshold = _num(inp.settings, "require_when_value_above", "50000")
    total = as_decimal(field_value(record, "total")) or ZERO

    if not number and not finding:
        if total > threshold:
            return CheckOutcome(
                "warn",
                f"No e-way bill is linked to this invoice, which is above "
                f"{threshold}; one is normally required for movement of goods.",
                details={"total": str(total), "threshold": str(threshold)},
                fields=["eway_bill_number"],
            )
        return CheckOutcome("skipped", "No e-way bill on this document.")

    if not finding:
        return CheckOutcome(
            "warn",
            f"E-way bill {number} is referenced on the invoice but its details were "
            "not available to compare.",
            degraded=True,
            fields=["eway_bill_number"],
        )

    tolerance = _num(inp.settings, "value_tolerance_rupees", "1.00")
    problems: list[str] = []
    if finding.value is not None and abs(money(finding.value - total)) > tolerance:
        problems.append(f"value: invoice {total} against e-way bill {finding.value}")
    if finding.supplier_gstin and gstn_mod.normalise_gstin(field_value(record, "vendor_gstin")) != finding.supplier_gstin:
        problems.append("supplier GSTIN differs")
    invoice_number = as_text(field_value(record, "invoice_number"))
    if finding.document_number and invoice_number and finding.document_number != invoice_number:
        problems.append(f"document number: invoice {invoice_number} against e-way bill {finding.document_number}")
    if finding.expired:
        problems.append(f"e-way bill expired on {finding.valid_upto}")

    if problems:
        return CheckOutcome(
            "warn",
            "E-way bill does not match the invoice (" + "; ".join(problems) + ").",
            details={"problems": problems},
            fields=["eway_bill_number", "total"],
        )
    return CheckOutcome("pass", "E-way bill matches the invoice.")


def check_tds_applicability(inp: CheckInput) -> CheckOutcome:
    record = inp.record
    section = as_text(field_value(record, "tds_section"))
    rate = as_decimal(field_value(record, "tds_rate"))
    taxable = as_decimal(field_value(record, "taxable_value")) or ZERO
    single = _num(inp.settings, "threshold_194c_single", "30000")

    if section and rate is not None:
        return CheckOutcome(
            "pass",
            f"TDS is captured at section {section} and rate {rate}%.",
            details={"section": section, "rate": str(rate)},
        )
    if section and rate is None:
        return CheckOutcome(
            "warn",
            f"TDS section {section} is captured but the rate is missing.",
            fields=["tds_rate", "tds_section"],
        )
    if taxable >= single:
        return CheckOutcome(
            "warn",
            f"Payment of {taxable} is at or above the TDS threshold single-payment limit "
            f"of {single}, but no TDS section is captured.",
            details={"taxable_value": str(taxable), "threshold": str(single)},
            fields=["tds_section", "taxable_value"],
        )
    return CheckOutcome(
        "pass",
        "No TDS applies at this value.",
        details={"taxable_value": str(taxable)},
    )


def check_date_sanity(inp: CheckInput) -> CheckOutcome:
    record = inp.record
    candidate: dt.date | None = None
    used_field: str | None = None
    for name in inp.rule.fields:
        value = as_date(field_value(record, name))
        if value is not None:
            candidate = value
            used_field = name
            break
    if candidate is None:
        return CheckOutcome("fail", "No usable document date was found.", fields=inp.rule.fields[:1])

    max_future_days = int(inp.settings.get("max_future_days", 2))
    max_age_days = int(inp.settings.get("max_age_days", 1095))
    today = inp.today

    if candidate > today + dt.timedelta(days=max_future_days):
        return CheckOutcome(
            "fail",
            f"Document date {candidate:%d %b %Y} is in the future.",
            details={"date": candidate.isoformat(), "field": used_field, "today": today.isoformat()},
            fields=[used_field] if used_field else [],
        )
    if (today - candidate).days > max_age_days:
        return CheckOutcome(
            "warn",
            f"Document date {candidate:%d %b %Y} is more than {max_age_days // 365} years old.",
            details={"date": candidate.isoformat(), "field": used_field},
            fields=[used_field] if used_field else [],
        )
    for start, end in inp.lookups.open_periods:
        if start <= candidate <= end:
            return CheckOutcome(
                "pass",
                f"Document date {candidate:%d %b %Y} falls in an open period.",
                details={"date": candidate.isoformat(), "field": used_field},
            )
    if inp.lookups.open_periods:
        return CheckOutcome(
            "fail",
            f"Document date {candidate:%d %b %Y} falls outside the open financial periods "
            "loaded from Tally.",
            details={
                "date": candidate.isoformat(),
                "field": used_field,
                "open_periods": [[s.isoformat(), e.isoformat()] for s, e in inp.lookups.open_periods],
            },
            fields=[used_field] if used_field else [],
        )
    return CheckOutcome(
        "pass",
        f"Document date {candidate:%d %b %Y} is within range.",
        details={"date": candidate.isoformat(), "field": used_field},
    )


# ── bank statement rules ────────────────────────────────────────────────────


def check_bank_running_balance(inp: CheckInput) -> CheckOutcome:
    debit = as_decimal(field_value(inp.record, "debit")) or ZERO
    credit = as_decimal(field_value(inp.record, "credit")) or ZERO
    balance = as_decimal(field_value(inp.record, "balance"))
    previous = inp.lookups.previous_balance
    if balance is None or previous is None:
        return CheckOutcome(
            "skipped",
            "There is no previous balance to compare against, so the running balance "
            "was not checked.",
            degraded=True,
        )
    tolerance = _num(inp.settings, "tolerance_rupees", "0.01")
    expected = money(previous + credit - debit)
    diff = money(balance - expected)
    details = {"previous_balance": str(previous), "expected": str(expected), "stated": str(balance), "difference": str(diff)}
    if abs(diff) > tolerance:
        return CheckOutcome(
            "warn",
            f"Balance {balance} does not agree with the previous balance plus this "
            f"movement, which gives {expected} (difference {diff}).",
            details=details,
            fields=["balance", "debit", "credit"],
        )
    return CheckOutcome("pass", "Running balance agrees with the previous line.", details=details)


def check_bank_line_categorisation(inp: CheckInput) -> CheckOutcome:
    narration = (as_text(field_value(inp.record, "narration")) or "").lower()
    keywords: dict[str, list[str]] = inp.settings.get("keywords", {}) or {}
    for category, needles in keywords.items():
        if any(needle.lower() in narration for needle in needles):
            return CheckOutcome(
                "pass",
                f"Line looks like {category.replace('_', ' ')}.",
                details={"category": category},
            )
    debit = as_decimal(field_value(inp.record, "debit")) or ZERO
    credit = as_decimal(field_value(inp.record, "credit")) or ZERO
    return CheckOutcome(
        "warn" if (debit or credit) else "skipped",
        "No confident ledger or open bill match for this line; a reviewer should "
        "pick the ledger.",
        details={"narration": narration},
        fields=["narration"],
    )


# ── registry ────────────────────────────────────────────────────────────────

CHECKS: dict[str, Callable[[CheckInput], CheckOutcome]] = {
    "gstin_format": check_gstin_format,
    "gstin_active_status": check_gstin_active_status,
    "tax_type_vs_place_of_supply": check_tax_type_vs_place_of_supply,
    "tax_arithmetic": check_tax_arithmetic,
    "total_arithmetic": check_total_arithmetic,
    "hsn_rate_check": check_hsn_rate_check,
    "duplicate_invoice": check_duplicate_invoice,
    "three_way_match": check_three_way_match,
    "gstr2b_match": check_gstr2b_match,
    "e_invoice_irn": check_e_invoice_irn,
    "eway_bill_consistency": check_eway_bill_consistency,
    "tds_applicability": check_tds_applicability,
    "date_sanity": check_date_sanity,
    "bank_running_balance": check_bank_running_balance,
    "bank_line_categorisation": check_bank_line_categorisation,
}


def get_check(name: str) -> Callable[[CheckInput], CheckOutcome] | None:
    return CHECKS.get(name)
