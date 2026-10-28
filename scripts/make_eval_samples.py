#!/usr/bin/env python
"""Generate the manufacturing pack's evaluation set and GSTN fixtures.

The pack ships with an evaluation set so every release can be measured (section 7
part 5) and the stage 1 gate can be checked (section 12a). These samples are
**synthetic**: they exercise the pipeline and the accuracy harness, and they are
not the design-partner documents the gate ultimately needs. Replace them with
real labelled documents (with the customer's consent) before treating any
accuracy number as meaningful.

Each sample is written twice:

* ``eval/documents/<id>.txt``   a text-layer document, as an OCR/text extractor
                                would hand it to the engine
* ``eval/documents/<id>.truth.json``  the labelled ground truth: expected field
                                values, expected rule outcomes and whether the
                                document must auto-post

Run with ``python scripts/make_eval_samples.py``.
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from engine.settings import get_settings  # noqa: E402
from engine.validation.gstn import STATE_CODES, gstin_checksum  # noqa: E402

OUT = get_settings().packs_dir / "manufacturing" / "eval" / "documents"


def gstin(prefix14: str) -> str:
    """A syntactically valid GSTIN with a correct checksum character."""
    return prefix14 + gstin_checksum(prefix14)


VENDOR_STEEL = ("SHREE STEEL TUBES PVT LTD", gstin("27AAPFU0939F1Z"), "27")
VENDOR_PLASTIC = ("GUJARAT POLYMER INDUSTRIES", gstin("24AABCS1429B1Z"), "24")
VENDOR_FASTENERS = ("KARNATAKA FASTENERS PVT LTD", gstin("29AABCK7642P1Z"), "29")
VENDOR_MACHINING = ("NAGPUR MACHINING WORKS", gstin("27AABCT1332L1Z"), "27")
VENDOR_CANCELLED = ("Vidarbha Castings Pvt Ltd", gstin("27AAACR5055K1Z"), "27")
VENDOR_SERVICES = ("NAGPUR ENGINEERING CONSULTANTS", gstin("27AAGCN2211L1Z"), "27")

BUYER = ("PRECISION PIPES LIMITED", gstin("27AABCP8899Q1Z"), "27")
BUYER_OUT_OF_STATE = ("PRECISION PIPES LIMITED - CHENNAI PLANT", gstin("33AABCP8899Q1Z"), "33")


def invoice_text(
    *,
    vendor: tuple[str, str, str],
    invoice_number: str,
    invoice_date: str,
    lines: list[tuple[str, str, str, str, str, str, str]],
    taxable: str,
    cgst: str,
    sgst: str,
    igst: str,
    round_off: str,
    total: str,
    buyer: tuple[str, str, str],
    place_of_supply: str,
    extra_header: str = "",
    extra_footer: str = "",
    permissions: str = "",
) -> str:
    """Render an invoice in the label/value layout a text-layer document gives us."""
    rows = "\n".join(
        f"{desc} | {hsn} | {qty} | {unit} | {rate} | {amount} | {gst_rate}"
        for desc, hsn, qty, unit, rate, amount, gst_rate in lines
    )
    header = f"""
TAX INVOICE
{vendor[0]}
Plot 14, MIDC Industrial Area, Nagpur 440016
GSTIN: {vendor[1]}
State: {STATE_CODES[vendor[2]]} ({vendor[2]})
{extra_header}
Invoice No: {invoice_number}
Invoice Date: {invoice_date}
Billed To: {buyer[0]}
Buyer GSTIN: {buyer[1]}
Place of Supply: {place_of_supply}
PO Reference: {permissions or "-"}

Description | HSN | Qty | Unit | Rate | Amount | GST%
{rows}

Taxable Value: {taxable}
CGST @ 9%: {cgst}
SGST @ 9%: {sgst}
IGST: {igst}
Round Off: {round_off}
Grand Total: {total}
{extra_footer}
Amount in words: Rupees as above
For {vendor[0]}
Authorised Signatory
"""
    return header.strip() + "\n"


def write_case(case_id: str, text: str, truth: dict) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"{case_id}.txt").write_text(text, encoding="utf-8")
    (OUT / f"{case_id}.truth.json").write_text(
        json.dumps(truth, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def truth(
    doc_type: str,
    fields: dict,
    *,
    rules: dict | None = None,
    autopost: bool,
    tags: list[str] | None = None,
    notes: str = "",
) -> dict:
    return {
        "doc_type": doc_type,
        "fields": fields,
        "expected_rules": rules or {},
        "must_auto_post": autopost,
        "tags": tags or [],
        "notes": notes,
    }


def main() -> int:
    today = dt.date.today()
    recent = (today - dt.timedelta(days=9)).strftime("%d/%m/%Y")
    recent_iso = (today - dt.timedelta(days=9)).isoformat()
    other_recent = (today - dt.timedelta(days=4)).strftime("%d/%m/%Y")
    other_recent_iso = (today - dt.timedelta(days=4)).isoformat()
    future = (today + dt.timedelta(days=25)).strftime("%d/%m/%Y")
    future_iso = (today + dt.timedelta(days=25)).isoformat()

    # -- inv-001: clean intra-state invoice, must auto-post -----------------
    write_case(
        "inv-001-intra-state-clean",
        invoice_text(
            vendor=VENDOR_STEEL,
            invoice_number="GST/2026-27/0142",
            invoice_date=recent,
            lines=[
                ("MS PIPE 50MM X 3MM", "7306", "1240", "KG", "62.50", "77500.00", "18"),
                ("MS PIPE 25MM X 2MM", "7306", "480", "KG", "58.00", "27840.00", "18"),
            ],
            taxable="105340.00",
            cgst="9480.60",
            sgst="9480.60",
            igst="0.00",
            round_off="0.00",
            total="124301.20",
            buyer=BUYER,
            place_of_supply="Maharashtra (27)",
            permissions="PO/2026/0912",
        ),
        truth(
            "supplier_invoice",
            {
                "vendor_name": VENDOR_STEEL[0],
                "vendor_gstin": VENDOR_STEEL[1],
                "invoice_number": "GST/2026-27/0142",
                "invoice_date": recent_iso,
                "taxable_value": "105340.00",
                "cgst": "9480.60",
                "sgst": "9480.60",
                "igst": "0.00",
                "round_off": "0.00",
                "total": "124301.20",
                "line_items": [
                    {"description": "MS PIPE 50MM X 3MM", "hsn": "7306", "quantity": "1240", "unit": "KG", "rate": "62.50", "amount": "77500.00", "gst_rate": "18"},
                    {"description": "MS PIPE 25MM X 2MM", "hsn": "7306", "quantity": "480", "unit": "KG", "rate": "58.00", "amount": "27840.00", "gst_rate": "18"},
                ],
            },
            rules={"gstin_format": "pass", "gstin_active_status": "pass",
                   "tax_type_vs_place_of_supply": "pass", "tax_arithmetic": "pass",
                   "total_arithmetic": "pass"},
            autopost=True,
            tags=["clean", "intra_state", "auto_post"],
        ),
    )

    # -- inv-002: clean inter-state invoice, IGST ---------------------------
    write_case(
        "inv-002-inter-state-clean",
        invoice_text(
            vendor=VENDOR_PLASTIC,
            invoice_number="GPI/2026/1187",
            invoice_date=other_recent,
            lines=[
                ("HDPE PIPE 110MM PN6", "3917", "860", "KG", "118.00", "101480.00", "18"),
                ("HDPE ELBOW 110MM", "3917", "240", "PCS", "96.00", "23040.00", "18"),
            ],
            taxable="124520.00",
            cgst="0.00",
            sgst="0.00",
            igst="22413.60",
            round_off="-0.60",
            total="146933.00",
            buyer=BUYER,
            place_of_supply="Maharashtra (27)",
        ),
        truth(
            "supplier_invoice",
            {
                "vendor_name": VENDOR_PLASTIC[0],
                "vendor_gstin": VENDOR_PLASTIC[1],
                "invoice_number": "GPI/2026/1187",
                "invoice_date": other_recent_iso,
                "taxable_value": "124520.00",
                "cgst": "0.00",
                "sgst": "0.00",
                "igst": "22413.60",
                "round_off": "-0.60",
                "total": "146933.00",
            },
            rules={"tax_type_vs_place_of_supply": "pass", "total_arithmetic": "pass"},
            autopost=True,
            tags=["clean", "inter_state", "auto_post"],
        ),
    )

    # -- inv-003: intra-state supply billed with IGST, must not auto-post ---
    write_case(
        "inv-003-tax-mismatch",
        invoice_text(
            vendor=VENDOR_FASTENERS,
            invoice_number="KFP/26/0781",
            invoice_date=other_recent,
            lines=[("HSFG BOLT M16 X 80", "7318", "3000", "NOS", "24.00", "72000.00", "18")],
            taxable="72000.00",
            cgst="0.00",
            sgst="0.00",
            igst="12960.00",
            round_off="0.00",
            total="84960.00",
            buyer=BUYER,
            place_of_supply="Karnataka (29)",
        ),
        truth(
            "supplier_invoice",
            {
                "vendor_gstin": VENDOR_FASTENERS[1],
                "invoice_number": "KFP/26/0781",
                "taxable_value": "72000.00",
                "igst": "12960.00",
                "total": "84960.00",
            },
            rules={"tax_type_vs_place_of_supply": "fail"},
            autopost=False,
            tags=["rule_fail", "tax_type_vs_place_of_supply", "review"],
            notes="Karnataka to Karnataka is intra-state, so CGST and SGST apply, not IGST.",
        ),
    )

    # -- inv-004: total does not agree with its parts -----------------------
    write_case(
        "inv-004-total-mismatch",
        invoice_text(
            vendor=VENDOR_MACHINING,
            invoice_number="TNM/2026/0455",
            invoice_date=other_recent,
            lines=[("CNC MACHINED SHAFT", "8481", "150", "NOS", "640.00", "96000.00", "18")],
            taxable="96000.00",
            cgst="8640.00",
            sgst="8640.00",
            igst="0.00",
            round_off="0.00",
            total="113292.00",
            buyer=BUYER,
            place_of_supply="Maharashtra (27)",
            ),
        truth(
            "supplier_invoice",
            {
                "vendor_gstin": VENDOR_MACHINING[1],
                "invoice_number": "TNM/2026/0455",
                "taxable_value": "96000.00",
                "total": "113292.00",
            },
            rules={"total_arithmetic": "fail", "tax_arithmetic": "pass"},
            autopost=False,
            tags=["rule_fail", "total_arithmetic", "review"],
            notes="Taxable plus tax is 113280.00; the stated total is 12 rupees higher.",
        ),
    )

    # -- inv-005: duplicate of inv-001 --------------------------------------
    duplicate_text = invoice_text(
        vendor=VENDOR_STEEL,
        invoice_number="GST/2026-27/0142",
        invoice_date=recent,
        lines=[
            ("MS PIPE 50MM X 3MM", "7306", "1240", "KG", "62.50", "77500.00", "18"),
            ("MS PIPE 25MM X 2MM", "7306", "480", "KG", "58.00", "27840.00", "18"),
        ],
        taxable="105340.00",
        cgst="9480.60",
        sgst="9480.60",
        igst="0.00",
        round_off="0.00",
        total="124301.20",
        buyer=BUYER,
        place_of_supply="Maharashtra (27)",
        permissions="PO/2026/0912",
    )
    write_case(
        "inv-005-duplicate",
        duplicate_text,
        truth(
            "supplier_invoice",
            {
                "vendor_gstin": VENDOR_STEEL[1],
                "invoice_number": "GST/2026-27/0142",
                "taxable_value": "105340.00",
                "total": "124301.20",
            },
            rules={"duplicate_invoice": "fail"},
            autopost=False,
            tags=["rule_fail", "duplicate_invoice", "review"],
            notes="Same vendor, same invoice number and same content as inv-001.",
        ),
    )

    # -- inv-006: invoice dated in the future -------------------------------
    write_case(
        "inv-006-future-date",
        invoice_text(
            vendor=VENDOR_SERVICES,
            invoice_number="NEC/2026/0099",
            invoice_date=future,
            lines=[("DESIGN AND DRAFTING SERVICES", "998313", "1", "JOB", "85000.00", "85000.00", "18")],
            taxable="85000.00",
            cgst="7650.00",
            sgst="7650.00",
            igst="0.00",
            round_off="0.00",
            total="100300.00",
            buyer=BUYER,
            place_of_supply="Maharashtra (27)",
            ),
        truth(
            "supplier_invoice",
            {
                "vendor_gstin": VENDOR_SERVICES[1],
                "invoice_number": "NEC/2026/0099",
                "invoice_date": future_iso,
                "taxable_value": "85000.00",
                "total": "100300.00",
            },
            rules={"date_sanity": "fail", "tds_applicability": "warn"},
            autopost=False,
            tags=["rule_fail", "date_sanity", "review"],
        ),
    )

    # -- inv-007: poor scan, several fields unreadable ----------------------
    placeholders = invoice_text(
        vendor=VENDOR_CANCELLED,
        invoice_number="VCP-2026-0417",
        invoice_date=other_recent,
        lines=[("CAST IRON FLANGE", "7307", "600", "NOS", "?", "47400.00", "18")],
        taxable="47400.00",
        cgst="4266.00",
        sgst="4266.00",
        igst="0.00",
        round_off="0.00",
        total="55932.00",
        buyer=BUYER,
        place_of_supply="Maharashtra (27)",
        extra_footer="(scan quality poor: rate column partly unreadable, signature over total)",
    )
    write_case(
        "inv-007-low-confidence-scan",
        placeholders.replace("GSTIN: " + VENDOR_CANCELLED[1], "GSTIN: " + VENDOR_CANCELLED[1].lower()),
        truth(
            "supplier_invoice",
            {
                "vendor_gstin": VENDOR_CANCELLED[1],
                "invoice_number": "VCP-2026-0417",
                "taxable_value": "47400.00",
                "total": "55932.00",
            },
            rules={"gstin_active_status": "fail"},
            autopost=False,
            tags=["low_confidence", "review", "cancelled_vendor"],
            notes="Vendor GSTIN is cancelled in the fixture, and one rate is unreadable.",
        ),
    )

    # -- po-001: purchase order for the three-way match ---------------------
    po_text = f"""
PURCHASE ORDER
{BUYER[0]}
GSTIN: {BUYER[1]}

PO Number: PO/2026/0912
PO Date: {recent}
Vendor: {VENDOR_STEEL[0]}
Vendor GSTIN: {VENDOR_STEEL[1]}
Delivery Date: {(today + dt.timedelta(days=6)).strftime('%d/%m/%Y')}
Payment Terms: 45 days from invoice date
Place of Supply: Maharashtra (27)

Description | HSN | Qty | Unit | Rate | Amount
MS PIPE 50MM X 3MM | 7306 | 1240 | KG | 62.50 | 77500.00
MS PIPE 25MM X 2MM | 7306 | 480 | KG | 58.00 | 27840.00

Order Value: 105340.00
""".strip()
    write_case(
        "po-001-clean",
        po_text,
        truth(
            "purchase_order",
            {
                "po_number": "PO/2026/0912",
                "vendor_name": VENDOR_STEEL[0],
                "vendor_gstin": VENDOR_STEEL[1],
                "agreed_rate_total": "105340.00",
            },
            rules={},
            autopost=False,
            tags=["clean"],
            notes="Used for the three-way match against inv-001.",
        ),
    )

    # -- grn-001: goods receipt for the three-way match ---------------------
    grn_text = f"""
DELIVERY CHALLAN
{VENDOR_STEEL[0]}
GSTIN: {VENDOR_STEEL[1]}

Challan Number: DC/2026/5510
Challan Date: {other_recent}
PO Reference: PO/2026/0912
Vehicle Number: MH40 AB 1234
Place of Supply: Maharashtra (27)

Description | HSN | Qty | Unit
MS PIPE 50MM X 3MM | 7306 | 1236 | KG
MS PIPE 25MM X 2MM | 7306 | 480 | KG

Total Quantity Received: 1716
Received By: Stores Department
""".strip()
    write_case(
        "grn-001-clean",
        grn_text,
        truth(
            "goods_receipt",
            {
                "challan_number": "DC/2026/5510",
                "po_reference": "PO/2026/0912",
                "vehicle_number": "MH40 AB 1234",
                "quantity_received": "1716",
            },
            rules={},
            autopost=False,
            tags=["clean"],
            notes=(
                "Received 1236 KG against 1240 KG ordered: a 0.32% quantity variance, "
                "which the three-way match raises as a warn on the invoice."
            ),
        ),
    )

    # -- ewb-001: e-way bill consistent with inv-001 ------------------------
    ewb_text = f"""
GOVERNMENT OF INDIA
E-WAY BILL

E-Way Bill No: 391004812266
Generation Date: {recent}
Supplier GSTIN: {VENDOR_STEEL[1]}
Recipient GSTIN: {BUYER[1]}
Document Number: GST/2026-27/0142
Document Date: {recent}
HSN Code: 7306
Total Value: 124301.20
Vehicle Number: MH40 AB 1234
Valid Upto: {(today + dt.timedelta(days=3)).strftime('%d/%m/%Y')}
""".strip()
    write_case(
        "ewb-001-clean",
        ewb_text,
        truth(
            "e_way_bill",
            {
                "eway_bill_number": "391004812266",
                "supplier_gstin": VENDOR_STEEL[1],
                "recipient_gstin": BUYER[1],
                "document_number": "GST/2026-27/0142",
                "total_value": "124301.20",
            },
            rules={},
            autopost=False,
            tags=["clean"],
            notes="Consistency is asserted on the invoice, where both sides are available.",
        ),
    )

    # -- bank-001: statement lines, including a bank charge -----------------
    bank_text = f"""
STATEMENT OF ACCOUNT
ACCOUNT NO: 50100234567890
PRECISION PIPES LIMITED - HDFC BANK NAGPUR

Date | Narration | Ref No | Withdrawal | Deposit | Balance
{(today - dt.timedelta(days=12)).strftime('%d/%m/%Y')} | B/F | | | | 842311.50
{(today - dt.timedelta(days=11)).strftime('%d/%m/%Y')} | NEFT DR SHREE STEEL TUBES | N123456 | 124301.20 | | 718010.30
{(today - dt.timedelta(days=10)).strftime('%d/%m/%Y')} | RTGS CR CUSTOMER ADVANCE | R998811 | | 500000.00 | 1218010.30
{(today - dt.timedelta(days=9)).strftime('%d/%m/%Y')} | BANK CHARGES GST ON CHARGES | CHG0412 | 826.00 | | 1217184.30
{(today - dt.timedelta(days=8)).strftime('%d/%m/%Y')} | INTEREST CREDIT | INT0326 | | 3120.00 | 1220304.30
""".strip()
    write_case(
        "bank-001-clean",
        bank_text,
        truth(
            "bank_statement",
            {
                "account_reference": "50100234567890",
                "line_count": 4,
            },
            rules={"bank_line_categorisation": "pass"},
            autopost=False,
            tags=["clean", "bank"],
            notes="Four movement lines plus a brought-forward line that carries no movement.",
        ),
    )

    # -- GSTN fixtures for the prototype lookup stub (not production) -------
    fixtures = {
        "notes": (
            "Prototype fixture data for the GSTN stub. Appendix B requires the official "
            "GSTN source through an empanelled GSP in production; these entries only let "
            "the pipeline and its tests run without GSP credentials."
        ),
        "gstins": {
            VENDOR_STEEL[1]: {"status": "active", "legal_name": VENDOR_STEEL[0], "taxpayer_type": "Regular", "trade_name": "Shree Steel Tubes"},
            VENDOR_PLASTIC[1]: {"status": "active", "legal_name": VENDOR_PLASTIC[0], "taxpayer_type": "Regular"},
            VENDOR_FASTENERS[1]: {"status": "active", "legal_name": VENDOR_FASTENERS[0], "taxpayer_type": "Regular"},
            VENDOR_MACHINING[1]: {"status": "active", "legal_name": VENDOR_MACHINING[0], "taxpayer_type": "Regular"},
            VENDOR_CANCELLED[1]: {"status": "cancelled", "legal_name": VENDOR_CANCELLED[0], "taxpayer_type": "Regular", "detail": "Cancelled with effect from 12/09/2024"},
            VENDOR_SERVICES[1]: {"status": "active", "legal_name": VENDOR_SERVICES[0], "taxpayer_type": "Regular"},
            BUYER[1]: {"status": "active", "legal_name": BUYER[0], "taxpayer_type": "Regular"},
            BUYER_OUT_OF_STATE[1]: {"status": "active", "legal_name": BUYER_OUT_OF_STATE[0], "taxpayer_type": "Regular"},
        },
    }
    (OUT.parent / "gstn_fixtures.json").write_text(
        json.dumps(fixtures, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    print(f"wrote {len(list(OUT.glob('*.txt')))} sample documents to {OUT}")
    print(f"wrote GSTN fixtures to {OUT.parent / 'gstn_fixtures.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
