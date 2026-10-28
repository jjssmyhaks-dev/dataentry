"""Validation: GSTIN handling and the Appendix B rules."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from engine.extraction.baseline import extract_segment
from engine.extraction.classifier import split_documents
from engine.models import ExtractedRecord, FieldValue
from engine.validation import checks
from engine.validation.gstn import (
    GstinStatus,
    gstin_checksum,
    normalise_gstin,
    state_code_of_place_of_supply,
    validate_gstin,
)
from engine.validation.lookups import DuplicateFinding, EwayBillFinding, Gstr2bFinding, LookupBundle
from engine.validation.rules import RuleContext, RuleEngine


# ── GSTIN ───────────────────────────────────────────────────────────────────


def test_gstin_checksum_matches_known_valid_gstin():
    gstin = "27AAPFU0939F1ZV"
    assert gstin_checksum(gstin[:14]) == gstin[14]
    assert validate_gstin(gstin) == (True, None)


@pytest.mark.parametrize(
    "value,expected_fragment",
    [
        ("27AAPFU0939F1Z", "15 characters"),
        ("00AAPFU0939F1ZV", "state code"),
        ("27AAPFU0939F1ZA", "checksum"),
        ("", "missing"),
        (None, "missing"),
    ],
)
def test_invalid_gstins_are_rejected_with_a_plain_reason(value, expected_fragment):
    ok, reason = validate_gstin(value)
    assert not ok
    assert expected_fragment in (reason or "").lower()


def test_gstin_is_uppercased_and_trimmed():
    assert normalise_gstin(" 27aapfu0939f1zv ") == "27AAPFU0939F1ZV"
    assert validate_gstin(" 27aapfu0939f1zv ")[0]


@pytest.mark.parametrize(
    "value,code",
    [
        ("27", "27"),
        ("Maharashtra", "27"),
        ("Maharashtra (27)", "27"),
        ("27-Maharashtra", "27"),
        ("Karnataka", "29"),
        ("Orissa", "21"),
        ("Atlantis", None),
        (None, None),
    ],
)
def test_place_of_supply_resolves_to_a_state_code(value, code):
    assert state_code_of_place_of_supply(value) == code


# ── rule checks, one at a time ──────────────────────────────────────────────


def _record(payload: dict, doc_type: str = "supplier_invoice") -> ExtractedRecord:
    return ExtractedRecord(
        doc_type=doc_type,
        pack_key="manufacturing",
        pack_version="1.0.0",
        payload=payload,
        fields=[],
    )


class _Input:
    """A CheckInput without needing a pack on disk."""

    def __init__(self, record, rule, settings=None, lookups=None, pack=None):
        self.record = record
        self.rule = rule
        self.settings = settings or {}
        self.lookups = lookups or LookupBundle()
        self.pack = pack
        self.today = dt.date(2026, 10, 26)


def _rule(pack, key):
    return pack.rules_by_key[key]


def test_gstin_format_rule_passes_and_fails(pack):
    rule = _rule(pack, "gstin_format")
    good = checks.check_gstin_format(_Input(_record({"vendor_gstin": "27AAPFU0939F1ZV"}), rule, pack=pack))
    assert good.outcome == "pass"
    bad = checks.check_gstin_format(_Input(_record({"vendor_gstin": "27AAPFU0939F1ZX"}), rule, pack=pack))
    assert bad.outcome == "fail"
    assert "checksum" in bad.message.lower()


def test_cancelled_gstin_fails_and_unavailable_degrades_to_warn(pack):
    rule = _rule(pack, "gstin_active_status")
    gstin = "27AAACR5055K1Z7"
    cancelled = LookupBundle(
        gstin_status={gstin: GstinStatus(gstin=gstin, state="cancelled", source="fixture")}
    )
    result = checks.check_gstin_active_status(
        _Input(_record({"vendor_gstin": gstin}), rule, lookups=cancelled, pack=pack)
    )
    assert result.outcome == "fail"

    degraded = LookupBundle(
        gstin_status={gstin: GstinStatus(gstin=gstin, state="unknown", degraded=True)}
    )
    result = checks.check_gstin_active_status(
        _Input(_record({"vendor_gstin": gstin}), rule, lookups=degraded, pack=pack)
    )
    assert result.outcome == "warn"
    assert result.degraded


def test_tax_type_rule_catches_both_the_mismatches(pack):
    rule = _rule(pack, "tax_type_vs_place_of_supply")

    intra_state_with_igst = _record(
        {
            "vendor_gstin": "27AAPFU0939F1ZV",
            "place_of_supply": "Maharashtra (27)",
            "igst": Decimal("1800.00"),
        }
    )
    result = checks.check_tax_type_vs_place_of_supply(_Input(intra_state_with_igst, rule, pack=pack))
    assert result.outcome == "fail"
    assert "IGST" in result.message

    inter_state_with_cgst = _record(
        {
            "vendor_gstin": "24AABCS1429B1Z0",
            "place_of_supply": "Maharashtra (27)",
            "cgst": Decimal("900.00"),
            "sgst": Decimal("900.00"),
        }
    )
    result = checks.check_tax_type_vs_place_of_supply(_Input(inter_state_with_cgst, rule, pack=pack))
    assert result.outcome == "fail"
    assert "CGST" in result.message


def test_unknown_place_of_supply_degrades_instead_of_failing(pack):
    rule = _rule(pack, "tax_type_vs_place_of_supply", )
    record = _record({"vendor_gstin": "27AAPFU0939F1ZV", "cgst": Decimal("900.00")})
    result = checks.check_tax_type_vs_place_of_supply(_Input(record, rule, pack=pack))
    assert result.outcome == "warn"


def test_total_arithmetic_accepts_a_round_off_bridge(pack):
    rule = _rule(pack, "total_arithmetic")
    record = _record(
        {
            "taxable_value": Decimal("100.00"),
            "igst": Decimal("18.00"),
            "round_off": Decimal("-0.50"),
            "total": Decimal("117.50"),
        }
    )
    assert checks.check_total_arithmetic(_Input(record, rule, pack=pack)).outcome == "pass"


def test_total_arithmetic_fails_on_a_real_discrepancy(pack):
    rule = _rule(pack, "total_arithmetic")
    record = _record(
        {
            "taxable_value": Decimal("96000.00"),
            "cgst": Decimal("8640.00"),
            "sgst": Decimal("8640.00"),
            "total": Decimal("113292.00"),
        }
    )
    result = checks.check_total_arithmetic(_Input(record, rule, pack=pack))
    assert result.outcome == "fail"
    assert "12" in result.message


def test_duplicate_rule_separates_exact_from_near(pack):
    rule = _rule(pack, "duplicate_invoice")
    record = _record({"vendor_gstin": "27AAPFU0939F1ZV", "invoice_number": "X/1"})
    exact = LookupBundle(duplicate=DuplicateFinding(exact=True, similarity=1.0, method="key_fields"))
    assert checks.check_duplicate_invoice(_Input(record, rule, lookups=exact, pack=pack)).outcome == "fail"
    near = LookupBundle(duplicate=DuplicateFinding(exact=False, similarity=0.95, method="pgvector"))
    assert checks.check_duplicate_invoice(_Input(record, rule, lookups=near, pack=pack)).outcome == "warn"
    clean = LookupBundle(duplicate=DuplicateFinding(exact=False, similarity=0.1, method="none"))
    assert checks.check_duplicate_invoice(_Input(record, rule, lookups=clean, pack=pack)).outcome == "pass"


def test_three_way_match_severity_follows_the_variance(pack):
    rule = _rule(pack, "three_way_match")
    record = _record({"po_reference": "PO/1", "line_items": [{"amount": "100.00"}]})

    small = LookupBundle(
        three_way=__import__("engine.validation.lookups", fromlist=["ThreeWayFinding"]).ThreeWayFinding(
            po_number="PO/1", po_found=True, quantity_variance_pct=Decimal("0.32")
        )
    )
    assert checks.check_three_way_match(_Input(record, rule, lookups=small, pack=pack)).outcome == "pass"

    medium = LookupBundle(
        three_way=__import__("engine.validation.lookups", fromlist=["ThreeWayFinding"]).ThreeWayFinding(
            po_number="PO/1", po_found=True, quantity_variance_pct=Decimal("5.00")
        )
    )
    assert checks.check_three_way_match(_Input(record, rule, lookups=medium, pack=pack)).outcome == "warn"

    large = LookupBundle(
        three_way=__import__("engine.validation.lookups", fromlist=["ThreeWayFinding"]).ThreeWayFinding(
            po_number="PO/1", po_found=True, quantity_variance_pct=Decimal("15.00")
        )
    )
    assert checks.check_three_way_match(_Input(record, rule, lookups=large, pack=pack)).outcome == "fail"


def test_e_invoice_irn_only_fails_when_e_invoicing_applies(pack):
    rule = _rule(pack, "e_invoice_irn")
    without_irn = _record({"vendor_gstin": "27AAPFU0939F1ZV", "invoice_number": "X/1"})
    quiet = LookupBundle(e_invoicing_applies=False)
    assert checks.check_e_invoice_irn(_Input(without_irn, rule, lookups=quiet, pack=pack)).outcome == "skipped"
    applicable = LookupBundle(e_invoicing_applies=True)
    assert checks.check_e_invoice_irn(_Input(without_irn, rule, lookups=applicable, pack=pack)).outcome == "fail"

    with_irn = _record({"irn": "a" * 64})
    assert checks.check_e_invoice_irn(_Input(with_irn, rule, lookups=applicable, pack=pack)).outcome == "pass"

    short_irn = _record({"irn": "abc123"})
    assert checks.check_e_invoice_irn(_Input(short_irn, rule, lookups=applicable, pack=pack)).outcome == "warn"


def test_date_sanity_fails_on_a_future_invoice(pack):
    rule = _rule(pack, "date_sanity")
    future = _record({"invoice_date": dt.date(2026, 12, 1)})
    result = checks.check_date_sanity(_Input(future, rule, pack=pack))
    assert result.outcome == "fail"
    assert "future" in result.message

    today = _record({"invoice_date": dt.date(2026, 10, 26)})
    assert checks.check_date_sanity(_Input(today, rule, pack=pack)).outcome == "pass"


def test_date_sanity_respects_open_periods(pack):
    rule = _rule(pack, "date_sanity")
    record = _record({"invoice_date": dt.date(2024, 5, 1)})
    closed = LookupBundle(open_periods=[(dt.date(2026, 4, 1), dt.date(2027, 3, 31))])
    result = checks.check_date_sanity(
        type("I", (), {"record": record, "rule": rule, "settings": {"max_age_days": 5000},
                       "lookups": closed, "pack": pack, "today": dt.date(2026, 10, 26)})()
    )
    assert result.outcome == "fail"
    assert "open financial periods" in result.message


def test_hsn_rate_check_flags_a_wrong_rate_and_an_unknown_code(pack):
    rule = _rule(pack, "hsn_rate_check")
    wrong = _record({"line_items": [{"description": "MS PIPE", "hsn": "7306", "gst_rate": "28"}]})
    result = checks.check_hsn_rate_check(_Input(wrong, rule, pack=pack))
    assert result.outcome == "warn"
    assert "18%" in result.message

    unknown = _record({"line_items": [{"description": "WIDGET", "hsn": "999999", "gst_rate": "18"}]})
    assert checks.check_hsn_rate_check(_Input(unknown, rule, pack=pack)).outcome == "warn"

    ok = _record({"line_items": [{"description": "MS PIPE", "hsn": "7306", "gst_rate": "18"}]})
    assert checks.check_hsn_rate_check(_Input(ok, rule, pack=pack)).outcome == "pass"


def test_bank_categorisation_uses_the_pack_keywords(pack):
    rule = _rule(pack, "bank_line_categorisation")
    charges = _record(
        {"narration": "BANK CHARGES GST ON CHARGES", "debit": Decimal("826.00")},
        doc_type="bank_statement",
    )
    # The keywords are pack configuration, so the check is given the pack's own settings.
    result = checks.check_bank_line_categorisation(
        _Input(charges, rule, settings=rule.settings, pack=pack)
    )
    assert result.outcome == "pass"
    assert result.details["category"] == "bank_charges"


# ── the engine as a whole ───────────────────────────────────────────────────


def test_rules_engine_caps_a_warn_rule_so_it_cannot_block_auto_post(pack):
    """A rule declared as a warning can never produce a failure, however badly it
    fails, because only fails block automatic posting."""
    engine = RuleEngine(pack)
    record = _record({"line_items": [{"description": "X", "hsn": "7306", "gst_rate": "28"}]})
    summary = engine.evaluate(record, RuleContext(pack=pack), LookupBundle())
    hsn = next(r for r in summary.results if r.rule_key == "hsn_rate_check")
    assert hsn.severity == "warn"
    assert hsn.outcome == "warn"
    # Other rules may fail on this near-empty record, but the warn rule never can.
    assert "hsn_rate_check" not in summary.failed_keys


def test_tenant_can_switch_a_rule_off_and_change_its_severity(pack):
    engine = RuleEngine(pack)
    record = _record({"vendor_gstin": "27AAPFU0939F1ZX"})
    context = RuleContext(
        pack=pack,
        enabled={"gstin_format": False},
        severity_overrides={"gstin_format": "warn"},
    )
    summary = engine.evaluate(record, context, LookupBundle())
    gstin_rule = next(r for r in summary.results if r.rule_key == "gstin_format")
    assert gstin_rule.outcome == "skipped"

    context = RuleContext(pack=pack, severity_overrides={"gstin_format": "warn"})
    summary = engine.evaluate(record, context, LookupBundle())
    gstin_rule = next(r for r in summary.results if r.rule_key == "gstin_format")
    assert gstin_rule.outcome == "warn"


def test_extracted_samples_route_as_the_evaluation_set_labels_them(pack, sample_text, sample_truth):
    """The labelled cases must produce the labelled rule outcomes end to end."""
    from engine.validation.rules import RuleEngine

    fixtures = {}
    engine = RuleEngine(pack)
    for case_id in ("inv-001-intra-state-clean", "inv-004-total-mismatch", "inv-006-future-date"):
        segments = split_documents(sample_text(case_id), pack)
        record = extract_segment(segments[0], pack)[0]
        truth = sample_truth(case_id)
        lookups = LookupBundle(
            gstin_status={
                record.payload["vendor_gstin"]: GstinStatus(
                    gstin=record.payload["vendor_gstin"], state="active", source="fixture"
                )
            },
            gstr2b=Gstr2bFinding(found=True, period="2026-10"),
            duplicate=DuplicateFinding(exact=False, similarity=0.0, method="none"),
        )
        summary = engine.evaluate(record, RuleContext(pack=pack), lookups)
        by_key = {r.rule_key: r.outcome for r in summary.results}
        for rule_key, expected in (truth.get("expected_rules") or {}).items():
            if rule_key in {"duplicate_invoice", "three_way_match", "eway_bill_consistency"}:
                continue
            if rule_key == "gstr2b_match":
                continue
            assert by_key.get(rule_key) == expected, f"{case_id} {rule_key}: {by_key.get(rule_key)} != {expected}"
